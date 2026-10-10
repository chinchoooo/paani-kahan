"""Rain, as one seamless field over the whole region.

Every tile asks the same service for rain on its own grid, so neighbouring tiles always
agree at their seams, whatever the source:

  live     Open-Meteo 15-minute precipitation on a fixed 0.125° lattice (about 14 km,
           close to the forecast models' own resolution). Points are fetched in batches,
           only where tiles need them, and refreshed every 15 minutes near you and hourly
           further out. Bilinear interpolation between lattice points.
  demo     An elevated storm: a band of heavy rain (a rare downpour for Chennai) that
           crosses wherever you are from the south-east, timed to reach you at about
           6 PM. Version 1 is the 'nowcast update' where the band slows and intensifies.
           It runs on your real streets; only the rain is scripted, and it is labelled.
  replay   Recorded hourly rain (ERA5) from a past Chennai storm on the same lattice,
           cached on disk forever (the past doesn't change).
"""
from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates

from . import net
from ..engine.rain import TZ, now_local

LATTICE = 0.125
FORECAST = os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
ARCHIVE = os.getenv("OPEN_METEO_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive")
SCENARIOS = {
    0: {"peak": 58.0, "speed": 165.0, "arrive": 100, "label": "Demo · elevated storm · nowcast at 4:10 PM"},
    1: {"peak": 84.0, "speed": 118.0, "arrive": 88, "label": "Demo · elevated storm · updated nowcast: band slowing, heavier"},
}
NEAR_KM = 30


@dataclass
class Run:
    id: str
    source: str           # live | demo | replay:<key>
    label: str
    version: int
    now_ts: float         # epoch seconds of t = 0
    now_hm: tuple
    detail: dict

    @property
    def kind(self):
        return self.source.split(":")[0]


def _lat_idx(lat):
    return math.floor(lat / LATTICE)


class RainService:
    def __init__(self, cache: net.DiskCache):
        self.cache = cache
        self.lock = threading.RLock()
        self.source = "live"
        self.anchor = (12.979, 80.223)
        self.version = 0
        self.points = {}       # (i, j) -> {"t": epoch s array, "v": mm/h array, "at": fetched}
        self.wanted = {}       # (i, j) -> last time a tile asked for it
        self.error = None
        self.replay = None     # replay metadata
        self.run = self._new_run()
        tex = gaussian_filter(np.random.default_rng(42).normal(0, 1, (400, 400)), 9, mode="wrap")
        self._tex = (tex / tex.std()).astype(np.float32)

    # ------------------------------------------------------------- runs
    def demo_run(self, version):
        h, m = (int(v) for v in os.getenv("DEMO_NOW", "16:10").split(":"))
        now = now_local().replace(hour=h, minute=m, second=0, microsecond=0)
        a = self.anchor
        return Run(f"demo-{a[0]:.3f}-{a[1]:.3f}-v{version}", "demo", SCENARIOS[min(version, 1)]["label"],
                   version, now.timestamp(), (h, m), {"anchor": list(a)})

    def _new_run(self):
        if self.source == "demo":
            return self.demo_run(self.version)
        if self.source.startswith("replay:"):
            r = self.replay
            t0 = dt.datetime.fromtimestamp(r["now_ts"], TZ)
            return Run(f"replay-{r['key']}", self.source, f"Replay · {r['name']} · recorded rain from {t0.strftime('%-d %b %Y, %-I:%M %p')}",
                       0, r["now_ts"], (t0.hour, t0.minute), {"replay": r})
        now = now_local()
        ok = sum(1 for p in self.points.values() if p.get("v") is not None)
        label = f"Live · Open-Meteo 15-min · updated {now.strftime('%-I:%M %p')}" if ok else "Live · waiting for Open-Meteo"
        return Run(f"live-{int(now.timestamp())}", "live", label, 0, now.timestamp(), (now.hour, now.minute), {"points": ok})

    def set_source(self, source, anchor=None):
        with self.lock:
            if anchor:
                self.anchor = (round(anchor[0], 3), round(anchor[1], 3))
            prev = (self.source, self.points, self.replay)
            if source != self.source:
                self.points = {}
            if source.startswith("replay:"):
                try:
                    self._load_replay(source.split(":", 1)[1])
                except Exception:
                    self.source, self.points, self.replay = prev
                    raise
                self._load_replay(source.split(":", 1)[1])
            self.source = source
            self.version = 0
            self.run = self._new_run()
            return self.run

    def bump_demo(self, v=None):
        with self.lock:
            self.version = min(self.version + 1, 1) if v is None else v
            self.run = self._new_run()
            return self.run

    # ------------------------------------------------------------ lattice
    def lattice_for(self, bbox, margin=1):
        s, w, n, e = bbox
        return [(i, j) for i in range(_lat_idx(s) - margin, _lat_idx(n) + margin + 1)
                for j in range(_lat_idx(w) - margin, _lat_idx(e) + margin + 1)]

    def ensure(self, bbox):
        """Make sure lattice points covering bbox have data (fetches only what's missing)."""
        if self.source == "demo":
            return
        keys = self.lattice_for(bbox)
        now = time.time()
        with self.lock:
            for k in keys:
                self.wanted[k] = now
            missing = [k for k in keys if k not in self.points]
        if missing:
            self._fetch(missing)
            with self.lock:
                if self.source == "live" and "waiting" in self.run.label and any(p.get("v") is not None for p in self.points.values()):
                    t = __import__("datetime").datetime.fromtimestamp(self.run.now_ts, TZ)
                    self.run.label = f"Live · Open-Meteo 15-min · updated {t.strftime('%-I:%M %p')}"

    def refresh(self, focus=None, force=False):
        """Live: re-fetch stale points (15 min near the focus, 60 min elsewhere). Returns True if a new run started."""
        if self.source != "live":
            return False
        now = time.time()
        with self.lock:
            keys = [k for k, at in self.wanted.items() if now - at < 6 * 3600]
        stale = []
        for k in keys:
            p = self.points.get(k)
            near = focus and math.hypot((k[0] + .5) * LATTICE - focus[0], (k[1] + .5) * LATTICE - focus[1]) * 111 < NEAR_KM
            age = now - p["at"] if p else 1e9
            if force or age > (14 * 60 if near else 55 * 60):
                stale.append(k)
        if not stale:
            return False
        self._fetch(stale)
        with self.lock:
            self.run = self._new_run()
        return True

    def _fetch(self, keys):
        if self.source.startswith("replay:"):
            return self._fetch_replay(keys)
        for b in range(0, len(keys), 60):
            batch = keys[b:b + 60]
            lats = [(i + 0.5) * LATTICE for i, _ in batch]
            lons = [(j + 0.5) * LATTICE for _, j in batch]
            base = {"latitude": ",".join(f"{v:.4f}" for v in lats), "longitude": ",".join(f"{v:.4f}" for v in lons), "timezone": "GMT"}
            try:
                js, step, field = None, 15, "minutely_15"
                try:
                    r = net.METEO.get(FORECAST, params={**base, "minutely_15": "precipitation", "past_minutely_15": 12, "forecast_minutely_15": 28})
                    js = r.json()
                    js = js if isinstance(js, list) else [js]
                    if all(all(v is None for v in d[field]["precipitation"]) for d in js):
                        js = None
                except Exception:  # noqa: BLE001
                    js = None
                if js is None:
                    r = net.METEO.get(FORECAST, params={**base, "hourly": "precipitation", "past_hours": 3, "forecast_hours": 8})
                    js = r.json()
                    js = js if isinstance(js, list) else [js]
                    step, field = 60, "hourly"
                now = time.time()
                with self.lock:
                    for k, d in zip(batch, js):
                        ts = np.array([dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp() for t in d[field]["time"]])
                        v = np.nan_to_num(np.array([x if x is not None else np.nan for x in d[field]["precipitation"]], dtype=float)) * (60 / step)
                        self.points[k] = {"t": ts - step * 30, "v": v, "at": now, "step": step}
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"[:160]
                now = time.time()
                with self.lock:
                    for k in batch:
                        self.points.setdefault(k, {"t": None, "v": None, "at": now - 13 * 60})  # retry in ~2 min

    # ------------------------------------------------------------- replays
    def _load_replay(self, key):
        from ..engine.chennai import REPLAYS, antecedent_effect
        ev = REPLAYS[key]
        self.replay = {"key": key, **ev}
        i, j = _lat_idx(self.anchor[0]), _lat_idx(self.anchor[1])
        self.points = {}
        self.source = f"replay:{key}"
        self._fetch_replay([(i, j)])
        p = self.points.get((i, j))
        if not p or p.get("v") is None:
            raise RuntimeError(self.error or "replay data unavailable")
        t, v = p["t"], p["v"]
        first = int(np.searchsorted(t, dt.datetime.fromisoformat(ev["start"]).replace(tzinfo=TZ).timestamp()))
        win = np.convolve(v, np.ones(6), mode="valid")
        i0 = first + int(win[first:].argmax())
        now_i = max(first, i0 - 1)
        before = v[:now_i]
        self.replay.update({"now_ts": float(t[now_i] + 1800), "total_6h": round(float(win[first:].max()), 1),
                            "antecedent": antecedent_effect(float(before[-72:].sum()), float(before[-168:].sum())),
                            "date": dt.datetime.fromtimestamp(t[now_i], TZ).strftime("%-d %b %Y")})

    def _fetch_replay(self, keys):
        from ..engine.chennai import REPLAYS
        r = self.replay
        ev = REPLAYS[r["key"]]
        start = (dt.date.fromisoformat(ev["start"]) - dt.timedelta(days=7)).isoformat()
        todo = []
        for k in keys:
            doc = self.cache.get("replay", r["key"], f"{k[0]}_{k[1]}")
            if doc:
                with self.lock:
                    self.points[k] = {"t": np.array(doc["t"]), "v": np.array(doc["v"]), "at": time.time()}
            else:
                todo.append(k)
        for b in range(0, len(todo), 40):
            batch = todo[b:b + 40]
            try:
                resp = net.METEO_ARCHIVE.get(ARCHIVE, params={
                    "latitude": ",".join(f"{(i + .5) * LATTICE:.4f}" for i, _ in batch),
                    "longitude": ",".join(f"{(j + .5) * LATTICE:.4f}" for _, j in batch),
                    "start_date": start, "end_date": ev["end"], "hourly": "precipitation", "timezone": "GMT"})
                js = resp.json()
                js = js if isinstance(js, list) else [js]
                for k, d in zip(batch, js):
                    ts = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp() - 1800 for t in d["hourly"]["time"]]
                    v = [float(x or 0) for x in d["hourly"]["precipitation"]]
                    self.cache.put({"t": ts, "v": v}, "replay", r["key"], f"{k[0]}_{k[1]}")
                    with self.lock:
                        self.points[k] = {"t": np.array(ts), "v": np.array(v), "at": time.time()}
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"[:160]

    # --------------------------------------------------------------- fields
    def grid(self, bbox, rows, cols, cell_m, run=None):
        return GridRain(self, run or self.run, bbox, rows, cols)

    def _lattice_field(self, run, bbox, t_min, lat, lon):
        """Bilinear interpolation of lattice values at (lat, lon) arrays."""
        keys = self.lattice_for(bbox, margin=1)
        i0, j0 = min(k[0] for k in keys), min(k[1] for k in keys)
        ni, nj = max(k[0] for k in keys) - i0 + 1, max(k[1] for k in keys) - j0 + 1
        L = np.zeros((ni, nj))
        ts = run.now_ts + t_min * 60
        for (i, j) in keys:
            p = self.points.get((i, j))
            if p and p.get("v") is not None and len(p["t"]):
                L[i - i0, j - j0] = np.interp(ts, p["t"], p["v"], left=p["v"][0], right=0.0)
        gi = lat / LATTICE - 0.5 - i0
        gj = lon / LATTICE - 0.5 - j0
        return np.clip(map_coordinates(L, [gi, gj], order=1, mode="nearest"), 0, None)

    def _demo_field(self, run, t_min, lat, lon):
        p = SCENARIOS[min(run.version, 1)]
        a = run.detail["anchor"]
        y = (lat - a[0]) * 110_574.0
        x = (lon - a[1]) * 111_320.0 * math.cos(math.radians(a[0]))
        proj = x * -0.7071 + y * 0.7071
        perp = x * 0.7071 + y * 0.7071
        c = p["speed"] * (t_min - p["arrive"])
        along = proj - c
        band = np.exp(-(along / 4500.0) ** 2 / 2)
        tc = map_coordinates(self._tex, [(along / 100.0) % 400, (perp / 100.0) % 400], order=1, mode="wrap")
        cells = np.clip(1 + 0.32 * tc, 0.35, 1.9)
        return 2.0 + p["peak"] * band * cells

    def field_at(self, run, bbox, t_min, lat, lon):
        if run.kind == "demo":
            return self._demo_field(run, t_min, lat, lon)
        return self._lattice_field(run, bbox, t_min, lat, lon)

    def series_at(self, lat, lon, times, run=None):
        run = run or self.run
        bb = [lat - 0.01, lon - 0.01, lat + 0.01, lon + 0.01]
        la, lo = np.array([lat]), np.array([lon])
        return [round(float(self.field_at(run, bb, t + 5, la, lo)[0]), 1) for t in times]

    def status(self):
        with self.lock:
            pts = [p for p in self.points.values() if p.get("v") is not None]
        return {"source": self.source, "run": self.run.id, "label": self.run.label, "lattice_deg": LATTICE,
                "points": len(pts), "error": self.error, "replay": self.replay and {k: self.replay[k] for k in ("key", "name", "date", "total_6h") if k in self.replay}}


class GridRain:
    """The rain field on one tile's grid, evaluated coarsely (every 4 cells) and upsampled."""

    def __init__(self, svc: RainService, run: Run, bbox, rows, cols):
        self.svc, self.run, self.bbox, self.rows, self.cols = svc, run, bbox, rows, cols
        self.version = run.version
        self.label = run.label
        s, w, n, e = bbox
        self.k = 4
        rr = (np.arange(0, rows, self.k) + self.k / 2) / rows
        cc = (np.arange(0, cols, self.k) + self.k / 2) / cols
        self.lat = (n - rr * (n - s))[:, None].repeat(len(cc), 1)
        self.lon = (w + cc * (e - w))[None, :].repeat(len(rr), 0)

    def field(self, t_min):
        f = self.svc.field_at(self.run, self.bbox, t_min, self.lat, self.lon)
        up = np.repeat(np.repeat(f, self.k, 0), self.k, 1)[:self.rows, :self.cols]
        return up.astype(np.float32)
