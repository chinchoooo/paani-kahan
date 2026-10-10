"""Rainfall nowcasts.

Two sources:
  demo        A scripted storm band crossing the area from the south-east, used for
              recording the demo so every run is identical. Version 0 is the 4:10 PM
              nowcast; version 1+ is the 'nowcast update' where the band slows down and
              intensifies (the classic Chennai north-east monsoon pattern).
  open-meteo  Live hourly precipitation for the area centre from api.open-meteo.com,
              shaped across the area with the same band geometry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SCENARIOS = {
    0: {"peak": 58.0, "speed_m_per_min": 165.0, "arrive_min": 100, "label": "Demo storm · nowcast at 4:10 PM"},
    1: {"peak": 84.0, "speed_m_per_min": 118.0, "arrive_min": 88, "label": "Demo storm · updated nowcast: band slowing, heavier"},
}


@dataclass
class RainModel:
    rows: int
    cols: int
    cell_m: float
    version: int = 0
    source: str = "demo"
    hourly_mm: list | None = None  # open-meteo: mm for each of the next hours

    def __post_init__(self):
        rr, cc = np.mgrid[0:self.rows, 0:self.cols]
        x = cc * self.cell_m
        y = (self.rows - rr) * self.cell_m  # north-up metres
        xc, yc = x.mean(), y.mean()
        # distance along a south-east -> north-west axis
        self._proj = (x - xc) * -0.7071 + (y - yc) * 0.7071
        # perpendicular axis, and a smooth random field of convective cells that rides with the band
        self._perp = (x - xc) * 0.7071 + (y - yc) * 0.7071
        rng = np.random.default_rng(42)
        from scipy.ndimage import gaussian_filter
        tex = gaussian_filter(rng.normal(0, 1, (400, 400)), 9, mode="wrap")
        self._tex = (tex / tex.std()).astype(np.float32)  # 100 m texture cells, wraps every 40 km

    @property
    def params(self):
        return SCENARIOS[min(self.version, max(SCENARIOS))]

    @property
    def label(self):
        if self.source == "open-meteo":
            return "Open-Meteo hourly forecast"
        return self.params["label"]

    def field(self, t_min: float) -> np.ndarray:
        """Rain intensity in mm/h for every cell at t_min minutes after 'now'."""
        p = self.params
        sigma = 4500.0
        c = p["speed_m_per_min"] * (t_min - p["arrive_min"])  # band centre crosses mid-area at arrive_min
        along = self._proj - c
        band = np.exp(-(along / sigma) ** 2 / 2)
        from scipy.ndimage import map_coordinates
        tc = map_coordinates(self._tex, [(along / 100.0) % 400, (self._perp / 100.0) % 400], order=1, mode="wrap")
        cells = np.clip(1 + 0.32 * tc, 0.35, 1.9)
        if self.source == "open-meteo" and self.hourly_mm:
            h = max(0.0, t_min / 60.0)
            i = min(int(h), len(self.hourly_mm) - 1)
            j = min(i + 1, len(self.hourly_mm) - 1)
            f = h - int(h)
            base = self.hourly_mm[i] * (1 - f) + self.hourly_mm[j] * f
            shape = band / max(band.mean(), 1e-6)
            return np.clip(base * (0.6 + 0.4 * shape) * cells, 0, None).astype(np.float32)
        return (2.0 + p["peak"] * band * cells).astype(np.float32)


def fetch_open_meteo(lat: float, lon: float, timeout: float = 5.0):
    import httpx
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=timeout, params={
        "latitude": lat, "longitude": lon, "hourly": "precipitation",
        "forecast_hours": 8, "timezone": "Asia/Kolkata"})
    r.raise_for_status()
    vals = r.json()["hourly"]["precipitation"]
    return [float(v or 0) for v in vals[:8]]


# ----------------------------------------------------------------- live rain
import datetime as _dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")


def now_local():
    return _dt.datetime.now(TZ)


def _grid_points(bbox, n):
    s, w, n_, e = bbox
    lats = [n_ - (i + 0.5) / n * (n_ - s) for i in range(n)]   # north -> south
    lons = [w + (j + 0.5) / n * (e - w) for j in range(n)]      # west -> east
    return lats, lons


def fetch_live(bbox, n=3, timeout=8.0):
    """Precipitation from Open-Meteo at an n x n grid of points over the area.

    15-minute data for the last 2 hours and the next 6.5 hours (falls back to hourly).
    Returns {"mmph": (n*n, T) array, "t_min": minutes relative to now, "fetched": "HH:MM", ...}.
    """
    import httpx
    lats, lons = _grid_points(bbox, n)
    pts = [(la, lo) for la in lats for lo in lons]
    base = {"latitude": ",".join(f"{p[0]:.4f}" for p in pts), "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
            "timezone": "Asia/Kolkata"}
    now = now_local()

    def get(params):
        r = httpx.get("https://api.open-meteo.com/v1/forecast", params={**base, **params}, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        return j if isinstance(j, list) else [j]

    rows, times, step = None, None, 15
    try:
        js = get({"minutely_15": "precipitation", "past_minutely_15": 8, "forecast_minutely_15": 27})
        times = js[0]["minutely_15"]["time"]
        rows = [[(v if v is not None else np.nan) for v in d["minutely_15"]["precipitation"]] for d in js]
        if np.all(np.isnan(np.array(rows, dtype=float))):
            rows = None
    except Exception:  # noqa: BLE001
        rows = None
    if rows is None:
        js = get({"hourly": "precipitation", "past_hours": 3, "forecast_hours": 8})
        times = js[0]["hourly"]["time"]
        rows = [[(v if v is not None else np.nan) for v in d["hourly"]["precipitation"]] for d in js]
        step = 60
    arr = np.nan_to_num(np.array(rows, dtype=float), nan=0.0)
    mmph = arr * (60 / step)  # values are totals over the preceding interval
    t_min = []
    for ts in times:
        t = _dt.datetime.fromisoformat(ts).replace(tzinfo=TZ)
        t_min.append((t - now).total_seconds() / 60 - step / 2)  # centre of the interval
    return {"mmph": mmph, "t_min": np.array(t_min), "n": n, "step": step,
            "fetched": now.strftime("%-I:%M %p") if hasattr(now, "strftime") else "",
            "now": (now.hour, now.minute), "points": pts}


@dataclass
class LiveRain:
    rows: int
    cols: int
    cell_m: float
    data: dict
    version: int = 0
    source: str = "live"

    @property
    def label(self):
        if self.data.get("replay"):
            return f"Replay · {self.data['replay']} · recorded rain from {self.data['fetched']}"
        res = "15-min" if self.data["step"] == 15 else "hourly"
        return f"Live · Open-Meteo {res} · updated {self.data['fetched']}"

    def field(self, t_min: float) -> np.ndarray:
        from scipy.ndimage import zoom
        d = self.data
        vals = np.array([np.interp(t_min, d["t_min"], d["mmph"][k], left=d["mmph"][k][0], right=0.0)
                         for k in range(d["mmph"].shape[0])]).reshape(d["n"], d["n"])
        if d["n"] == 1:
            return np.full((self.rows, self.cols), float(vals[0, 0]), dtype=np.float32)
        up = zoom(vals, (self.rows / d["n"], self.cols / d["n"]), order=1, mode="nearest", grid_mode=True)
        out = np.zeros((self.rows, self.cols), dtype=np.float32)
        r, c = min(self.rows, up.shape[0]), min(self.cols, up.shape[1])
        out[:r, :c] = up[:r, :c]
        return np.clip(out, 0, None)
