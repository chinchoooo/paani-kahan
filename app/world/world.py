"""The World: every tile, its data, its forecast, and the background work that keeps
them fresh. The API server is a thin layer over this.

Pipeline per tile (each step a keyed, prioritised job on the scheduler):

    fetch (net)  ->  build (cpu)  ->  forecast for run R (cpu)  ->  index (net)
    OSM + terrain    Area + model     rain(R) + context -> depth     OpenSearch

What gets scheduled, and how urgently:
    VISIBLE  tiles the map is drawing right now (the client asked for them)
    ROUTE    tiles a route or the saved commute passes through
    NEAR     street-level tiles within DETAIL_KM of you (default 10 km)
    REGION   main-road tiles within REGION_KM of you (default 100 km)
    IDLE     re-forecasting tiles nobody is looking at

Reads never wait on writes: the newest finished forecast of a tile is served while
the next one computes (stale-while-revalidate), flagged as such.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import threading
import time
from collections import OrderedDict

import numpy as np
from scipy.ndimage import gaussian_filter

from ..engine import chennai, traffic
from ..engine.area import M_PER_DEG_LAT, build_area
from ..engine.hydro import DT_MIN, FloodModel, raster_polys
from ..engine.routing import Router
from .context import ContextService
from .providers import make_providers
from .rainfield import RainService, Run
from .scheduler import IDLE, NEAR, REGION, ROUTE, VISIBLE, Scheduler
from .tiles import DETAIL, REGION as REGION_LOD, TileKey, in_bbox, km_between, tile_of, tiles_in_bbox, tiles_within

DETAIL_KM = float(os.getenv("DETAIL_KM", "10"))
REGION_KM = float(os.getenv("REGION_KM", "100"))
MAX_MODELS = int(os.getenv("MAX_MODELS", "40"))
CLASS = {"motorway": 0, "trunk": 0, "primary": 0, "secondary": 1, "tertiary": 1}
GEOM_V = 3
FRAMES = list(range(0, 361, DT_MIN))


class Preparing(Exception):
    """The tiles this request needs are still loading."""

    def __init__(self, ready, total, what="streets"):
        super().__init__(f"{ready}/{total} {what} ready")
        self.ready, self.total, self.what = ready, total, what


class Tile:
    def __init__(self, key: TileKey):
        self.key = key
        self.status = "new"        # new | fetching | building | ready | empty | failed
        self.error = None
        self.source = None         # network | cache | stale cache | generated
        self.area = None
        self.data = None           # parsed OSM (dropped after build)
        self.dem = None
        self.raw = OrderedDict()   # run id -> SimResult (model output)
        self.fc = OrderedDict()    # run id -> SimResult (with resident reports assimilated)
        self.built_at = 0
        self.last_seen = 0.0
        self.payload = None        # gzip-ready geometry JSON bytes
        self.terrain_png = None
        self.mids = None           # (nseg, 2) metres, for nearest-segment queries
        self.documented = []
        self.seeds = []

    @property
    def gid_prefix(self):
        return str(self.key)

    def latest_fc(self, run_id=None):
        if run_id and run_id in self.fc:
            return self.fc[run_id], False
        if self.fc:
            rid = next(reversed(self.fc))
            return self.fc[rid], rid != run_id
        return None, True


class Composite:
    """Several tiles seen as one area (for routing and the agent)."""

    def __init__(self, tiles, exclude_in=()):
        self.tiles = tiles
        self.segments = {}
        self.nodes = {}
        self.barriers = {}
        lats = []
        for t in tiles:
            a = t.area
            self.nodes.update(a.nodes)
            self.barriers.update(a.barriers)
            for sg in a.segments:
                if t.key.lod is REGION_LOD and any(in_bbox(sg.mid[0], sg.mid[1], b) for b in exclude_in):
                    continue  # the street-level tile here has this road in full detail
                self.segments[sg.gid] = sg
            lats.append(a.lat0)
        self.lat0 = float(np.mean(lats)) if lats else 13.0
        self.m_per_deg_lon = 111_320.0 * math.cos(math.radians(self.lat0))

    def dist_m(self, a, b):
        return math.hypot((a[0] - b[0]) * M_PER_DEG_LAT, (a[1] - b[1]) * self.m_per_deg_lon)


class CompSim:
    """Depth lookups across tiles, for one run (falls back to each tile's newest forecast)."""

    def __init__(self, world, run_id):
        self.world, self.run_id = world, run_id
        self.times = FRAMES
        self._cache = {}

    def frame_at(self, t):
        return int(min(max(round(t / DT_MIN), 0), len(FRAMES) - 1))

    def _fc(self, prefix):
        if prefix not in self._cache:
            t = self.world.tiles.get(prefix)
            self._cache[prefix] = t.latest_fc(self.run_id)[0] if t else None
        return self._cache[prefix]

    def depth(self, gid, f):
        prefix, i = gid.rsplit(":", 1)
        fc = self._fc(prefix)
        if fc is None or not fc.seg_depth.shape[1]:
            return 0
        return int(fc.seg_depth[min(f, fc.seg_depth.shape[0] - 1), int(i)])

    def timeline(self, gid):
        prefix, i = gid.rsplit(":", 1)
        fc = self._fc(prefix)
        return fc.seg_depth[:, int(i)].tolist() if fc is not None else [0] * len(FRAMES)


class World:
    def __init__(self, data_dir, publish=lambda ev: None, log=print):
        self.data_dir = data_dir
        self.publish = publish
        self.log = log
        self.osm, self.terrain, self.cache = make_providers(data_dir)
        self.synthetic = getattr(self.osm, "synthetic", False)
        # 'bulk' = one lane for the heavy 38 km main-road downloads, so they never crowd out your streets
        self.sched = Scheduler({"bulk": 1, "net": int(os.getenv("NET_WORKERS", "5")), "cpu": int(os.getenv("CPU_WORKERS", str(max(1, min(3, (os.cpu_count() or 2) - 0)))))})
        self.rain = RainService(self.cache)
        self.ctx = ContextService(data_dir / "world", lambda k, fn, prio, pool: self.sched.submit(k, fn, prio, pool))
        self.lock = threading.RLock()
        self.tiles: dict[str, Tile] = {}
        self.models = OrderedDict()   # tile path -> FloodModel (LRU)
        self.focus = None             # (lat, lon)
        self.focus_at = 0
        self.store = None
        self.resident = []            # resident reports (persisted by the server)
        self.closed, self.slow, self.closure_records = set(), {}, []
        self.tt_error = None
        self.demo_persona = None
        self.demo_state = "off"
        self._routers = OrderedDict()
        self._pending_events = {"tiles": set(), "forecast": set()}
        self._ev_lock = threading.Lock()
        threading.Thread(target=self._event_pump, daemon=True).start()

    # ---------------------------------------------------------------- events
    def _emit(self, kind, key):
        with self._ev_lock:
            self._pending_events[kind].add(key)

    def _event_pump(self):
        while True:
            time.sleep(0.6)
            with self._ev_lock:
                ev = {k: sorted(v) for k, v in self._pending_events.items() if v}
                for v in self._pending_events.values():
                    v.clear()
            if ev.get("tiles"):
                self.publish({"type": "tiles", "tiles": ev["tiles"]})
            if ev.get("forecast"):
                self.publish({"type": "forecast", "tiles": ev["forecast"], "run_id": self.rain.run.id, "label": self.rain.run.label})

    # ------------------------------------------------------------------ tiles
    def tile(self, key) -> Tile:
        k = str(key)
        with self.lock:
            t = self.tiles.get(k)
            if t is None:
                t = self.tiles[k] = Tile(key if isinstance(key, TileKey) else TileKey.parse(k))
            return t

    def _prio(self, cls, key):
        d = km_between(self.focus, key.center) if self.focus else 0
        return (cls, round(d, 1))

    def want(self, key, cls=NEAR, forecast=True, run=None):
        """Make sure a tile is built (and forecast for the current run), at this priority."""
        t = self.tile(key)
        if cls == VISIBLE:
            t.last_seen = time.time()
        prio = self._prio(cls, t.key)
        if t.status in ("new", "failed"):
            pool = "bulk" if t.key.lod is REGION_LOD and cls in (REGION, IDLE) else "net"
            self.sched.submit(f"fetch:{t.key}", lambda: self._fetch(t), prio, pool,
                              on_done=lambda j: j.state == "done" and self._after_fetch(t, cls, forecast))
        elif t.status == "fetched":
            self._after_fetch(t, cls, forecast)
        elif t.status == "ready" and forecast:
            self._want_fc(t, run or self.rain.run, prio)
        return t

    def _after_fetch(self, t, cls, forecast):
        if t.status in ("empty", "failed"):
            return
        self.sched.submit(f"build:{t.key}", lambda: self._build(t), self._prio(cls, t.key), "cpu",
                          on_done=lambda j: j.state == "done" and forecast and t.status == "ready" and self._want_fc(t, self.rain.run, self._prio(cls, t.key)))

    def _want_fc(self, t, run: Run, prio):
        if run.id in t.fc:
            return
        self.sched.submit(f"fc:{t.key}:{run.id}", lambda: self._forecast(t, run), prio, "cpu")

    # -------------------------------------------------------------- pipeline
    def _fetch(self, t: Tile):
        t.status = "fetching"
        key = t.key
        try:
            data, src = self.osm.osm(key)
            t.source = src
            if not data["ways"]:
                t.status = "empty"
                self._emit("tiles", str(key))
                self._maybe_region()
                return
            pb = key.padded_bbox()
            cell = key.lod.cell_m
            rows = max(16, int((pb[2] - pb[0]) * M_PER_DEG_LAT / cell))
            cols = max(16, int((pb[3] - pb[1]) * 111_320 * math.cos(math.radians((pb[0] + pb[2]) / 2)) / cell))
            dem, _ = self.terrain.dem(pb, rows, cols, cell)
            t.data, t.dem = data, dem
            t.status = "fetched"
        except Exception as e:
            t.status, t.error = "failed", f"{type(e).__name__}: {e}"[:200]
            self._emit("tiles", str(key))
            self._maybe_region()
            raise

    def _build(self, t: Tile):
        t.status = "building"
        key, data = t.key, t.data
        try:
            pb, tb = key.padded_bbox(), key.bbox
            dem = gaussian_filter(t.dem.astype(np.float32), 1.2)  # SRTM in cities is noisy (rooftops, trees)
            rows, cols = dem.shape
            if data["water"]:
                dem[raster_polys(pb, rows, cols, data["water"])] -= 1.0
            locs = data["localities"] or [{"name": self._place_name(key), "lat": key.center[0], "lon": key.center[1]}]
            meta = {"name": self._place_name(key, locs), "city": "", "synthetic": self.synthetic, "bbox": pb, "tile_bbox": tb,
                    "localities": locs, "pois": [p for p in data["pois"] if in_bbox(p["lat"], p["lon"], tb)],
                    "water": data["water"], "cell_m": key.lod.cell_m, "lod": key.lod.name}
            area = build_area(str(key), meta, dem, {"nodes": data["nodes"], "ways": data["ways"], "barriers": data["barriers"]},
                              owner_bbox=tb, gid_prefix=str(key))
            t.area = area
            t.mids = np.array([[s.mid[0] * M_PER_DEG_LAT, s.mid[1] * area.m_per_deg_lon] for s in area.segments]) if area.segments else np.zeros((0, 2))
            self._model(t)
            t.documented = chennai.documented_reports(area) if key.lod is DETAIL else []
            t.payload = self._geometry(t)
            t.terrain_png = self._hillshade(t)
            t.built_at = time.time()
            t.data, t.dem = None, None
            t.status = "ready"
            with self.lock:
                self._routers.clear()
            self._emit("tiles", str(key))
            if key.lod is DETAIL:
                self._maybe_region()
            if self.store:
                self.sched.submit(f"idx-tile:{key}", lambda: self.store.index_tile(t), (IDLE, 0), "net")
        except Exception as e:
            t.status, t.error = "failed", f"{type(e).__name__}: {e}"[:200]
            raise

    def _model(self, t: Tile) -> FloodModel:
        k = str(t.key)
        with self.lock:
            m = self.models.get(k)
            if m is not None:
                self.models.move_to_end(k)
                return m
        m = FloodModel(t.area)
        with self.lock:
            self.models[k] = m
            while len(self.models) > MAX_MODELS:
                self.models.popitem(last=False)
        return m

    def _place_name(self, key, locs=None):
        if locs:
            c = key.center
            return min(locs, key=lambda L: (L["lat"] - c[0]) ** 2 + (L["lon"] - c[1]) ** 2)["name"]
        return f"Tile {key}"

    def context_for(self, t: Tile, run: Run):
        lat, lon = t.key.center
        clim = self.ctx.climate_for(lat, lon) if t.key.lod is DETAIL else self.ctx.nearest_climate(lat, lon)
        base = chennai.drain_capacity_from_climate(clim) if clim else 18.0
        ground, tide = None, None
        if run.kind == "live":
            z = self.ctx.live(str(t.key.parent(REGION_LOD.z)), lat, lon)
            ground, tide = z.get("ante"), z.get("tide")
        elif run.kind == "demo":
            ground = chennai.antecedent_effect(30, 70)  # a damp week, fixed so the demo is reproducible
        else:
            ground = (run.detail.get("replay") or {}).get("antecedent")
        return {"base_drain": base, "climate": clim, "ground": ground, "tide": tide}

    def _forecast(self, t: Tile, run: Run):
        if t.status != "ready":
            return
        if run.kind != "demo":
            self.rain.ensure(t.key.padded_bbox())
        m = self._model(t)
        c = self.context_for(t, run)
        m.base_drain = c["base_drain"]
        m.calibrate(self._reports_in(t))
        m.set_context(runoff=(c["ground"] or {}).get("runoff", 0.85), drain_eff=(c["ground"] or {}).get("drain_eff", 1.0), tide=c["tide"])
        a = t.area
        rows, cols = a.dem.shape
        sim = m.simulate(self.rain.grid(a.bbox, rows, cols, a.cell_m, run), run.id, start=-120 if run.kind != "demo" else -30)
        sim.label = run.label
        sim.context = {"drain_mmph": round(c["base_drain"], 1), "ground": c["ground"],
                       "tide": c["tide"] and {k: c["tide"][k] for k in ("next_high_min", "next_high_m")},
                       "climate": c["climate"] and {"years": c["climate"]["years"], "T2": c["climate"]["hourly"]["T2"], "T100": c["climate"]["hourly"]["T100"]}}
        with self.lock:
            t.raw[run.id] = sim
            t.fc[run.id] = self.assimilate(t, sim)
            for d in (t.raw, t.fc):
                while len(d) > 3:
                    d.popitem(last=False)
        if run.kind == "demo" and run.version == 1 and t.key.lod is DETAIL and not t.seeds:
            t.seeds = self._seed_reports(t, sim)
        self._emit("forecast", str(t.key))
        if self.store and run.id == self.rain.run.id:
            self.sched.submit(f"idx-fc:{t.key}:{run.id}", lambda: self.store.index_forecast(t, t.fc.get(run.id)), (IDLE, 1), "net")

    # ------------------------------------------------------------ payloads
    def _geometry(self, t: Tile):
        """Compact, immutable geometry for the client: integer micro-degree deltas."""
        a = t.area
        s, w = t.key.bbox[0], t.key.bbox[1]
        names, nidx = [], {}
        loc_names = [L["name"] for L in a.meta["localities"]]
        segs = []
        for sg in a.segments:
            if sg.name not in nidx:
                nidx[sg.name] = len(names)
                names.append(sg.name)
            flat, pla, plo = [], round((s) * 1e6), round(w * 1e6)
            for la, lo in sg.coords:
                ila, ilo = round(la * 1e6), round(lo * 1e6)
                flat += [ila - pla, ilo - plo]
                pla, plo = ila, ilo
            flags = (1 if sg.bridge else 0) | (2 if sg.underpass else 0) | (4 if sg.construction else 0)
            segs.append([CLASS.get(sg.highway, 2), nidx[sg.name], loc_names.index(sg.locality) if sg.locality in loc_names else 0, flat, flags])
        tb = t.key.bbox
        doc = {"key": str(t.key), "lod": t.key.lod.name, "bbox": tb, "origin": [s, w], "names": names,
               "localities": [L for L in a.meta["localities"]], "own_localities": [i for i, L in enumerate(a.meta["localities"]) if in_bbox(L["lat"], L["lon"], tb)],
               "segments": segs, "water": a.meta["water"] if t.key.lod is DETAIL else [p for p in a.meta["water"] if len(p) > 30],
               "pois": a.meta["pois"], "cell_m": a.meta["cell_m"], "synthetic": self.synthetic, "source": t.source,
               "v": GEOM_V, "built": int(t.built_at or time.time())}
        return json.dumps(doc, separators=(",", ":")).encode()

    def _hillshade(self, t: Tile):
        """Terrain shading for the tile window, as a small greyscale PNG (low ground darker)."""
        from PIL import Image
        m = self._model(t)
        r0, r1, c0, c1 = m.inner
        z = m.dem_flat.reshape(m.rows, m.cols)[r0:r1, c0:c1]
        cell = m.cell
        gy, gx = np.gradient(z, cell)
        az, alt = math.radians(315), math.radians(40)
        slope = np.arctan(np.hypot(gx, gy) * 6)
        aspect = np.arctan2(-gx, gy)
        hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
        lo, hi = np.percentile(z, 2), np.percentile(z, 98)
        rel = np.clip((z - lo) / max(hi - lo, 0.5), 0, 1)
        # white = flat high ground (no change under multiply); shadowed slopes and low ground darker
        shade = 1 - 1.4 * np.clip(np.sin(alt) - hs, 0, None) - 0.22 * (1 - rel)
        v = np.clip(shade * 255, 0, 255).astype(np.uint8)
        img = Image.fromarray(v, "L")
        if max(img.size) > 256:
            img = img.resize((max(1, img.width // 2), max(1, img.height // 2)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def forecast_payload(self, t: Tile):
        fc, stale = t.latest_fc(self.rain.run.id)
        if fc is None:
            return None
        b = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()  # noqa: E731
        return {"key": str(t.key), "run_id": fc.run_id, "stale": stale, "label": fc.label,
                "nseg": int(fc.seg_depth.shape[1]), "frames": len(fc.times), "seg_depth": b(fc.seg_depth),
                "raster": b(fc.raster), "raster_shape": list(fc.raster.shape[1:]), "raster_bbox": fc.raster_bbox,
                "rain": b(fc.rain_coarse), "rain_shape": list(fc.rain_coarse.shape[1:]),
                "rain_mean": fc.rain_mean, "total_mm": fc.total_mm, "observations": fc.observations}

    # --------------------------------------------------------------- focus
    def set_focus(self, lat, lon, view=None):
        """Someone is looking here: schedule street detail near them and main roads for 100 km."""
        moved = not self.focus or km_between(self.focus, (lat, lon)) > 1.0
        self.focus, self.focus_at = (lat, lon), time.time()
        if view:
            for k in tiles_in_bbox(view, DETAIL.z)[:40]:
                self.want(k, VISIBLE)
        if not moved:
            return
        for k in tiles_within(lat, lon, DETAIL_KM, DETAIL.z):
            self.want(k, NEAR)
        self._region_plan = (lat, lon)
        self._maybe_region()
        if self.rain.source != "demo":
            from .tiles import bbox_around
            bb = bbox_around(lat, lon, min(REGION_KM, 60))
            self.sched.submit(f"rain-prefetch:{round(lat, 1)}:{round(lon, 1)}:{self.rain.run.id}", lambda: self.rain.ensure(bb), (NEAR, 0), "net")
        self.ctx.climate_for(lat, lon, prio=(NEAR, 0))

    def _maybe_region(self):
        """Main roads for 100 km start only once the streets near you are in: your area first, always."""
        plan = getattr(self, "_region_plan", None)
        if not plan:
            return
        near = [self.tiles.get(str(k)) for k in tiles_within(*plan, DETAIL_KM, DETAIL.z)]
        if any(t is None or t.status not in ("ready", "empty", "failed") for t in near):
            return
        self._region_plan = None
        for k in tiles_within(*plan, REGION_KM, REGION_LOD.z):
            d = km_between(plan, k.center)
            self.want(k, REGION if d < 45 else IDLE, forecast=True)

    def progress(self, lat=None, lon=None):
        lat, lon = (lat, lon) if lat is not None else (self.focus or (None, None))
        out = {}
        for lod, km in ((DETAIL, DETAIL_KM), (REGION_LOD, REGION_KM)):
            keys = tiles_within(lat, lon, km, lod.z) if lat is not None else []
            st = [self.tiles[str(k)].status if str(k) in self.tiles else "new" for k in keys]
            fc = sum(1 for k in keys if str(k) in self.tiles and self.rain.run.id in self.tiles[str(k)].fc)
            out[lod.name] = {"tiles": len(keys), "ready": st.count("ready"), "empty": st.count("empty"),
                             "failed": st.count("failed"), "forecast": fc, "km": km}
        return out

    # -------------------------------------------------------------- runs
    def new_run(self, run: Run):
        """A new rain run: re-forecast tiles by priority (what you're looking at first)."""
        now = time.time()
        with self.lock:
            ready = [t for t in self.tiles.values() if t.status == "ready"]
        for t in ready:
            d = km_between(self.focus, t.key.center) if self.focus else 0
            if now - t.last_seen < 120:
                cls = VISIBLE
            elif t.key.lod is DETAIL and d <= DETAIL_KM:
                cls = NEAR
            elif t.key.lod is REGION_LOD and d < 45:
                cls = REGION
            else:
                cls = IDLE
            self._want_fc(t, run, (cls, round(d, 1)))

    def wait_for(self, keys, run_id, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            ts = [self.tiles.get(str(k)) for k in keys]
            if all(t and (t.status in ("empty", "failed") or (t.status == "ready" and run_id in t.fc)) for t in ts):
                return True
            time.sleep(0.25)
        return False

    # ------------------------------------------------------------- reports
    def _reports_in(self, t: Tile):
        pb = t.key.padded_bbox()
        return [r for r in self.all_reports() if r.get("kind") != "closed" and in_bbox(r["lat"], r["lon"], pb)]

    def all_reports(self, with_seeds=None):
        out = []
        with self.lock:
            tiles = list(self.tiles.values())
        for t in tiles:
            out += t.documented
            if (with_seeds if with_seeds is not None else self.rain.source == "demo"):
                out += t.seeds
        return out + [r for r in self.resident if r.get("kind") != "closed"]

    def assimilate(self, t: Tile, raw):
        """Pull the next 90 minutes of a tile's forecast towards recent resident reports nearby."""
        import copy
        sim = copy.copy(raw)
        sim.seg_depth = raw.seg_depth.copy()
        now = time.time()
        used = 0
        pb = t.key.padded_bbox()
        mlon = t.area.m_per_deg_lon
        for r in self.resident:
            if r.get("kind") == "closed" or not in_bbox(r["lat"], r["lon"], pb):
                continue
            age = (now - r.get("ts", 0)) / 60
            if age > 120 or not len(t.mids):
                continue
            p = np.array([r["lat"] * M_PER_DEG_LAT, r["lon"] * mlon])
            near = np.nonzero(np.hypot(*(t.mids - p).T) <= 120)[0]
            if not len(near):
                continue
            used += 1
            obs = float(r["depth_cm_reported"]) * (1 - age / 180)
            for fi, tm in enumerate(sim.times):
                if tm > 90:
                    break
                w = 1 - tm / 90
                cur = sim.seg_depth[fi, near].astype(float)
                new = np.where(obs < cur, cur * (1 - w) + obs * w, np.maximum(cur, obs * w))
                sim.seg_depth[fi, near] = np.clip(np.round(new), 0, 250)
        sim.observations = used
        return sim

    def reassimilate_near(self, lat, lon):
        for k in [tile_of(lat, lon, DETAIL.z), *tile_of(lat, lon, DETAIL.z).neighbours()]:
            t = self.tiles.get(str(k))
            if t and t.status == "ready":
                with self.lock:
                    for rid, raw in t.raw.items():
                        t.fc[rid] = self.assimilate(t, raw)
                self._emit("forecast", str(k))
                if self.store and self.rain.run.id in t.fc:
                    self.sched.submit(f"idx-fc:{t.key}:{time.time()}", lambda t=t: self.store.index_forecast(t, t.fc.get(self.rain.run.id)), (IDLE, 1), "net")

    def nearest_segment(self, lat, lon, max_m=200):
        best = (None, max_m)
        for k in [tile_of(lat, lon, DETAIL.z), *tile_of(lat, lon, DETAIL.z).neighbours()]:
            t = self.tiles.get(str(k))
            if not t or t.status != "ready" or not len(t.mids):
                continue
            p = np.array([lat * M_PER_DEG_LAT, lon * t.area.m_per_deg_lon])
            d = np.hypot(*(t.mids - p).T)
            i = int(d.argmin())
            if d[i] < best[1]:
                best = (t.area.segments[i], float(d[i]))
        return best[0]

    def segment(self, gid):
        prefix, i = gid.rsplit(":", 1)
        t = self.tiles.get(prefix)
        if not t or t.status != "ready":
            return None, None
        i = int(i)
        return (t, t.area.segments[i]) if 0 <= i < len(t.area.segments) else (None, None)

    def _seed_reports(self, t, sim):
        from ..engine.history import TEMPLATES, DATES
        import random
        rng = random.Random(hash(str(t.key)) & 0xFFFF)
        peak = sim.seg_depth.max(axis=0).astype(int)
        cands = [s for s in t.area.segments if peak[s.idx] >= 20 and not s.name.startswith("Unnamed")]
        out = []
        for i in range(min(10, len(cands))):
            s = rng.choices(cands, weights=[peak[c.idx] for c in cands])[0]
            out.append({"id": f"seed-{t.key}-{i}", "text": rng.choice(TEMPLATES).format(street=s.name), "street": s.name,
                        "locality": s.locality, "date": rng.choice(DATES), "depth_cm_reported": int(min(120, peak[s.idx])),
                        "lat": s.mid[0], "lon": s.mid[1], "seg": s.gid, "source": "synthetic-seed"})
        if self.store:
            self.sched.submit(f"idx-seeds:{t.key}", lambda: self.store.add_reports(out), (IDLE, 0), "net")
        return out

    # ------------------------------------------------------------- routing
    def corridor(self, frm, to):
        km = km_between(frm, to)
        pad = max(0.012, 0.18 * max(abs(frm[0] - to[0]), abs(frm[1] - to[1])))
        bbox = [min(frm[0], to[0]) - pad, min(frm[1], to[1]) - pad, max(frm[0], to[0]) + pad, max(frm[1], to[1]) + pad]
        if km <= 14:
            detail = tiles_in_bbox(bbox, DETAIL.z)
            region = []
        else:
            detail = sorted({tile_of(*frm, DETAIL.z), tile_of(*to, DETAIL.z)})
            region = tiles_in_bbox(bbox, REGION_LOD.z)
        return km, bbox, detail, region

    def composite_for(self, frm, to, cls=ROUTE, require=True):
        km, bbox, detail, region = self.corridor(frm, to)
        run = self.rain.run
        for k in detail + region:
            self.want(k, cls)
        ts = [self.tiles[str(k)] for k in detail + region]
        need = [t for t in ts if t.status not in ("empty",)]
        ok = [t for t in need if t.status == "ready" and t.fc]
        if require and len(ok) < len(need):
            failed = [t for t in need if t.status == "failed"]
            if failed and len(ok) + len(failed) == len(need) and ok:
                pass  # route with what we have
            else:
                raise Preparing(len(ok), len(need), "main roads" if region else "streets")
        dt = [t for t in ok if t.key.lod is DETAIL]
        rt = [t for t in ok if t.key.lod is REGION_LOD]
        sig = tuple(sorted(str(t.key) for t in ok)) + (len(self.closed), tuple(sorted(self.slow.items()))[:5])
        with self.lock:
            router = self._routers.get(sig)
        if router is None:
            comp = Composite(dt + rt, exclude_in=[t.key.bbox for t in dt])
            if not comp.segments:
                raise Preparing(0, len(need))
            router = Router(comp)
            with self.lock:
                self._routers[sig] = router
                while len(self._routers) > 6:
                    self._routers.popitem(last=False)
        router.closed, router.slow = self.closed, self.slow
        return router, CompSim(self, run.id), km

    def plan(self, frm, to, mode, depart, cls=ROUTE):
        router, sim, km = self.composite_for(frm, to, cls)
        p = router.plan(frm, to, mode, min(depart, 340), sim)
        p["corridor_km"] = round(km, 1)
        p["lod"] = "main roads" if km > 14 else "streets"
        return p, router, sim

    # ------------------------------------------------------------- closures
    def refresh_closures(self):
        """TomTom incidents near you + resident 'road closed' reports + construction."""
        recs, closed, slow = [], set(), {}
        if traffic.key() and self.focus:
            from .tiles import bbox_around
            ts = [self.tiles[str(k)] for k in tiles_within(*self.focus, DETAIL_KM, DETAIL.z) if str(k) in self.tiles and self.tiles[str(k)].status == "ready"]
            if ts:
                try:
                    inc = traffic.fetch_incidents(bbox_around(*self.focus, DETAIL_KM))
                    idx = traffic.SegmentIndex(Composite(ts))
                    c, s, r = traffic.to_closures(inc, idx)
                    closed |= c
                    slow.update(s)
                    recs += r
                    self.tt_error = None
                except Exception as e:  # noqa: BLE001
                    self.tt_error = f"{type(e).__name__}: {e}"[:160]
        elif not traffic.key():
            self.tt_error = "TOMTOM_API_KEY not set"
        now = time.time()
        for r in self.resident:
            if r.get("kind") == "closed" and now - r.get("ts", 0) < 3 * 3600 and r.get("seg"):
                closed.add(r["seg"])
                recs.append({"segs": [r["seg"]], "kind": "Road closed", "closes": True, "desc": f"Reported closed by a resident at {r.get('time', '')}",
                             "until": None, "source": "resident"})
        with self.lock:
            tiles = [t for t in self.tiles.values() if t.status == "ready" and t.key.lod is DETAIL]
        for t in tiles:
            for sg in t.area.segments:
                if sg.construction:
                    closed.add(sg.gid)
                    recs.append({"segs": [sg.gid], "kind": "Under construction", "closes": True, "desc": "Road under construction (OpenStreetMap)",
                                 "until": None, "source": "OpenStreetMap"})
        self.closed, self.slow, self.closure_records = closed, slow, recs

    # ------------------------------------------------------------- demo
    def setup_demo(self, anchor):
        """Demo storm on the streets where you are: forecast both nowcasts for the 3x3 tiles
        around you, then pick a commute that tells the story (watch at 4:10, flooded at 4:25)."""
        self.demo_state = "preparing"
        self.demo_persona = None
        threading.Thread(target=self._demo_job, args=(tuple(anchor),), daemon=True).start()

    def locality_of(self, lat, lon):
        t = self.tiles.get(str(tile_of(lat, lon, DETAIL.z)))
        return t.area.locality_of(lat, lon) if t and t.area else "Here"

    def _demo_job(self, anchor):
        from ..engine.story import pick_story
        try:
            run0, run1 = self.rain.demo_run(0), self.rain.demo_run(1)
            center = tile_of(*anchor, DETAIL.z)
            keys = [center, *center.neighbours()]
            for k in keys:
                self.want(k, ROUTE, run=run0)
            t0 = time.time()
            while time.time() - t0 < 300:
                ts = [self.tiles[str(k)] for k in keys]
                if all(t.status in ("ready", "empty", "failed") for t in ts):
                    break
                for t in ts:
                    if t.status == "ready":
                        self._want_fc(t, run0, (ROUTE, 0))
                time.sleep(0.5)
            ready = [self.tiles[str(k)] for k in keys if self.tiles[str(k)].status == "ready"]
            for t in ready:
                self._want_fc(t, run0, (ROUTE, 0))
                self._want_fc(t, run1, (ROUTE, 1))
            self.wait_for([t.key for t in ready], run0.id, 240)
            self.wait_for([t.key for t in ready], run1.id, 240)
            if self.rain.source != "demo":
                self.demo_state = "off"
                return
            comp = Composite(ready)
            router = Router(comp)
            persona = pick_story(router, CompSim(self, run0.id), CompSim(self, run1.id), self.locality_of, center=anchor)
            self.demo_persona = persona
            self.demo_state = "ready" if persona else "none"
            self.log(f"Demo commute: {persona and persona['from_label']} -> {persona and persona['to_label']} {persona and persona['check']}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.demo_state = f"failed: {type(e).__name__}"
        self.publish({"type": "demo", "state": self.demo_state})

    def demo_status(self):
        return {"state": self.demo_state, "persona": self.demo_persona}
