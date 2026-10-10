"""Where tile data comes from.

  OSMProvider        streets, water, localities, hospitals and schools from OpenStreetMap
                     (Overpass API, mirror failover), cached per tile on disk for 30 days.
  TerrainProvider    elevation from AWS Terrain Tiles (Registry of Open Data, terrarium PNG),
                     each PNG cached on disk once and shared by every tile that overlaps it.
  SyntheticProvider  an invented, seamless procedural world (streets, terrain, lakes) for
                     offline use and tests. Labelled "synthetic streets" in the UI.

Both real providers return the same shapes, so the rest of the system never knows
which one it is talking to.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import net
from .tiles import TileKey

SCHEMA = 3  # bump when the parsed OSM format changes
OSM_TTL_S = 30 * 24 * 3600
MAJOR = "motorway|trunk|primary|secondary|motorway_link|trunk_link|primary_link|secondary_link"
HIGHWAYS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|"
            "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link")
OVERPASS_URLS = net.env_list("OVERPASS_URLS", ["https://overpass-api.de/api/interpreter",
                                              "https://overpass.kumi.systems/api/interpreter",
                                              "https://overpass.private.coffee/api/interpreter"])
TERRAIN_URL = os.getenv("TERRAIN_URL", "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png")


def _b(bb):
    s, w, n, e = bb
    return f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"


# ====================================================================== OSM
class OSMProvider:
    name = "osm"
    synthetic = False
    attribution = ["© OpenStreetMap contributors (ODbL)", "Terrain Tiles, AWS Registry of Open Data (SRTM)"]

    def __init__(self, cache: net.DiskCache):
        self.cache = cache
        self._mirror = 0

    def query(self, key: TileKey):
        tb, pb = _b(key.bbox), _b(key.padded_bbox())
        major = key.lod.major_only
        roads = MAJOR if major else HIGHWAYS
        places = "city|town|suburb" if major else "suburb|neighbourhood|quarter"
        detail = "" if major else f"""
way["highway"="construction"]({tb})->.cons;
.cons out body;
.cons >;
out skel qt;
node(w.roads)["barrier"];
out;
nwr["amenity"~"^(hospital|clinic|school)$"]({tb});
out center;"""
        water = f"""
(way["natural"="water"]({pb});way["landuse"="reservoir"]({pb});way["natural"="wetland"]({pb});way["waterway"="riverbank"]({pb}););
out geom;
relation["natural"="water"]({pb});
out geom;""" if not major else f"""
(way["natural"="water"]["water"~"lake|reservoir|river|lagoon"]({pb});way["landuse"="reservoir"]({pb}););
out geom;
relation["natural"="water"]["water"~"lake|reservoir|river|lagoon"]({pb});
out geom;"""
        return f"""[out:json][timeout:150];
way["highway"~"^({roads})$"]({tb})->.roads;
.roads out body;
.roads >;
out skel qt;{detail}{water}
node["place"~"^({places})$"]({pb});
out;"""

    def osm(self, key: TileKey):
        """Parsed OSM for a tile; cache first, refreshed after 30 days (stale copy kept on failure)."""
        doc = self.cache.get("osm", key.z, key.x, key.y, max_age_s=OSM_TTL_S)
        if doc and doc.get("v") == SCHEMA and not doc.get("_stale"):
            return doc["data"], "cache"
        try:
            raw = self._fetch(self.query(key))
            data = parse_osm(raw, major_only=key.lod.major_only)
            self.cache.put({"v": SCHEMA, "data": data}, "osm", key.z, key.x, key.y)
            return data, "network"
        except Exception:
            if doc and doc.get("v") == SCHEMA:
                return doc["data"], "stale cache"
            raise

    def _fetch(self, q):
        """Try the least-busy healthy mirror first; mirrors run in parallel, each within its own limits."""
        import json
        mirrors = [net.overpass_mirror(u) for u in OVERPASS_URLS]
        order = sorted(range(len(mirrors)), key=lambda i: (mirrors[i].state == "open", mirrors[i].busy, mirrors[i]._fails, (i - self._mirror) % len(mirrors)))
        self._mirror = (self._mirror + 1) % len(mirrors)
        last = None
        for i in order:
            m = mirrors[i]
            if m.state == "open":
                continue
            try:
                return json.loads(m.post(OVERPASS_URLS[i], data={"data": q}, retries=1).content)
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"All Overpass mirrors failed or cooling off: {last}")


def _join_rings(parts):
    """Stitch multipolygon member ways into closed rings (end-to-end joining)."""
    parts = [p[:] for p in parts if len(p) >= 2]
    rings = []
    while parts:
        cur = parts.pop()
        changed = True
        while changed and cur[0] != cur[-1]:
            changed = False
            for i, p in enumerate(parts):
                if p[0] == cur[-1]:
                    cur += p[1:]
                elif p[-1] == cur[-1]:
                    cur += p[::-1][1:]
                elif p[-1] == cur[0]:
                    cur = p + cur[1:]
                elif p[0] == cur[0]:
                    cur = p[::-1] + cur[1:]
                else:
                    continue
                parts.pop(i)
                changed = True
                break
        if len(cur) >= 4:
            rings.append(cur)
    return rings


def parse_osm(osm, major_only=False):
    nodes, ways, water, places, pois, barriers = {}, [], [], [], [], {}
    for el in osm.get("elements", []):
        t = el.get("tags", {})
        if el["type"] == "node" and "lat" in el:
            nodes.setdefault(str(el["id"]), [round(el["lat"], 6), round(el["lon"], 6)])
        if el["type"] == "way" and "highway" in t and "nodes" in el:
            try:
                layer = int(str(t.get("layer", "0")).split(";")[0])
            except ValueError:
                layer = 0
            construction = t["highway"] == "construction"
            hw = (t.get("construction") or "residential") if construction else t["highway"]
            ways.append({"id": el["id"], "name": t.get("name:en") or t.get("name") or t.get("ref") or "",
                         "highway": hw.replace("_link", ""), "nodes": [str(n) for n in el["nodes"]],
                         "bridge": t.get("bridge") not in (None, "no") or layer > 0,
                         "tunnel": t.get("tunnel") in ("yes", "building_passage") or layer < 0,
                         "construction": construction})
        elif el["type"] == "way" and "geometry" in el:
            poly = [[round(p["lat"], 6), round(p["lon"], 6)] for p in el["geometry"]]
            if len(poly) >= 4 and (not major_only or len(poly) > 30):
                water.append(poly)
        elif el["type"] == "relation" and el.get("members"):
            outer = [[[round(p["lat"], 6), round(p["lon"], 6)] for p in m.get("geometry", [])]
                     for m in el["members"] if m.get("type") == "way" and m.get("role", "outer") in ("outer", "") and m.get("geometry")]
            water += _join_rings(outer)
        elif el["type"] == "node" and t.get("barrier"):
            barriers[str(el["id"])] = {"type": t["barrier"], "access": t.get("motor_vehicle") or t.get("access") or ""}
        elif el["type"] == "node" and t.get("place"):
            nm = t.get("name:en") or t.get("name")
            if nm:
                places.append({"name": nm, "lat": el["lat"], "lon": el["lon"], "kind": t["place"]})
        elif t.get("amenity") in ("hospital", "clinic", "school"):
            c = el.get("center") or ({"lat": el["lat"], "lon": el["lon"]} if "lat" in el else None)
            nm = t.get("name:en") or t.get("name")
            if c and nm:
                pois.append({"name": nm, "kind": "hospital" if t["amenity"] != "school" else "school",
                             "lat": c["lat"], "lon": c["lon"]})
    used = {n for w in ways for n in w["nodes"]}
    nodes = {k: v for k, v in nodes.items() if k in used}
    rank = {"city": 0, "town": 1, "suburb": 2, "quarter": 3, "neighbourhood": 4}
    seen, locs = set(), []
    for p in sorted(places, key=lambda p: rank.get(p["kind"], 9)):
        if p["name"] not in seen and len(locs) < 16:
            seen.add(p["name"])
            locs.append({"name": p["name"], "lat": p["lat"], "lon": p["lon"]})
    return {"nodes": nodes, "ways": ways, "water": water, "localities": locs, "pois": pois[:60], "barriers": barriers}


# ================================================================== terrain
def deg2tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n
    return x, y


def terrain_zoom(cell_m):
    return 14 if cell_m <= 25 else 13 if cell_m <= 50 else 12 if cell_m <= 100 else 11


class TerrainProvider:
    def __init__(self, cache: net.DiskCache):
        self.cache = cache
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="terrain")

    def _png(self, z, x, y):
        b = self.cache.get_bytes("terrain", z, x, y, ext=".png")
        if b is None:
            b = net.TERRAIN.get(TERRAIN_URL.format(z=z, x=x, y=y)).content
            self.cache.put_bytes(b, "terrain", z, x, y, ext=".png")
        return _decode_terrarium(b)

    def dem(self, bbox, rows, cols, cell_m):
        s, w, n, e = bbox
        z = terrain_zoom(cell_m)
        x0, y0 = (int(v) for v in deg2tile(n, w, z))
        x1, y1 = (int(v) for v in deg2tile(s, e, z))
        keys = [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)]
        imgs = dict(zip(keys, self.pool.map(lambda k: self._png(z, *k), keys)))
        mosaic = np.zeros(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), dtype=np.float32)
        for (tx, ty), img in imgs.items():
            mosaic[(ty - y0) * 256:(ty - y0 + 1) * 256, (tx - x0) * 256:(tx - x0 + 1) * 256] = img
        lat = n - (np.arange(rows) + 0.5) / rows * (n - s)
        lon = w + (np.arange(cols) + 0.5) / cols * (e - w)
        fx, _ = deg2tile(lat[:1], lon, z)
        _, fy = deg2tile(lat, lon[:1], z)
        gx = (fx - x0) * 256 - 0.5
        gy = (fy - y0) * 256 - 0.5
        from scipy.ndimage import map_coordinates
        yy, xx = np.meshgrid(gy, gx, indexing="ij")
        return map_coordinates(mosaic, [yy, xx], order=1, mode="nearest").astype(np.float32), f"terrarium z{z}, {len(keys)} PNGs"


@lru_cache(maxsize=256)
def _decode_cached(digest, b):  # noqa: ARG001  (digest makes the cache key cheap)
    from PIL import Image
    img = np.asarray(Image.open(io.BytesIO(b)).convert("RGB")).astype(np.float32)
    return img[..., 0] * 256 + img[..., 1] + img[..., 2] / 256 - 32768


def _decode_terrarium(b: bytes):
    return _decode_cached(hashlib.blake2s(b, digest_size=8).hexdigest(), b)


# ================================================================ synthetic
R_EARTH = 6378137.0
SP = 150.0  # street lattice spacing, Web-Mercator metres
ART_I, ART_J = 12, 9  # arterial every n lattice lines
NAMES = ["Gandhi", "Nehru", "Bharathi", "Kamarajar", "Periyar", "Anna", "Vivekananda", "Tagore", "Patel",
         "Thiru Vi Ka", "Rajaji", "Ambedkar", "Kalki", "Pari", "Valluvar", "Avvai", "Kambar", "Ilango"]
ARTERIALS = ["Lake View Rd", "Station Rd", "Temple St", "Canal Rd", "Tech Park Rd", "Old Town Rd", "Market Rd",
             "Mill Rd", "Harbour Rd", "Ring Rd", "College Rd", "Bazaar Rd"]
LOCALITIES = ["Lakeside", "Station Colony", "Marsh End", "Tech Park", "Old Town", "Canal Side", "Mill Lane", "Palm Grove",
              "Riverbend", "Gandhi Nagar", "Sunrise Colony", "Bazaar Quarter", "Fort View", "Harbour End", "Hill Side", "New Town"]


def _h(*a):
    """Deterministic hash -> [0, 1)."""
    v = hashlib.blake2s(repr(a).encode(), digest_size=8).digest()
    return int.from_bytes(v, "little") / 2 ** 64


def _merc(lat, lon):
    return R_EARTH * math.radians(lon), R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _unmerc(x, y):
    return math.degrees(2 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2), math.degrees(x / R_EARTH)


def _value_noise(X, Y, scale, seed):
    """Smooth value noise on a global lattice (vectorised)."""
    gx, gy = X / scale, Y / scale
    i0, j0 = np.floor(gx).astype(np.int64), np.floor(gy).astype(np.int64)
    fx, fy = gx - i0, gy - j0
    sx, sy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)

    def r(i, j):
        h = (i * 374761393 + j * 668265263 + seed * 1442695041) & 0xFFFFFFFF
        h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
        return (h ^ (h >> 16)) / 2 ** 32

    a, b, c, d = r(i0, j0), r(i0 + 1, j0), r(i0, j0 + 1), r(i0 + 1, j0 + 1)
    return (a * (1 - sx) + b * sx) * (1 - sy) + (c * (1 - sx) + d * sx) * sy


class SyntheticProvider:
    name = "synthetic"
    synthetic = True
    attribution = ["Synthetic streets and terrain (invented, for offline use)"]

    def lakes_near(self, X0, Y0, X1, Y1):
        L, out = 3000.0, []
        for i in range(int(X0 // L) - 1, int(X1 // L) + 2):
            for j in range(int(Y0 // L) - 1, int(Y1 // L) + 2):
                if _h("lake", i, j) < 0.16:
                    cx, cy = (i + _h("lx", i, j)) * L, (j + _h("ly", i, j)) * L
                    out.append((cx, cy, 160 + 260 * _h("lr", i, j)))
        return out

    def dem(self, bbox, rows, cols, cell_m):
        s, w, n, e = bbox
        lat = n - (np.arange(rows) + 0.5) / rows * (n - s)
        lon = w + (np.arange(cols) + 0.5) / cols * (e - w)
        X = R_EARTH * np.radians(lon)[None, :].repeat(rows, 0)
        Y = (R_EARTH * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)))[:, None].repeat(cols, 1)
        z = 4.0 + 7.0 * _value_noise(X, Y, 9000, 1) + 2.4 * _value_noise(X, Y, 2200, 2) + 1.0 * _value_noise(X, Y, 600, 3) + 0.35 * _value_noise(X, Y, 180, 4)
        X0, Y0, X1, Y1 = X.min(), Y.min(), X.max(), Y.max()
        for cx, cy, r in self.lakes_near(X0, Y0, X1, Y1):
            d = np.hypot(X - cx, Y - cy)
            z -= 2.6 * np.exp(-(d / (r * 1.6)) ** 2)
        # a few low colonies: small closed depressions that pond first
        L = 1100.0
        for i in range(int(X0 // L) - 1, int(X1 // L) + 2):
            for j in range(int(Y0 // L) - 1, int(Y1 // L) + 2):
                if _h("dip", i, j) < 0.35:
                    cx, cy = (i + _h("dx", i, j)) * L, (j + _h("dy", i, j)) * L
                    z -= (0.5 + 0.7 * _h("da", i, j)) * np.exp(-(np.hypot(X - cx, Y - cy) / (90 + 120 * _h("ds", i, j))) ** 2)
        return z.astype(np.float32), "synthetic terrain"

    def osm(self, key: TileKey):
        s, w, n, e = key.bbox
        X0, Y0 = _merc(s, w)
        X1, Y1 = _merc(n, e)
        major = key.lod.major_only
        lakes = self.lakes_near(X0, Y0, X1, Y1)

        def wet(x, y):
            return any(math.hypot(x - cx, y - cy) < r * 1.1 for cx, cy, r in lakes)

        def pos(i, j):
            return i * SP + (_h("jx", i, j) - 0.5) * 56, j * SP + (_h("jy", i, j) - 0.5) * 56

        nodes, ways = {}, []
        i0, i1 = int(X0 // SP) - 1, int(X1 // SP) + 1
        j0, j1 = int(Y0 // SP) - 1, int(Y1 // SP) + 1

        def nid(i, j):
            k = f"s{i}_{j}"
            if k not in nodes:
                la, lo = _unmerc(*pos(i, j))
                nodes[k] = [round(la, 6), round(lo, 6)]
            return k

        def inside(i, j):
            x, y = pos(i, j)
            return X0 <= x <= X1 and Y0 <= y <= Y1

        if major:  # main roads only: just walk the arterial lattice lines
            cells = [(i, j) for j in range(j0, j1 + 1) if j % ART_J == 0 for i in range(i0, i1 + 1)]
            cells += [(i, j) for i in range(i0, i1 + 1) if i % ART_I == 0 for j in range(j0, j1 + 1)]
            cells = sorted(set(cells))
        else:
            cells = [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
        for i, j in cells:
            if True:
                for di, dj, axis in ((1, 0, "h"), (0, 1, "v")):
                    a, b = (i, j), (i + di, j + dj)
                    if not (inside(*a) or inside(*b)):
                        continue
                    if wet(*pos(*a)) or wet(*pos(*b)):
                        continue
                    art = (axis == "h" and j % ART_J == 0) or (axis == "v" and i % ART_I == 0)
                    if major and not art:
                        continue
                    if not art and _h("edge", i, j, axis) > 0.8:
                        continue
                    if art:
                        name = ARTERIALS[int(_h("an", axis, j if axis == "h" else i) * len(ARTERIALS))]
                        hw = "primary" if axis == "h" else "secondary"
                    elif axis == "h":
                        nth = (j % 40) + 1
                        name = f"{nth}{'th' if 10 <= nth % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(nth % 10, 'th')} Cross St"
                        hw = "residential"
                    else:
                        name = f"{NAMES[(i // 3) % len(NAMES)]} St"
                        hw = "residential"
                    xa, ya = pos(*a)
                    xb, yb = pos(*b)
                    bend = (_h("bend", i, j, axis) - 0.5) * 18
                    mids = []
                    for t_ in (1 / 3, 2 / 3):
                        mx, my = xa + (xb - xa) * t_, ya + (yb - ya) * t_
                        dx, dy = yb - ya, xa - xb
                        L = math.hypot(dx, dy) or 1
                        k = f"m{i}_{j}_{axis}{int(t_ * 3)}"
                        la, lo = _unmerc(mx + dx / L * bend, my + dy / L * bend)
                        nodes[k] = [round(la, 6), round(lo, 6)]
                        mids.append(k)
                    hsub = _h("sub", i, j, axis)
                    ways.append({"id": f"w{i}_{j}_{axis}", "name": ("Railway Subway" if art and hsub < 0.012 else name), "highway": hw,
                                 "nodes": [nid(*a), *mids, nid(*b)], "bridge": False,
                                 "tunnel": bool(art and hsub < 0.012),
                                 "construction": bool(not art and hsub > 0.994)})
        pb = key.padded_bbox()
        PX0, PY0 = _merc(pb[0], pb[1])
        PX1, PY1 = _merc(pb[2], pb[3])
        water = []
        for cx, cy, r in self.lakes_near(PX0, PY0, PX1, PY1):
            if PX0 - r < cx < PX1 + r and PY0 - r < cy < PY1 + r:
                ring = [list(map(lambda v: round(v, 6), _unmerc(cx + r * math.cos(t), cy + r * 0.8 * math.sin(t))))
                        for t in np.linspace(0, 2 * math.pi, 40)]
                water.append(ring)
        locs, pois = [], []
        LL = 1700.0 if not major else 9000.0
        for i in range(int(PX0 // LL), int(PX1 // LL) + 1):
            for j in range(int(PY0 // LL), int(PY1 // LL) + 1):
                x, y = (i + 0.2 + 0.6 * _h("lcx", i, j)) * LL, (j + 0.2 + 0.6 * _h("lcy", i, j)) * LL
                if PX0 <= x <= PX1 and PY0 <= y <= PY1 and not wet(x, y):
                    la, lo = _unmerc(x, y)
                    nm = LOCALITIES[int(_h("ln", i, j) * len(LOCALITIES))]
                    locs.append({"name": f"{nm} {('North', 'South', 'East', 'West', '')[int(_h('ls', i, j) * 5)]}".strip(), "lat": la, "lon": lo})
        if not major:
            PL = 1200.0
            for i in range(int(X0 // PL), int(X1 // PL) + 1):
                for j in range(int(Y0 // PL), int(Y1 // PL) + 1):
                    hh = _h("poi", i, j)
                    if hh < 0.3:
                        x, y = (i + _h("px", i, j)) * PL, (j + _h("py", i, j)) * PL
                        if X0 <= x <= X1 and Y0 <= y <= Y1:
                            la, lo = _unmerc(x, y)
                            kind = "hospital" if hh < 0.12 else "school"
                            nm = f"{NAMES[int(_h('pn', i, j) * len(NAMES))]} {'General Hospital' if kind == 'hospital' else 'Higher Secondary School'}"
                            pois.append({"name": nm, "kind": kind, "lat": la, "lon": lo})
        used = {n_ for w_ in ways for n_ in w_["nodes"]}
        return {"nodes": {k: v for k, v in nodes.items() if k in used}, "ways": ways, "water": water,
                "localities": locs, "pois": pois, "barriers": {}}, "generated"


def make_providers(data_dir: Path):
    cache = net.DiskCache(data_dir / "world")
    if os.getenv("DATA_SOURCE", "osm").lower() == "synthetic":
        p = SyntheticProvider()
        return p, p, cache
    return OSMProvider(cache), TerrainProvider(cache), cache
