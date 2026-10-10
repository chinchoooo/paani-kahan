"""Paani Kahan API server.

Run:  uvicorn app.server:app --port 8080
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alerts
from .agent import Advisor, clock, model_status
from .engine.area import DATA_DIR, load_area
from .engine.history import seed_reports
from .engine.hydro import FloodModel
from .engine import chennai, traffic
from .engine.rain import LiveRain, RainModel, fetch_live, now_local
from .engine.routing import LIMIT_CM, Router
from .engine.story import pick_story
from .policy import DECISION_LOG, USERS, Policy
from .store import QUERY_LOG, connect

WEB = Path(__file__).parent / "web"
ROAD_CLASS = {"motorway": 0, "trunk": 0, "primary": 0, "secondary": 1, "tertiary": 1}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.ready = False
        self.boot_log = []
        self.advice_cache = {}
        self.building = False

    def say(self, msg):
        print(f"[paani] {msg}", flush=True)
        self.boot_log.append(msg)

    def boot(self, area_id=None):
        t0 = time.time()
        self.area = load_area(area_id or os.getenv("AREA") or None)
        self.say(f"Area '{self.area.meta['name']}': {len(self.area.segments)} street segments, grid {self.area.dem.shape}")
        self.model = FloodModel(self.area)
        self.router = Router(self.area)
        segs = self.area.segments
        self.seg_mid = np.array([[s.mid[0] * 110_574, s.mid[1] * self.area.m_per_deg_lon] for s in segs]) if segs else np.zeros((0, 2))
        self.say(f"Terrain: {self.model.ncomp} depressions found · {sum(sg.underpass for sg in segs)} subway segments · "
                 f"{sum(sg.construction for sg in segs)} under construction · {len(self.area.barriers)} barriers")
        self.seg_index = traffic.SegmentIndex(self.area)
        self.closures = []
        self.context = {}
        try:
            lat_c, lon_c = self.area.lat0, (self.area.bbox[1] + self.area.bbox[3]) / 2
            self.climate = chennai.fetch_climate(lat_c, lon_c, DATA_DIR / self.area.area_id / "climate.json")
            self.say(f"Climate {self.climate['period']}: 2-yr hourly rain {self.climate['hourly']['T2']} mm/h, "
                     f"100-yr {self.climate['hourly']['T100']} mm/h ({self.climate['years']} years of ERA5)")
            self.model.set_context(base_drain=chennai.drain_capacity_from_climate(self.climate))
        except Exception as e:  # noqa: BLE001
            self.climate = None
            self.say(f"Climate record unavailable ({type(e).__name__}); drains at the default 18 mm/h")
        self.documented = chennai.documented_reports(self.area)
        if self.documented:
            self.say(f"Documented past closures in this area: {len(self.documented)}")
        self.rain_source = os.getenv("RAIN_SOURCE", "live")
        if not (self.rain_source in ("live", "demo") or self.rain_source.startswith("replay:")):
            self.rain_source = "live"
        self.live = None
        self.resident = load_reports()
        self.policy = Policy(self.model.loc_names)
        self.store, warn = connect(os.getenv("OPENSEARCH_URL", "http://localhost:9200"),
                                   float(os.getenv("OPENSEARCH_WAIT_S", "60")))
        self.store_warning = warn
        if warn:
            self.say(warn)
        self.setup_mode()
        self.version = 0
        self.run()
        self.ready = True
        self.say(f"Ready in {time.time() - t0:.1f}s")

    def setup_mode(self):
        """Reports, calibration, users and the demo commute depend on live vs demo."""
        demo = self.rain_source != "live"
        seeds = seed_reports(self.area, self.model) if self.rain_source == "demo" else []
        water = [r for r in self.resident if r.get("kind") != "closed"]
        self.reports = self.documented + seeds + water
        real = self.documented + water
        cal = real if len(real) >= 3 else seeds
        drains = self.model.calibrate(cal)
        self.say(f"Drain capacity (mm/h): {drains}" + ("" if cal else " (no reports yet: baseline)"))
        USERS["priya"]["label"] = "Priya" if demo else "You"
        USERS["priya"]["desc"] = "Resident · demo commuter" if demo else "Resident"
        self.demo_persona = None
        if demo:
            self.demo_persona = self.area.meta.get("persona")
            if not self.demo_persona or self.demo_persona.get("name") == "You" and self.area.area_id != "trip":
                self.say("Choosing demo commute...")
                self.demo_persona = pick_story(self.area, self.model, self.router)
                self.area.meta["persona"] = self.demo_persona
                (DATA_DIR / self.area.area_id / "area.json").write_text(json.dumps(self.area.meta, indent=1))
        self.store.setup(self.area, self.reports)
        self.say(f"Store: {self.store.engine} · {len(self.documented)} documented, {len(water)} resident, {len(seeds)} seed reports")

    # ------------------------------------------------------------ commute
    @property
    def persona(self):
        """The saved commute (live), or the demo commute, with depart_min worked out from the clock."""
        saved = load_commutes().get("priya")
        if self.area.area_id == "trip" and self.area.meta.get("persona"):
            base = dict(self.area.meta["persona"])
        elif self.rain_source != "live" and self.demo_persona:
            base = dict(self.demo_persona)  # the demo story, so the video is reproducible
        elif saved:
            base = dict(saved)
        else:
            return None
        if "leave" in base:
            hh, mm = map(int, base["leave"].split(":"))
            mins = (hh * 60 + mm) - (self.now[0] * 60 + self.now[1])
            if mins < -30:
                mins += 24 * 60
            base["depart_min"] = max(0, mins)
            base["depart_label"] = clock(self.now, mins)
            base["in_window"] = mins <= 340
        else:
            base["in_window"] = True
        return base

    # ------------------------------------------------------------ forecast
    def run(self, refetch=True):
        rows, cols = self.area.dem.shape
        lat_c, lon_c = self.area.lat0, (self.area.bbox[1] + self.area.bbox[3]) / 2
        start, label_override = -30, None
        ctx = {"ground": None, "tide": None}
        if self.rain_source == "live":
            if refetch or self.live is None:
                try:
                    n = 3 if max(rows, cols) * self.area.cell_m < 8000 else 5
                    self.live = fetch_live(self.area.bbox, n=n)
                    self.live_error = None
                except Exception as e:  # noqa: BLE001
                    self.live_error = f"{type(e).__name__}: {e}"[:160]
                    self.say(f"Live rain fetch failed ({self.live_error})")
                try:
                    self.ante = chennai.fetch_antecedent(lat_c, lon_c)
                except Exception:  # noqa: BLE001
                    self.ante = None
                try:
                    self.tide = chennai.fetch_tide(lat_c, lon_c)
                except Exception:  # noqa: BLE001
                    self.tide = None
                self.refresh_traffic()
            if self.live:
                self.now = self.live["now"]
                rain = LiveRain(rows, cols, self.area.cell_m, self.live)
                start = -120
            else:
                lt = now_local()
                self.now = (lt.hour, lt.minute)
                rain = RainModel(rows, cols, self.area.cell_m, version=self.version)
                label_override = "Live rain unavailable · showing demo storm"
            ante, tide = getattr(self, "ante", None), getattr(self, "tide", None)
        elif self.rain_source.startswith("replay:"):
            key = self.rain_source.split(":", 1)[1]
            try:
                data = chennai.fetch_replay(self.area.bbox, key)
                self.now = data["now"]
                rain = LiveRain(rows, cols, self.area.cell_m, data)
                start = -120
                ante, tide = data["antecedent"], None
                self.replay_info = {"name": data["replay"], "date": data["date"], "total_6h": data["total_6h"]}
            except Exception as e:  # noqa: BLE001
                self.live_error = f"Replay unavailable: {type(e).__name__}"
                self.say(self.live_error)
                self.rain_source = "demo"
                return self.run()
        else:
            h, m = os.getenv("DEMO_NOW", "16:10").split(":")
            self.now = (int(h), int(m))
            rain = RainModel(rows, cols, self.area.cell_m, version=self.version)
            ante, tide = chennai.antecedent_effect(30, 70), None  # a damp week, fixed for reproducibility
        self.model.set_context(runoff=(ante or {}).get("runoff", 0.85), drain_eff=(ante or {}).get("drain_eff", 1.0), tide=tide)
        ctx = {"ground": ante, "tide": tide and {k: tide[k] for k in ("next_high_min", "next_high_m", "low", "high")}}
        rid = f"run-{int(time.time())}-{self.rain_source.replace(':', '-')}-v{self.version}"
        t = time.time()
        raw = self.model.simulate(rain, rid, start=start)
        if label_override:
            raw.label = label_override
        self.say(f"Simulated {rid}: {raw.total_mm} mm over 6 h in {time.time() - t:.1f}s")
        # how rare is the heaviest hour in this forecast, for Chennai?
        rm = np.array(raw.rain_mean)
        peak_h = float(np.convolve(rm, np.ones(6) / 6, mode="valid").max()) if len(rm) >= 6 else float(rm.max())
        rp = chennai.return_period(peak_h, self.climate["hourly"]["u"], self.climate["hourly"]["alpha"]) if self.climate else None
        ctx["peak_hourly_mm"] = round(peak_h, 1)
        ctx["return_years"] = round(rp, 1) if rp and peak_h > 5 else None
        ctx["drain_mmph"] = round(self.model.base_drain, 1)
        ctx["climate"] = self.climate and {k: self.climate[k] for k in ("source", "years", "period", "wettest_hour", "wettest_day")} | {
            "hourly": {k: v for k, v in self.climate["hourly"].items() if k.startswith("T")}}
        self.context = ctx
        self.apply_closures()
        with self.lock:
            self.sim_raw = raw
            self.sim = assimilate(raw, self.resident, self)
            self.advice_cache = {}
        self.store.index_forecast(self.area, self.sim)
        self.last_run = time.time()

    # ------------------------------------------------------------ closures
    def refresh_traffic(self):
        self.tt_closed, self.tt_slow, self.tt_records, self.tt_error = set(), {}, [], None
        if not traffic.key():
            self.tt_error = "TOMTOM_API_KEY not set"
            return
        try:
            inc = traffic.fetch_incidents(self.area.bbox)
            self.tt_closed, self.tt_slow, self.tt_records = traffic.to_closures(inc, self.seg_index)
            self.say(f"TomTom: {len(inc)} incidents, {len(self.tt_closed)} street segments closed")
        except Exception as e:  # noqa: BLE001
            self.tt_error = f"{type(e).__name__}: {e}"[:160]

    def apply_closures(self):
        """Live incidents + resident 'road closed' reports (3 h) -> the router."""
        now = time.time()
        res = [r for r in self.resident if r.get("kind") == "closed" and now - r.get("ts", 0) < 3 * 3600 and r.get("seg") is not None]
        closed = set(getattr(self, "tt_closed", set())) | {r["seg"] for r in res}
        self.router.closed = closed
        self.router.slow = dict(getattr(self, "tt_slow", {}))
        recs = list(getattr(self, "tt_records", []))
        for r in res:
            recs.append({"segs": [r["seg"]], "kind": "Road closed", "closes": True, "desc": f"Reported closed by a resident at {r.get('time', '')}",
                         "until": None, "source": "resident"})
        for sg in self.area.segments:
            if sg.construction:
                recs.append({"segs": [sg.idx], "kind": "Under construction", "closes": True, "desc": "Road under construction (OpenStreetMap)",
                             "until": None, "source": "OpenStreetMap"})
        self.closures = recs

    def trip(self, req=None):
        """The commute by default, or any from/to the person searched for."""
        if req is None or req.from_ is None or req.to is None:
            return self.persona
        return {"name": "You", "from": list(req.from_), "to": list(req.to),
                "from_label": req.from_label or "Start", "to_label": req.to_label or "Destination"}

    def plan(self, mode, depart, trip=None):
        t = trip or self.persona
        return self.router.plan(t["from"], t["to"], mode, min(depart, 340), self.sim)

    def advise(self, mode, depart, trip=None):
        t = trip or self.persona
        key = (self.sim.run_id, mode, depart, tuple(t["from"]), tuple(t["to"]))
        if key not in self.advice_cache:
            ctx = {"router": self.router, "sim": self.sim, "store": self.store, "persona": t,
                   "mode": mode, "depart_min": min(depart, 340), "now": self.now}
            self.advice_cache[key] = Advisor(ctx).run()
        return self.advice_cache[key]


COMMUTES = DATA_DIR / "commutes.json"
REPORTS = DATA_DIR / "reports.json"


def load_commutes():
    try:
        return json.loads(COMMUTES.read_text())
    except Exception:  # noqa: BLE001
        return {}


def load_reports():
    try:
        return json.loads(REPORTS.read_text())
    except Exception:  # noqa: BLE001
        return []


def assimilate(raw, reports, st):
    """Correct the next 90 minutes of the forecast near recent resident reports.

    A report of D cm on a street pulls that street (and streets within 120 m) towards D
    now, fading back to the model by +90 min. Reports older than 2 hours are ignored.
    'Road dry' reports pull the forecast down the same way."""
    import copy
    sim = copy.copy(raw)
    sim.seg_depth = raw.seg_depth.copy()
    now = time.time()
    used = 0
    for r in reports:
        if r.get("source") != "resident":
            continue
        age = (now - r.get("ts", 0)) / 60
        if age > 120 or not len(st.seg_mid):
            continue
        p = np.array([r["lat"] * 110_574, r["lon"] * st.area.m_per_deg_lon])
        near = np.nonzero(np.hypot(*(st.seg_mid - p).T) <= 120)[0]
        if not len(near):
            continue
        used += 1
        obs = float(r["depth_cm_reported"]) * (1 - age / 180)
        for fi, t in enumerate(sim.times):
            if t > 90:
                break
            w = 1 - t / 90
            cur = sim.seg_depth[fi, near].astype(float)
            new = np.where(obs < cur, cur * (1 - w) + obs * w, np.maximum(cur, obs * w))
            sim.seg_depth[fi, near] = np.clip(np.round(new), 0, 250)
    sim.observations = used
    return sim


def check_commute_alerts(source):
    p = S.persona
    out = []
    if not p or not p.get("in_window", True):
        return out
    plan = S.plan(p.get("mode", "two_wheeler"), p["depart_min"])
    if plan["status"] in ("alert", "watch"):
        adv = S.advise(p.get("mode", "two_wheeler"), p["depart_min"])
        out.append({"user": "priya", "status": plan["status"], "headline": adv["headline"], "advice": adv["advice"],
                    "depart_min": p["depart_min"], "mode": p.get("mode", "two_wheeler"), "run_id": S.sim.run_id,
                    "engine": adv["engine"]})
    return out


def _refresher():
    """Live mode: re-fetch the rain forecast and re-run every LIVE_REFRESH_MIN minutes."""
    every = float(os.getenv("LIVE_REFRESH_MIN", "15")) * 60
    while True:
        time.sleep(30)
        if not S.ready or S.building or S.rain_source != "live" or time.time() - getattr(S, "last_run", 0) < every:
            continue
        try:
            S.run()
            alerts.log("Scheduled refresh", S.sim.label)
            bus.publish({"type": "forecast", "run_id": S.sim.run_id, "label": S.sim.label})
            for a in check_commute_alerts("refresh"):
                if a["status"] == "alert" and getattr(S, "_last_alert_run", None) != a["run_id"]:
                    S._last_alert_run = a["run_id"]
                    bus.publish({"type": "alert", "via": "scheduled refresh (Open-Meteo)", **a})
        except Exception as e:  # noqa: BLE001
            S.say(f"Refresh failed: {e}")


S = State()
bus = alerts.Bus()
listener = alerts.SqsListener(bus)
app = FastAPI(title="Paani Kahan")
app.add_middleware(GZipMiddleware, minimum_size=2000)


@app.on_event("startup")
async def _startup():
    bus.loop = asyncio.get_running_loop()
    threading.Thread(target=_boot, daemon=True).start()


def _boot():
    try:
        S.boot()
        if os.getenv("ALERTS", "localstack") == "localstack":
            listener.start()
        threading.Thread(target=_refresher, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        S.say(f"Boot failed: {e}")


def need_ready():
    if not S.ready:
        raise HTTPException(503, {"booting": S.boot_log[-1] if S.boot_log else "starting"})


def b64(a: np.ndarray):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def deny(decision):
    return JSONResponse(status_code=403, content={"denied": True, "decision": decision})


# ------------------------------------------------------------------ static
def asset_version():
    """Changes whenever any front-end file changes; used to bust caches and version the service worker."""
    return str(max(int(f.stat().st_mtime) for f in WEB.rglob("*") if f.is_file()))


@app.get("/")
def index():
    # stamp asset URLs so a browser never runs a stale script
    from fastapi.responses import HTMLResponse
    v = asset_version()
    html = (WEB / "index.html").read_text().replace("__V__", v)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    # served from the root so it can control the whole app
    from fastapi.responses import Response
    js = (WEB / "sw.js").read_text().replace("__V__", asset_version())
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=WEB), name="static")


# ------------------------------------------------------------------ data
@app.get("/api/boot")
def boot_status():
    return {"ready": S.ready, "log": S.boot_log[-6:], "building": S.building,
            "area_id": S.area.area_id if hasattr(S, "area") else None}


class LocateReq(BaseModel):
    lat: float
    lon: float


@app.post("/api/locate")
def locate(req: LocateReq):
    """Is this location inside the forecast area? If not, build the area around it."""
    need_ready()
    s, w, n, e = S.area.bbox
    if s <= req.lat <= n and w <= req.lon <= e:
        return {"inside": True}
    if not (6 <= req.lat <= 37 and 68 <= req.lon <= 98):
        return {"inside": False, "building": False, "error": "Forecasts can be built for locations in India."}
    if not S.building:
        threading.Thread(target=_build_here, args=(req.lat, req.lon), daemon=True).start()
    return {"inside": False, "building": True}


def _build_here(lat, lon):
    import importlib.util
    S.building = True
    prev = S.area.area_id
    try:
        S.say(f"Fetching streets and terrain around {lat:.4f}, {lon:.4f}...")
        spec = importlib.util.spec_from_file_location("fetch_area", Path(__file__).resolve().parents[1] / "scripts" / "fetch_area.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bbox = [lat - 0.016, lon - 0.022, lat + 0.016, lon + 0.022]
        mod.build("here", bbox, "Your area", "")
        p = DATA_DIR / "here" / "area.json"
        meta = json.loads(p.read_text())
        locs = meta.get("localities") or []
        if locs:
            near = min(locs, key=lambda L: (L["lat"] - lat) ** 2 + (L["lon"] - lon) ** 2)
            meta["name"] = near["name"]
            p.write_text(json.dumps(meta, indent=1))
        S.say(f"Got {meta['name']}. Running the flood model...")
        S.ready = False
        S.boot("here")
        (DATA_DIR / "ACTIVE").write_text("here")
    except BaseException as ex:  # noqa: BLE001  (fetch_area raises SystemExit on network failure)
        S.say(f"Couldn't build your area: {ex}")
        if not S.ready:
            try:
                S.boot(prev)
            except Exception as e2:  # noqa: BLE001
                S.say(f"Reload failed: {e2}")
    finally:
        S.building = False


@app.get("/api/geocode")
def geocode(q: str):
    """Places anywhere in India, from OpenStreetMap's Nominatim (for destinations beyond the area)."""
    import httpx
    if len(q.strip()) < 3:
        return {"results": []}
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search", timeout=6,
                      params={"q": q, "format": "jsonv2", "countrycodes": "in", "limit": 6},
                      headers={"User-Agent": "paani-kahan/1.0 (flood forecast hackathon demo)"})
        r.raise_for_status()
        out = []
        for p in r.json():
            parts = p.get("display_name", "").split(", ")
            out.append({"name": p.get("name") or parts[0], "sub": ", ".join(parts[1:3]), "kind": "place",
                        "lat": float(p["lat"]), "lon": float(p["lon"]), "seg": None, "far": True})
        return {"results": out}
    except Exception as e:  # noqa: BLE001
        return {"results": [], "error": f"Place search unavailable ({type(e).__name__})"}


class TripReq(BaseModel):
    from_: list[float] = Field(alias="from")
    to: list[float]
    from_label: str = "Start"
    to_label: str = "Destination"
    model_config = {"populate_by_name": True}


@app.post("/api/trip")
def trip_build(req: TripReq):
    """Build a corridor forecast covering a long trip, on major roads."""
    need_ready()
    a, b = req.from_, req.to
    km = S.area.dist_m(a, b) / 1000
    if km > 160:
        return {"building": False, "error": f"That trip is {km:.0f} km. Corridor forecasts cover up to about 160 km."}
    if S.building:
        return {"building": True}
    threading.Thread(target=_build_trip, args=(req, km), daemon=True).start()
    return {"building": True, "km": round(km)}


def _build_trip(req, km):
    import importlib.util
    import math
    S.building = True
    prev = S.area.area_id
    try:
        (s, n), (w, e) = sorted([req.from_[0], req.to[0]]), sorted([req.from_[1], req.to[1]])
        pad_lat = max(0.03, (n - s) * 0.18)
        pad_lon = max(0.03, (e - w) * 0.18)
        bbox = [s - pad_lat, w - pad_lon, n + pad_lat, e + pad_lon]
        lat0 = (bbox[0] + bbox[2]) / 2
        area_m2 = (bbox[2] - bbox[0]) * 110_574 * (bbox[3] - bbox[1]) * 111_320 * math.cos(math.radians(lat0))
        cell = max(20.0, round(math.sqrt(area_m2 / 220_000) / 10) * 10)
        major = km > 8
        S.say(f"Trip of about {km:.0f} km: fetching {'major roads' if major else 'streets'} and terrain at {cell:.0f} m...")
        spec = importlib.util.spec_from_file_location("fetch_area", Path(__file__).resolve().parents[1] / "scripts" / "fetch_area.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        persona = {"name": "You", "mode": "car", "depart_min": 0, "depart_label": "Now",
                   "from": list(req.from_), "from_label": req.from_label,
                   "to": list(req.to), "to_label": req.to_label}
        mod.build("trip", bbox, f"{req.from_label} to {req.to_label}", "", cell_m=cell, major_only=major, persona=persona)
        S.say("Running the flood model along the corridor...")
        S.ready = False
        S.boot("trip")
        (DATA_DIR / "ACTIVE").write_text("trip")
    except BaseException as ex:  # noqa: BLE001
        S.say(f"Couldn't build the trip forecast: {ex}")
        if not S.ready:
            try:
                S.boot(prev)
            except Exception as e2:  # noqa: BLE001
                S.say(f"Reload failed: {e2}")
    finally:
        S.building = False


@app.post("/api/locate/reset")
def locate_reset():
    """Go back to the default area (Velachery or the sample)."""
    need_ready()
    (DATA_DIR / "ACTIVE").unlink(missing_ok=True)
    S.ready = False
    threading.Thread(target=S.boot, daemon=True).start()
    return {"ok": True}


@app.get("/api/area")
def area():
    need_ready()
    a = S.area
    names, locs = [], list(S.model.loc_names)
    name_idx = {}
    segs = []
    for s in a.segments:
        if s.name not in name_idx:
            name_idx[s.name] = len(names)
            names.append(s.name)
        flat = [round(v, 6) for c in s.coords for v in c]
        flags = (1 if s.bridge else 0) | (2 if s.underpass else 0) | (4 if s.construction else 0)
        segs.append([ROAD_CLASS.get(s.highway, 2), name_idx[s.name],
                     locs.index(s.locality) if s.locality in locs else 0, flat, flags])
    return {"area_id": a.area_id, "corridor": a.meta.get("corridor", False), "cell_m": round(a.cell_m),
            "name": a.meta["name"], "city": a.meta.get("city"), "synthetic": a.meta.get("synthetic", False),
            "bbox": a.bbox, "names": names, "localities": a.meta.get("localities") or [], "segments": segs,
            "water": a.meta.get("water") or [], "grid": list(a.dem.shape),
            "users": {k: {kk: v for kk, v in u.items()} for k, u in USERS.items()},
            "now": list(S.now), "limits": LIMIT_CM, "mode": S.rain_source}


@app.get("/api/forecast")
def forecast():
    need_ready()
    sim = S.sim
    return {"run_id": sim.run_id, "version": sim.version, "label": sim.label, "times": sim.times,
            "total_mm": sim.total_mm, "rain_mean": sim.rain_mean,
            "seg_depth": b64(sim.seg_depth), "nseg": int(sim.seg_depth.shape[1]),
            "raster": b64(sim.raster), "raster_shape": list(sim.raster.shape[1:]),
            "rain": b64(sim.rain_coarse), "rain_shape": list(sim.rain_coarse.shape[1:]),
            "clock": [clock(S.now, t) for t in sim.times], "now": list(S.now), "mode": S.rain_source.split(":")[0],
            "context": S.context, "replay": getattr(S, "replay_info", None) if S.rain_source.startswith("replay:") else None,
            "observations": getattr(sim, "observations", 0)}


class RouteReq(BaseModel):
    user: str = "priya"
    mode: str = "two_wheeler"
    depart_min: int = 110
    from_: list[float] | None = Field(default=None, alias="from")
    to: list[float] | None = None
    from_label: str | None = None
    to_label: str | None = None
    model_config = {"populate_by_name": True}


@app.get("/api/commute")
def commute(user: str = "priya"):
    need_ready()
    d = S.policy.check(user, "viewCommute", "Commute", "priya")
    if not d["allowed"]:
        return deny(d)
    return {"persona": S.persona, "decision": d, "live": S.rain_source == "live"}


class CommuteReq(BaseModel):
    user: str = "priya"
    from_: list[float] = Field(alias="from")
    to: list[float]
    from_label: str = "Work"
    to_label: str = "Home"
    leave: str = "18:00"
    mode: str = "two_wheeler"
    model_config = {"populate_by_name": True}


@app.post("/api/commute")
def save_commute(req: CommuteReq):
    need_ready()
    d = S.policy.check(req.user, "editCommute", "Commute", req.user)
    if not d["allowed"]:
        return deny(d)
    import re
    if not re.fullmatch(r"\d{1,2}:\d{2}", req.leave):
        raise HTTPException(400, "leave must be HH:MM")
    allc = load_commutes()
    allc[req.user] = {"name": "You", "from": req.from_, "to": req.to, "from_label": req.from_label,
                      "to_label": req.to_label, "leave": req.leave, "mode": req.mode}
    COMMUTES.write_text(json.dumps(allc, indent=1))
    S.advice_cache = {}
    return {"persona": S.persona, "decision": d}


class ReportReq(BaseModel):
    user: str = "priya"
    lat: float
    lon: float
    depth_cm: int
    street: str | None = None


@app.post("/api/report")
def report(req: ReportReq):
    """A resident reports water (or a dry road) where they are."""
    need_ready()
    d = S.policy.check(req.user, "submitReport", "Layer", "reports")
    if not d["allowed"]:
        return deny(d)
    s_, w, n, e = S.area.bbox
    if not (s_ - 0.01 <= req.lat <= n + 0.01 and w - 0.01 <= req.lon <= e + 0.01):
        raise HTTPException(400, "That location is outside the forecast area")
    closed_report = req.depth_cm < 0
    depth = 0 if closed_report else int(max(0, min(150, req.depth_cm)))
    seg = None
    if len(S.seg_mid):
        p = np.array([req.lat * 110_574, req.lon * S.area.m_per_deg_lon])
        seg = int(np.argmin(np.hypot(*(S.seg_mid - p).T)))
    sg = S.area.segments[seg] if seg is not None else None
    words = {0: "Road is dry", 10: "Ankle-deep water", 25: "Water up to the shin", 45: "Knee-deep water", 90: "Waist-deep water"}
    label = "Road closed" if closed_report else words.get(depth, f"About {depth} cm of water")
    r = {"id": f"u{int(time.time() * 1000)}", "ts": time.time(), "text": f"{label} on {sg.name if sg else 'this street'}",
         "street": sg.name if sg else (req.street or ""), "locality": sg.locality if sg else "",
         "date": now_local().strftime("%Y-%m-%d"), "time": clock(S.now, 0), "depth_cm_reported": depth,
         "lat": req.lat, "lon": req.lon, "seg": seg, "source": "resident", "kind": "closed" if closed_report else "water"}
    S.resident.append(r)
    S.resident = S.resident[-2000:]
    REPORTS.write_text(json.dumps(S.resident))
    if closed_report:
        S.apply_closures()
        S.advice_cache = {}
        bus.publish({"type": "closures"})
        return {"ok": True, "report": r, "decision": d}
    S.store.add_report(r)
    with S.lock:
        S.sim = assimilate(S.sim_raw, S.resident, S)
        S.advice_cache = {}
    threading.Thread(target=S.store.index_forecast, args=(S.area, S.sim), daemon=True).start()
    bus.publish({"type": "report", "report": {k: r[k] for k in ("id", "lat", "lon", "depth_cm_reported", "street", "time")}})
    bus.publish({"type": "forecast", "run_id": S.sim.run_id, "label": S.sim.label})
    return {"ok": True, "report": r, "decision": d}


@app.get("/api/closures")
def closures():
    need_ready()
    return {"closures": S.closures, "tomtom": {"enabled": bool(traffic.key()), "error": getattr(S, "tt_error", None)},
            "replays": {k: v["name"] for k, v in chennai.REPLAYS.items()}}


@app.get("/api/reports")
def recent_reports(hours: float = 6):
    need_ready()
    cut = time.time() - hours * 3600
    rows = [r for r in S.resident if r.get("ts", 0) >= cut]
    return {"reports": [{k: r.get(k) for k in ("id", "lat", "lon", "depth_cm_reported", "street", "time", "ts", "kind")} for r in rows[-300:]],
            "now": time.time()}


class RainReq(BaseModel):
    source: str


@app.post("/api/rain")
def set_rain(req: RainReq):
    """Switch between live rain and the scripted demo storm."""
    need_ready()
    if not (req.source in ("live", "demo") or (req.source.startswith("replay:") and req.source[7:] in chennai.REPLAYS)):
        raise HTTPException(400, "source must be live, demo or replay:<event>")
    S.rain_source = req.source
    S.version = 0
    S.setup_mode()
    S.run()
    bus.publish({"type": "forecast", "run_id": S.sim.run_id, "label": S.sim.label, "mode": req.source})
    return {"source": S.rain_source, "label": S.sim.label}


@app.post("/api/route")
def route(req: RouteReq):
    need_ready()
    d = S.policy.check(req.user, "checkRoute", "Layer", "routes")
    if not d["allowed"]:
        return deny(d)
    if req.mode not in LIMIT_CM:
        raise HTTPException(400, "mode must be two_wheeler, car or walk")
    trip = S.trip(req)
    if trip is None:
        return JSONResponse(status_code=409, content={"error": "no_commute", "message": "Set your commute first"})
    p = S.plan(req.mode, req.depart_min, trip)
    p["trip"] = {k: trip[k] for k in ("from", "to", "from_label", "to_label")}
    worst = p["usual"]["worst"]
    if worst:
        worst["clock"] = clock(S.now, worst["t"])
        worst["timeline"] = S.sim.seg_depth[:, worst["seg"]].tolist()
    p["depart_clock"] = clock(S.now, req.depart_min)
    p["run_id"] = S.sim.run_id
    return p


@app.post("/api/advise")
def advise(req: RouteReq):
    need_ready()
    d = S.policy.check(req.user, "checkRoute", "Layer", "routes")
    if not d["allowed"]:
        return deny(d)
    trip = S.trip(req)
    if trip is None:
        return JSONResponse(status_code=409, content={"error": "no_commute"})
    return S.advise(req.mode, req.depart_min, trip)


@app.get("/api/nearby")
def nearby(lat: float, lon: float, t: int = 0):
    need_ready()
    t = int(round(t / 10) * 10)
    rows = S.store.nearby(t, lat, lon)
    return {"t": t, "clock": clock(S.now, t), "streets": rows}


@app.get("/api/search")
def search(q: str = ""):
    need_ready()
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": S.store.search_places(q.strip(), 8)}


@app.get("/api/segment/{idx}")
def segment(idx: int):
    need_ready()
    if not 0 <= idx < len(S.area.segments):
        raise HTTPException(404, "No such street segment")
    s = S.area.segments[idx]
    lat, lon = s.mid
    reports = S.store.search_reports(s.name, lat, lon, 400, 4)
    closures_here = [c for c in S.closures if idx in c["segs"]]
    return {"name": s.name, "locality": s.locality, "highway": s.highway, "length_m": round(s.length_m),
            "lat": lat, "lon": lon, "underpass": s.underpass, "bridge": s.bridge, "construction": s.construction,
            "closures": closures_here,
            "timeline": S.sim.seg_depth[:, idx].tolist(), "reports": reports}


@app.get("/api/ops")
def ops(user: str, t: int = 110):
    need_ready()
    t = int(round(t / 10) * 10)
    summary = {r["locality"]: r for r in S.store.locality_summary(t)}
    allowed, denied, decisions = [], [], []
    for L in S.model.loc_names:
        d = S.policy.check(user, "viewOps", "Locality", L)
        decisions.append(d)
        if d["allowed"]:
            row = summary.get(L, {"locality": L, "segments_wet": 0, "impassable": 0, "max_depth": 0, "worst": None})
            row["drain_mmph"] = round(float(S.model.drain_by_loc[S.model.loc_names.index(L)]), 1)
            seen, uniq = set(), []
            for f in S.store.flooded_segments(t, 30, L, 20):  # deepest first; one row per street name
                if f["name"] not in seen:
                    seen.add(f["name"])
                    uniq.append(f)
            row["flooded"] = uniq[:5]
            allowed.append(row)
        else:
            denied.append(L)
    if not allowed:
        return deny(decisions[0])
    allowed.sort(key=lambda r: (-r["impassable"], -r["max_depth"]))
    return {"t": t, "clock": clock(S.now, t), "localities": allowed, "denied": denied,
            "decision": next(d for d in decisions if d["allowed"])}


@app.get("/api/vulnerable")
def vulnerable(user: str, t: int = 110):
    need_ready()
    d = S.policy.check(user, "viewVulnerableSites", "Layer", "vulnerable_sites")
    if not d["allowed"]:
        return deny(d)
    t = int(round(t / 10) * 10)
    return {"t": t, "clock": clock(S.now, t), "sites": S.store.pois_at_risk(t), "decision": d}


@app.get("/api/phone")
def phone():
    """Addresses to open the app on a phone, with QR codes."""
    import io
    import httpx
    import qrcode
    import qrcode.image.svg
    links = []
    try:  # the cloudflared quick tunnel reports its public hostname on its metrics port
        r = httpx.get("http://tunnel:2000/quicktunnel", timeout=1.5)
        host = r.json().get("hostname")
        if host:
            links.append({"kind": "https", "url": f"https://{host}", "label": "Phone link (HTTPS, everything works)"})
    except Exception:  # noqa: BLE001
        pass
    lan = os.getenv("PHONE_LAN_URL", "").strip()
    if lan:
        links.append({"kind": "lan", "url": lan, "label": "Same Wi-Fi (no location, offline or install)"})
    for L in links:
        buf = io.BytesIO()
        qrcode.make(L["url"], image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2).save(buf)
        L["qr"] = buf.getvalue().decode()
    return {"links": links}


@app.get("/api/status")
def status():
    st = {"ready": S.ready, "boot": S.boot_log[-8:]}
    if S.ready:
        st |= {
            "area": {"name": S.area.meta["name"], "synthetic": S.area.meta.get("synthetic", False),
                     "segments": len(S.area.segments)},
            "run": {"id": S.sim.run_id, "version": S.sim.version, "label": S.sim.label, "total_mm": S.sim.total_mm,
                    "rain_source": S.rain_source, "live_error": getattr(S, "live_error", None),
                    "points": len(S.live["points"]) if getattr(S, "live", None) else 0,
                    "observations": getattr(S.sim, "observations", 0), "resident_reports": len(S.resident),
                    "refresh_min": float(os.getenv("LIVE_REFRESH_MIN", "15")), "context": S.context,
                    "tomtom": {"enabled": bool(traffic.key()), "error": getattr(S, "tt_error", None),
                               "closed_segments": len(getattr(S, "tt_closed", []))},
                    "documented": len(S.documented), "replays": {k: v["name"] for k, v in chennai.REPLAYS.items()}},
            "store": {"engine": S.store.engine, "warning": S.store_warning,
                      "url": getattr(S.store, "url", None)},
            "agent": model_status(),
            "policy": {"engine": "cedar", "policies": S.policy.ids},
            "alerts": {"localstack": listener.connected, "queue": alerts.QUEUE_NAME,
                       "error": listener.error, "endpoint": alerts.LOCALSTACK_URL},
        }
    st["queries"] = list(QUERY_LOG)[:15]
    st["decisions"] = list(DECISION_LOG)[:15]
    st["pipeline"] = list(alerts.PIPELINE_LOG)[:15]
    return st


@app.get("/api/events")
async def events(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------- internal (poller)
class TickReq(BaseModel):
    source: str = "manual"
    deliver: str = "return"  # 'return' (caller publishes to SNS) or 'direct' (push to browsers now)


@app.post("/api/internal/tick")
def tick(req: TickReq):
    need_ready()
    alerts.log("Nowcast update requested", f"by {req.source}")
    if S.rain_source == "demo":
        S.version = min(S.version + 1, 1)
    S.run()  # live mode: fetches the latest Open-Meteo forecast
    bus.publish({"type": "forecast", "run_id": S.sim.run_id, "label": S.sim.label})
    out = check_commute_alerts(req.source)
    alerts.log("Forecast re-run", f"{S.sim.label}; {len(out)} commute alert(s)")
    if req.deliver == "direct":
        for a in out:
            alerts.log("Delivered in-process", a["headline"])
            bus.publish({"type": "alert", "via": "in-process (LocalStack not used)", **a})
    return {"run_id": S.sim.run_id, "label": S.sim.label, "alerts": out}


@app.post("/api/internal/reset")
def reset():
    need_ready()
    S.version = 0
    S.run()
    alerts.log("Reset", "back to 4:10 PM nowcast")
    bus.publish({"type": "forecast", "run_id": S.sim.run_id, "label": S.sim.label})
    return {"run_id": S.sim.run_id}
