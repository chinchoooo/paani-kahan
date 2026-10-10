"""Paani Kahan API server: a thin HTTP layer over the World (app/world).

Run:  uvicorn app.server:app --port 8080
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alerts
from .agent import Advisor, clock, model_status
from .engine import chennai, traffic
from .engine.area import DATA_DIR
from .engine.rain import now_local
from .engine.routing import LIMIT_CM
from .policy import DECISION_LOG, USERS, Policy
from .store import QUERY_LOG, StoreManager
from .world import net
from .world.scheduler import ROUTE, VISIBLE
from .world.tiles import DETAIL, REGION, TileKey, bbox_around, in_bbox, km_between, tile_of, tiles_in_bbox
from .world.world import FRAMES, GEOM_V, Preparing, World

WEB = Path(__file__).parent / "web"
DEFAULT_ORIGIN = tuple(float(v) for v in os.getenv("DEFAULT_ORIGIN", "12.979,80.223").split(","))
COMMUTES = DATA_DIR / "commutes.json"
REPORTS = DATA_DIR / "reports.json"
BOOT_LOG: list = []


def say(msg):
    print(f"[paani] {msg}", flush=True)
    BOOT_LOG.append(f"{time.strftime('%H:%M:%S')} {msg}")
    del BOOT_LOG[:-40]


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return default


# ------------------------------------------------------------------ state
bus = alerts.Bus()
listener = alerts.SqsListener(bus)
W = World(DATA_DIR, publish=bus.publish, log=say)
W.resident = load_json(REPORTS, [])
STORE = StoreManager(W, os.getenv("OPENSEARCH_URL", "http://localhost:9200"), float(os.getenv("OPENSEARCH_WAIT_S", "180")))
POLICY = Policy()
ADVICE: dict = {}
STATE = {"last_refresh": 0.0, "last_alert_run": None, "started": time.time()}


def origin():
    return W.focus or DEFAULT_ORIGIN


def persona():
    """The demo commute (demo mode) or your saved commute, with depart_min from the clock."""
    run = W.rain.run
    if run.kind == "demo":
        base = dict(W.demo_persona) if W.demo_persona else None
    else:
        base = load_json(COMMUTES, {}).get("priya")
        base = dict(base) if base else None
    if not base:
        return None
    now = run.now_hm
    if "leave" in base:
        hh, mm = map(int, base["leave"].split(":"))
        mins = (hh * 60 + mm) - (now[0] * 60 + now[1])
        if mins < -30:
            mins += 24 * 60
        base["depart_min"] = max(0, mins)
        base["depart_label"] = clock(now, mins)
        base["in_window"] = mins <= 340
    else:
        base["in_window"] = True
    return base


def ops_area():
    """Where ward operations look: the street-level tiles within 5 km of you."""
    lat, lon = origin()
    return bbox_around(lat, lon, 5)


def refresh_policy():
    bb = ops_area()
    locs, best = [], None
    for k in tiles_in_bbox(bb, DETAIL.z):
        t = W.tiles.get(str(k))
        if t and t.status == "ready":
            for L in t.area.meta["localities"]:
                if in_bbox(L["lat"], L["lon"], bb) and L["name"] not in locs:
                    locs.append(L["name"])
    if locs:
        o = origin()
        allL = [L for k in tiles_in_bbox(bb, DETAIL.z) if (t := W.tiles.get(str(k))) and t.status == "ready" for L in t.area.meta["localities"] if L["name"] in locs]
        best = min(allL, key=lambda L: (L["lat"] - o[0]) ** 2 + (L["lon"] - o[1]) ** 2)["name"]
    if locs != POLICY.localities:
        POLICY.refresh(locs, best)


def deny(decision):
    return JSONResponse(status_code=403, content={"denied": True, "decision": decision})


def preparing(e: Preparing):
    return JSONResponse(status_code=202, content={"preparing": True, "ready": e.ready, "total": e.total, "what": e.what,
                                                  "message": f"Loading {e.what} for this route · {e.ready}/{e.total}"})


# ----------------------------------------------------------------- background
def check_commute_alerts(wait=False):
    p = persona()
    if not p or not p.get("in_window", True):
        return []
    mode = p.get("mode", "two_wheeler")
    if wait:
        _, _, detail, region = W.corridor(p["from"], p["to"])
        for k in detail + region:
            W.want(k, ROUTE)
        W.wait_for(detail + region, W.rain.run.id, timeout=75)
    try:
        plan, router, sim = W.plan(p["from"], p["to"], mode, p["depart_min"])
    except Preparing:
        return []
    if plan["status"] not in ("alert", "watch"):
        return []
    adv = advise_for(p, mode, p["depart_min"], router, sim)
    return [{"user": "priya", "status": plan["status"], "headline": adv["headline"], "advice": adv["advice"],
             "depart_min": p["depart_min"], "mode": mode, "run_id": W.rain.run.id, "engine": adv["engine"]}]


def advise_for(trip, mode, depart, router=None, sim=None):
    key = (W.rain.run.id, mode, depart, tuple(trip["from"]), tuple(trip["to"]), len(W.resident))
    if key not in ADVICE:
        if router is None:
            _, router, sim = W.plan(trip["from"], trip["to"], mode, depart)
        st = STORE.store
        ADVICE[key] = Advisor({"router": router, "sim": sim, "store": st, "persona": trip, "mode": mode,
                               "depart_min": min(depart, 340), "now": W.rain.run.now_hm}).run()
        while len(ADVICE) > 40:
            ADVICE.pop(next(iter(ADVICE)))
    return ADVICE[key]


def refresher():
    """Live: new rain every LIVE_REFRESH_MIN; closures; commute alerts when the route floods."""
    every = float(os.getenv("LIVE_REFRESH_MIN", "15")) * 60
    while True:
        time.sleep(20)
        try:
            if W.rain.source == "live" and time.time() - STATE["last_refresh"] > every:
                STATE["last_refresh"] = time.time()
                if not W.rain.refresh(W.focus):
                    continue
                W.new_run(W.rain.run)
                alerts.log("Scheduled refresh", W.rain.run.label)
                bus.publish({"type": "run", "run_id": W.rain.run.id, "label": W.rain.run.label})
                W.refresh_closures()
                for a in check_commute_alerts(wait=True):
                    if a["status"] == "alert" and STATE["last_alert_run"] != a["run_id"]:
                        STATE["last_alert_run"] = a["run_id"]
                        bus.publish({"type": "alert", "via": "scheduled refresh (Open-Meteo)", **a})
            elif W.rain.source != "live" and time.time() - STATE["last_refresh"] > 300:
                STATE["last_refresh"] = time.time()
                W.refresh_closures()
        except Exception as e:  # noqa: BLE001
            say(f"Refresh failed: {type(e).__name__}: {e}")


def start_source(source, anchor=None):
    run = W.rain.set_source(source, anchor or origin())
    W.new_run(run)
    if run.kind == "demo":
        W.setup_demo(anchor or origin())
    else:
        W.demo_state = "off"
    ADVICE.clear()
    return run


app = FastAPI(title="Paani Kahan")
app.add_middleware(GZipMiddleware, minimum_size=1500)


@app.on_event("startup")
async def _startup():
    bus.loop = asyncio.get_running_loop()

    def boot():
        src = os.getenv("RAIN_SOURCE", "live")
        if not (src in ("live", "demo") or (src.startswith("replay:") and src[7:] in chennai.REPLAYS)):
            src = "live"
        say(f"Data: {'synthetic streets (offline)' if W.synthetic else 'OpenStreetMap + AWS Terrain Tiles'} · rain: {src}")
        W.set_focus(*DEFAULT_ORIGIN)
        try:
            start_source(src, DEFAULT_ORIGIN)
        except Exception as e:  # noqa: BLE001
            say(f"Rain source {src} unavailable ({e}); using live")
            start_source("live", DEFAULT_ORIGIN)
        STATE["last_refresh"] = time.time()
        if os.getenv("ALERTS", "localstack") == "localstack":
            listener.start()
        threading.Thread(target=refresher, daemon=True).start()
        say("Ready: streets load around you as you look")
    threading.Thread(target=boot, daemon=True).start()


# ------------------------------------------------------------------ static
def asset_version():
    return str(max(int(f.stat().st_mtime) for f in WEB.rglob("*") if f.is_file()))


@app.get("/")
def index():
    v = asset_version()
    return HTMLResponse((WEB / "index.html").read_text().replace("__V__", v), headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    js = (WEB / "sw.js").read_text().replace("__V__", asset_version())
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=WEB), name="static")


# ------------------------------------------------------------------ world
# Raster base maps that need no API key. OpenStreetMap's own tiles are shown in black and
# white by the client (canvas filters), so the map stays bold and minimal in both themes.
BW = {"light": "grayscale(1) contrast(1.05) brightness(1.06)", "dark": "invert(1) grayscale(1) brightness(2.4) contrast(0.95)"}
BASEMAPS = {
    "osm": {"light": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "dark": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "subdomains": "", "attribution": "© OpenStreetMap contributors", "maxZoom": 19, "filter": BW},
    "osm-color": {"light": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "dark": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                  "subdomains": "", "attribution": "© OpenStreetMap contributors", "maxZoom": 19, "filter": None},
    # needs a CARTO key since 2026: BASEMAP=carto and CARTO_API_KEY=...
    "carto": {"light": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?api_key=" + os.getenv("CARTO_API_KEY", ""),
              "dark": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?api_key=" + os.getenv("CARTO_API_KEY", ""),
              "subdomains": "abcd", "attribution": "© OpenStreetMap contributors © CARTO", "maxZoom": 19, "filter": None},
}


def basemap():
    v = os.getenv("BASEMAP", "osm").strip()
    if v == "none":
        return None
    if v.startswith("http"):  # a custom XYZ template
        return {"light": v, "dark": v, "subdomains": "", "attribution": os.getenv("BASEMAP_ATTRIBUTION", "© OpenStreetMap contributors"), "maxZoom": 19,
                "filter": BW if os.getenv("BASEMAP_BW", "1") == "1" else None}
    return BASEMAPS.get(v, BASEMAPS["osm"])


@app.get("/api/world")
def world_info():
    run = W.rain.run
    return {"detail_z": DETAIL.z, "region_z": REGION.z, "detail_km": float(os.getenv("DETAIL_KM", "10")),
            "region_km": float(os.getenv("REGION_KM", "100")), "limits": LIMIT_CM, "origin": list(origin()),
            "users": {k: dict(u) for k, u in USERS.items()}, "mode": run.kind, "synthetic": W.synthetic,
            "run": {"id": run.id, "label": run.label, "now": list(run.now_hm), "anchor": run.detail.get("anchor")}, "demo": W.demo_status(),
            "attribution": W.osm.attribution, "frames": len(FRAMES), "basemap": basemap()}


class FocusReq(BaseModel):
    lat: float
    lon: float
    view: list[float] | None = None


@app.post("/api/focus")
def focus(req: FocusReq):
    if not (-80 < req.lat < 80 and -180 <= req.lon <= 180):
        raise HTTPException(400, "bad location")
    W.set_focus(req.lat, req.lon, req.view)
    return {"progress": W.progress(req.lat, req.lon), "run_id": W.rain.run.id}


@app.get("/api/progress")
def progress(lat: float | None = None, lon: float | None = None):
    return {"progress": W.progress(lat, lon), "jobs": W.sched.summary(), "run_id": W.rain.run.id}


def _tile_or_404(z, x, y):
    try:
        return TileKey.parse(f"{z}/{x}/{y}")
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/tile/{z}/{x}/{y}")
def tile_geometry(z: int, x: int, y: int, request: Request):
    key = _tile_or_404(z, x, y)
    t = W.want(key, VISIBLE)
    if t.status == "empty":
        return JSONResponse({"key": str(key), "empty": True}, headers={"Cache-Control": "max-age=86400"})
    if t.status != "ready":
        return JSONResponse(status_code=202, content={"key": str(key), "status": t.status, "error": t.error})
    etag = f'"g{GEOM_V}-{int(t.built_at)}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(t.payload, media_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/api/tile/{z}/{x}/{y}/forecast")
def tile_forecast(z: int, x: int, y: int, request: Request):
    key = _tile_or_404(z, x, y)
    t = W.want(key, VISIBLE)
    if t.status != "ready":
        return JSONResponse(status_code=202, content={"key": str(key), "status": t.status})
    p = W.forecast_payload(t)
    if p is None:
        return JSONResponse(status_code=202, content={"key": str(key), "status": "forecasting"})
    etag = f'"{p["run_id"]}-{p["observations"]}-{len(W.resident)}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(p, headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/api/tile/{z}/{x}/{y}/terrain.png")
def tile_terrain(z: int, x: int, y: int):
    key = _tile_or_404(z, x, y)
    t = W.tiles.get(str(key))
    if not t or t.status != "ready" or not t.terrain_png:
        raise HTTPException(404, "not ready")
    return Response(t.terrain_png, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/summary")
def summary(lat: float | None = None, lon: float | None = None):
    """The 6-hour picture where you are: rain, context, how rare it is, loading progress."""
    lat, lon = (lat, lon) if lat is not None else origin()
    run = W.rain.run
    rain = W.rain.series_at(lat, lon, FRAMES, run)
    t = W.tiles.get(str(tile_of(lat, lon, DETAIL.z)))
    fc, stale = t.latest_fc(run.id) if t else (None, True)
    ctx = dict(getattr(fc, "context", {}) or {}) if fc is not None else {}
    rm = np.array(rain)
    peak_h = float(np.convolve(rm, np.ones(6) / 6, mode="valid").max()) if len(rm) >= 6 else float(rm.max())
    clim = W.ctx.nearest_climate(lat, lon)
    rp = chennai.return_period(peak_h, clim["hourly"]["u"], clim["hourly"]["alpha"]) if clim else None
    ctx.update({"peak_hourly_mm": round(peak_h, 1), "return_years": round(rp, 1) if rp and peak_h > 5 else None})
    return {"run_id": run.id, "label": run.label, "mode": run.kind, "now": list(run.now_hm), "times": FRAMES,
            "clock": [clock(run.now_hm, tm) for tm in FRAMES], "rain_mean": rain, "total_mm": round(float(rm.sum()) / 6, 1),
            "context": ctx, "replay": (W.rain.replay or {}) if run.kind == "replay" else None,
            "progress": W.progress(lat, lon), "stale": stale, "demo": W.demo_status(), "rain_error": W.rain.error}


# ------------------------------------------------------------------ commute + routes
@app.get("/api/commute")
def commute(user: str = "priya"):
    d = POLICY.check(user, "viewCommute", "Commute", "priya")
    if not d["allowed"]:
        return deny(d)
    return {"persona": persona(), "decision": d, "live": W.rain.run.kind == "live", "demo": W.demo_status()}


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
    d = POLICY.check(req.user, "editCommute", "Commute", req.user)
    if not d["allowed"]:
        return deny(d)
    if not re.fullmatch(r"\d{1,2}:\d{2}", req.leave):
        raise HTTPException(400, "leave must be HH:MM")
    allc = load_json(COMMUTES, {})
    allc[req.user] = {"name": "You", "from": req.from_, "to": req.to, "from_label": req.from_label,
                      "to_label": req.to_label, "leave": req.leave, "mode": req.mode}
    COMMUTES.parent.mkdir(parents=True, exist_ok=True)
    COMMUTES.write_text(json.dumps(allc, indent=1))
    _, _, detail, region = W.corridor(req.from_, req.to)
    for k in detail + region:
        W.want(k, ROUTE)
    return {"persona": persona(), "decision": d}


class RouteReq(BaseModel):
    user: str = "priya"
    mode: str = "two_wheeler"
    depart_min: int = 110
    from_: list[float] | None = Field(default=None, alias="from")
    to: list[float] | None = None
    from_label: str | None = None
    to_label: str | None = None
    model_config = {"populate_by_name": True}


def trip_of(req: RouteReq):
    if req.from_ is None or req.to is None:
        return persona()
    return {"name": "You", "from": list(req.from_), "to": list(req.to),
            "from_label": req.from_label or "Start", "to_label": req.to_label or "Destination"}


@app.post("/api/route")
def route(req: RouteReq):
    d = POLICY.check(req.user, "checkRoute", "Layer", "routes")
    if not d["allowed"]:
        return deny(d)
    if req.mode not in LIMIT_CM:
        raise HTTPException(400, "mode must be two_wheeler, car or walk")
    trip = trip_of(req)
    if trip is None:
        return JSONResponse(status_code=409, content={"error": "no_commute", "message": "Set your commute first", "demo": W.demo_status()})
    if km_between(trip["from"], trip["to"]) > 180:
        return JSONResponse(status_code=400, content={"error": "too_far", "message": "Routes are checked up to about 180 km"})
    try:
        p, router, sim = W.plan(trip["from"], trip["to"], req.mode, req.depart_min)
    except Preparing as e:
        return preparing(e)
    run = W.rain.run
    p["trip"] = {k: trip[k] for k in ("from", "to", "from_label", "to_label")}
    worst = p["usual"]["worst"]
    if worst:
        worst["clock"] = clock(run.now_hm, worst["t"])
    p["depart_clock"] = clock(run.now_hm, req.depart_min)
    p["run_id"] = run.id
    return p


@app.post("/api/advise")
def advise(req: RouteReq):
    d = POLICY.check(req.user, "checkRoute", "Layer", "routes")
    if not d["allowed"]:
        return deny(d)
    trip = trip_of(req)
    if trip is None:
        return JSONResponse(status_code=409, content={"error": "no_commute"})
    try:
        return advise_for(trip, req.mode, req.depart_min)
    except Preparing as e:
        return preparing(e)


# ------------------------------------------------------------------ places + streets
@app.get("/api/segment/{gid:path}")
def segment(gid: str):
    t, s = W.segment(gid)
    if s is None:
        raise HTTPException(404, "No such street segment (or its tile isn't loaded)")
    lat, lon = s.mid
    fc, stale = t.latest_fc(W.rain.run.id)
    return {"gid": gid, "name": s.name, "locality": s.locality, "highway": s.highway, "length_m": round(s.length_m),
            "lat": lat, "lon": lon, "underpass": s.underpass, "bridge": s.bridge, "construction": s.construction,
            "lod": t.key.lod.name, "closures": [c for c in W.closure_records if gid in c["segs"]],
            "timeline": fc.seg_depth[:, s.idx].tolist() if fc is not None else [], "stale": stale,
            "reports": STORE.store.search_reports(s.name, lat, lon, 400, 4)}


@app.get("/api/nearby")
def nearby(lat: float, lon: float, t: int = 0):
    t = int(round(t / 10) * 10)
    return {"t": t, "clock": clock(W.rain.run.now_hm, t), "streets": STORE.store.nearby(t, lat, lon)}


@app.get("/api/search")
def search(q: str = "", lat: float | None = None, lon: float | None = None):
    if len(q.strip()) < 2:
        return {"results": []}
    near = (lat, lon) if lat is not None else origin()
    return {"results": STORE.store.search_places(q.strip(), near, 8)}


@app.get("/api/geocode")
def geocode(q: str, lat: float | None = None, lon: float | None = None):
    """Places anywhere in India (OpenStreetMap Nominatim), biased towards you."""
    if len(q.strip()) < 3:
        return {"results": []}
    try:
        params = {"q": q, "format": "jsonv2", "countrycodes": "in", "limit": 6}
        if lat is not None:
            params["viewbox"] = f"{lon - 1.2},{lat + 1.2},{lon + 1.2},{lat - 1.2}"
        r = net.NOMINATIM.get(os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"), params=params, retries=0)
        out = []
        for p in r.json():
            parts = p.get("display_name", "").split(", ")
            out.append({"name": p.get("name") or parts[0], "sub": ", ".join(parts[1:3]), "kind": "place",
                        "lat": float(p["lat"]), "lon": float(p["lon"]), "seg": None, "far": True})
        return {"results": out}
    except Exception as e:  # noqa: BLE001
        return {"results": [], "error": f"Place search unavailable ({type(e).__name__})"}


# ------------------------------------------------------------------ reports + closures
class ReportReq(BaseModel):
    user: str = "priya"
    lat: float
    lon: float
    depth_cm: int
    street: str | None = None


@app.post("/api/report")
def report(req: ReportReq):
    d = POLICY.check(req.user, "submitReport", "Layer", "reports")
    if not d["allowed"]:
        return deny(d)
    sg = W.nearest_segment(req.lat, req.lon)
    if sg is None:
        raise HTTPException(400, "No streets loaded here yet")
    closed_report = req.depth_cm < 0
    depth = 0 if closed_report else int(max(0, min(150, req.depth_cm)))
    words = {0: "Road is dry", 10: "Ankle-deep water", 25: "Water up to the shin", 45: "Knee-deep water", 90: "Waist-deep water"}
    label = "Road closed" if closed_report else words.get(depth, f"About {depth} cm of water")
    r = {"id": f"u{int(time.time() * 1000)}", "ts": time.time(), "text": f"{label} on {sg.name}",
         "street": sg.name, "locality": sg.locality, "date": now_local().strftime("%Y-%m-%d"), "time": clock(W.rain.run.now_hm, 0),
         "depth_cm_reported": depth, "lat": req.lat, "lon": req.lon, "seg": sg.gid, "source": "resident",
         "kind": "closed" if closed_report else "water"}
    W.resident.append(r)
    W.resident = W.resident[-2000:]
    REPORTS.parent.mkdir(parents=True, exist_ok=True)
    REPORTS.write_text(json.dumps(W.resident))
    ADVICE.clear()
    if closed_report:
        W.refresh_closures()
        with W.lock:
            W._routers.clear()
        bus.publish({"type": "closures"})
        return {"ok": True, "report": r, "decision": d}
    STORE.store.add_report(r)
    W.reassimilate_near(req.lat, req.lon)
    bus.publish({"type": "report", "report": {k: r[k] for k in ("id", "lat", "lon", "depth_cm_reported", "street", "time", "kind")}})
    return {"ok": True, "report": r, "decision": d}


@app.get("/api/reports")
def recent_reports(hours: float = 6):
    cut = time.time() - hours * 3600
    rows = [r for r in W.resident if r.get("ts", 0) >= cut]
    return {"reports": [{k: r.get(k) for k in ("id", "lat", "lon", "depth_cm_reported", "street", "time", "ts", "kind")} for r in rows[-300:]],
            "now": time.time()}


@app.get("/api/closures")
def closures():
    return {"closures": W.closure_records[:400], "tomtom": {"enabled": bool(traffic.key()), "error": W.tt_error},
            "replays": {k: v["name"] for k, v in chennai.REPLAYS.items()}}


class RainReq(BaseModel):
    source: str
    lat: float | None = None
    lon: float | None = None


@app.post("/api/rain")
def set_rain(req: RainReq):
    """Live rain, the elevated demo storm (centred where you are), or a past storm replay."""
    if not (req.source in ("live", "demo") or (req.source.startswith("replay:") and req.source[7:] in chennai.REPLAYS)):
        raise HTTPException(400, "source must be live, demo or replay:<event>")
    anchor = (req.lat, req.lon) if req.lat is not None else origin()
    try:
        run = start_source(req.source, anchor)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": f"{req.source} unavailable: {type(e).__name__}: {e}"[:200]})
    STATE["last_refresh"] = time.time()
    bus.publish({"type": "run", "run_id": run.id, "label": run.label, "mode": run.kind})
    return {"source": W.rain.source, "label": run.label, "run_id": run.id}


# ------------------------------------------------------------------ operations
@app.get("/api/ops")
def ops(user: str, t: int = 110):
    refresh_policy()
    t = int(round(t / 10) * 10)
    bb = ops_area()
    summary_rows = {r["locality"]: r for r in STORE.store.locality_summary(t, bb)}
    allowed, denied, decisions = [], [], []
    for L in POLICY.localities:
        d = POLICY.check(user, "viewOps", "Locality", L)
        decisions.append(d)
        if d["allowed"]:
            row = summary_rows.get(L, {"locality": L, "segments_wet": 0, "impassable": 0, "max_depth": 0, "worst": None})
            seen, uniq = set(), []
            for f in STORE.store.flooded_segments(t, 30, L, bb, 20):
                if f["name"] not in seen:
                    seen.add(f["name"])
                    uniq.append(f)
            row["flooded"] = uniq[:5]
            allowed.append(row)
        else:
            denied.append(L)
    if not allowed:
        return deny(decisions[0] if decisions else POLICY.check(user, "viewOps", "Locality", "Area"))
    allowed.sort(key=lambda r: (-r["impassable"], -r["max_depth"]))
    return {"t": t, "clock": clock(W.rain.run.now_hm, t), "localities": allowed, "denied": denied,
            "decision": next(d for d in decisions if d["allowed"])}


@app.get("/api/vulnerable")
def vulnerable(user: str, t: int = 110):
    d = POLICY.check(user, "viewVulnerableSites", "Layer", "vulnerable_sites")
    if not d["allowed"]:
        return deny(d)
    t = int(round(t / 10) * 10)
    return {"t": t, "clock": clock(W.rain.run.now_hm, t), "sites": STORE.store.pois_at_risk(t, ops_area()), "decision": d}


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
    run = W.rain.run
    return {
        "ready": True, "boot": BOOT_LOG[-10:],
        "run": {"id": run.id, "label": run.label, "kind": run.kind, "source": W.rain.source, "rain": W.rain.status(),
                "resident_reports": len(W.resident), "documented": sum(len(t.documented) for t in list(W.tiles.values())),
                "refresh_min": float(os.getenv("LIVE_REFRESH_MIN", "15")), "replays": {k: v["name"] for k, v in chennai.REPLAYS.items()},
                "tomtom": {"enabled": bool(traffic.key()), "error": W.tt_error, "closed_segments": len(W.closed)},
                "demo": W.demo_status()},
        "world": {"synthetic": W.synthetic, "progress": W.progress(), "jobs": W.sched.summary(), "recent": W.sched.recent[:12],
                  "upstreams": [u.stats() for u in net.all_upstreams()], "net": list(net.NET_LOG)[:12],
                  "tiles_loaded": sum(1 for t in list(W.tiles.values()) if t.status == "ready"),
                  "segments": sum(len(t.area.segments) for t in list(W.tiles.values()) if t.status == "ready")},
        "store": {"engine": STORE.store.engine, "warning": STORE.warning, "url": STORE.url},
        "agent": model_status(),
        "policy": {"engine": "cedar", "policies": POLICY.ids},
        "alerts": {"localstack": listener.connected, "queue": alerts.QUEUE_NAME, "error": listener.error, "endpoint": alerts.LOCALSTACK_URL},
        "queries": list(QUERY_LOG)[:15], "decisions": list(DECISION_LOG)[:15], "pipeline": list(alerts.PIPELINE_LOG)[:15],
    }


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
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------- internal (poller)
class TickReq(BaseModel):
    source: str = "manual"
    deliver: str = "return"  # 'return' (caller publishes to SNS) or 'direct' (push to browsers now)


@app.post("/api/internal/tick")
def tick(req: TickReq):
    """Nowcast update: demo -> the updated (heavier) nowcast; live -> fetch the latest rain now."""
    alerts.log("Nowcast update requested", f"by {req.source}")
    if W.rain.source == "demo":
        run = W.rain.bump_demo()
    else:
        W.rain.refresh(W.focus, force=True)
        run = W.rain.run
        STATE["last_refresh"] = time.time()
    W.new_run(run)
    ADVICE.clear()
    out = check_commute_alerts(wait=True)
    bus.publish({"type": "run", "run_id": run.id, "label": run.label})
    alerts.log("Forecast re-run", f"{run.label}; {len(out)} commute alert(s)")
    if req.deliver == "direct":
        for a in out:
            alerts.log("Delivered in-process", a["headline"])
            bus.publish({"type": "alert", "via": "in-process (LocalStack not used)", **a})
    return {"run_id": run.id, "label": run.label, "alerts": out}


@app.post("/api/internal/reset")
def reset():
    if W.rain.source == "demo":
        run = W.rain.bump_demo(0)
        W.new_run(run)
    ADVICE.clear()
    alerts.log("Reset", "back to the 4:10 PM nowcast")
    bus.publish({"type": "run", "run_id": W.rain.run.id, "label": W.rain.run.label})
    return {"run_id": W.rain.run.id}
