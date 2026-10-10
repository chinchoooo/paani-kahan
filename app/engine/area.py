"""Area loading: street network, terrain grid, localities, points of interest.

An area lives in data/<area_id>/ as:
  area.json     metadata, grid definition, localities, POIs, water polygons, persona
  dem.npy       float32 elevation grid in metres (rows x cols, row 0 = north)
  streets.json  {"nodes": {id: [lat, lon]}, "ways": [{id, name, highway, nodes}]}
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

import os
DATA_DIR = Path(os.getenv("PK_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")

M_PER_DEG_LAT = 110_574.0

ROAD_CLASS = {
    "motorway": 0, "trunk": 0, "primary": 0,
    "secondary": 1, "tertiary": 1,
    "unclassified": 2, "residential": 2, "living_street": 2, "service": 2,
}


@dataclass
class Segment:
    idx: int
    u: str
    v: str
    coords: list  # [[lat, lon], ...]
    name: str
    highway: str
    length_m: float
    locality: str = ""
    cells: np.ndarray | None = None  # flat grid indices sampled along the segment
    bridge: bool = False             # flyovers and bridges sit above the water
    underpass: bool = False          # subways: the first places to flood in Chennai
    construction: bool = False       # roads under construction (closed)
    nodes: list = field(default_factory=list)
    gid: str = ""                    # global id across tiles: "<z>/<x>/<y>:<idx>"

    @property
    def mid(self):
        return self.coords[len(self.coords) // 2]


@dataclass
class Area:
    area_id: str
    meta: dict
    dem: np.ndarray
    nodes: dict
    segments: list = field(default_factory=list)
    barriers: dict = field(default_factory=dict)

    # ---- geometry helpers -------------------------------------------------
    @property
    def bbox(self):
        return self.meta["bbox"]  # [south, west, north, east]

    @property
    def lat0(self):
        s, w, n, e = self.bbox
        return (s + n) / 2

    @property
    def m_per_deg_lon(self):
        v = self.__dict__.get("_mlon")
        if v is None:
            v = self.__dict__["_mlon"] = 111_320.0 * math.cos(math.radians(self.lat0))
        return v

    def dist_m(self, a, b):
        dy = (a[0] - b[0]) * M_PER_DEG_LAT
        dx = (a[1] - b[1]) * self.m_per_deg_lon
        return math.hypot(dx, dy)

    def latlon_to_cell(self, lat, lon):
        s, w, n, e = self.bbox
        rows, cols = self.dem.shape
        r = int((n - lat) / (n - s) * rows)
        c = int((lon - w) / (e - w) * cols)
        return min(max(r, 0), rows - 1), min(max(c, 0), cols - 1)

    def cell_to_latlon(self, r, c):
        s, w, n, e = self.bbox
        rows, cols = self.dem.shape
        return n - (r + 0.5) / rows * (n - s), w + (c + 0.5) / cols * (e - w)

    @property
    def cell_m(self):
        s, w, n, e = self.bbox
        rows, cols = self.dem.shape
        return ((n - s) * M_PER_DEG_LAT / rows + (e - w) * self.m_per_deg_lon / cols) / 2

    # ---- locality lookup --------------------------------------------------
    def locality_of(self, lat, lon):
        locs = self.meta.get("localities") or []
        if not locs:
            return "Area"
        best = min(locs, key=lambda L: self.dist_m((lat, lon), (L["lat"], L["lon"])))
        return best["name"]

    def locality_grid(self):
        """Locality index for every grid cell (nearest locality centre)."""
        rows, cols = self.dem.shape
        locs = self.meta.get("localities") or [{"name": "Area", "lat": self.lat0, "lon": 0}]
        pts = np.array([[L["lat"] * M_PER_DEG_LAT, L["lon"] * self.m_per_deg_lon] for L in locs])
        rr, cc = np.mgrid[0:rows, 0:cols]
        s, w, n, e = self.bbox
        lat = n - (rr + 0.5) / rows * (n - s)
        lon = w + (cc + 0.5) / cols * (e - w)
        q = np.stack([lat.ravel() * M_PER_DEG_LAT, lon.ravel() * self.m_per_deg_lon], axis=1)
        _, idx = cKDTree(pts).query(q)
        return idx.reshape(rows, cols)


def _split_ways(nodes: dict, ways: list):
    """Split OSM ways into segments between intersections."""
    use = {}
    for w in ways:
        for i, nid in enumerate(w["nodes"]):
            use[nid] = use.get(nid, 0) + (1 if 0 < i < len(w["nodes"]) - 1 else 2)
    segs = []
    for w in ways:
        ids = [n for n in w["nodes"] if n in nodes]
        if len(ids) < 2:
            continue
        start = 0
        for i in range(1, len(ids)):
            if i == len(ids) - 1 or use.get(ids[i], 0) > 1:
                part = ids[start:i + 1]
                if len(part) >= 2 and part[0] != part[-1]:
                    segs.append((part, w.get("name") or "", w.get("highway") or "residential", bool(w.get("bridge")),
                                 bool(w.get("tunnel")), bool(w.get("construction"))))
                start = i
    return segs


def load_area(area_id: str | None = None) -> Area:
    """Legacy single-area loader (data/<area_id>/), used by scripts and tests."""
    area_id = area_id or "sample"
    d = DATA_DIR / area_id
    if not (d / "area.json").exists():
        if area_id != "sample":
            raise FileNotFoundError(f"No area data in {d}")
        from .sample import make_sample
        make_sample(d)
    meta = json.loads((d / "area.json").read_text())
    dem = np.load(d / "dem.npy").astype(np.float32)
    streets = json.loads((d / "streets.json").read_text())
    return build_area(area_id, meta, dem, streets)


def build_area(area_id: str, meta: dict, dem: np.ndarray, streets: dict, owner_bbox=None, gid_prefix=None) -> Area:
    """Turn streets + terrain into an Area. With owner_bbox, keep only the street segments
    whose midpoint lies in that box: neighbouring tiles fetch the same OSM ways, and this
    gives every segment exactly one owner, so tiles join without duplicates or gaps."""
    nodes = {str(k): tuple(v) for k, v in streets["nodes"].items()}
    ways = [{**w, "nodes": [str(n) for n in w["nodes"]]} for w in streets["ways"]]
    area = Area(area_id=area_id, meta=meta, dem=dem, nodes=nodes)
    rows, cols = dem.shape
    area.barriers = {str(k): v for k, v in (streets.get("barriers") or {}).items()}
    s, w, n, e = area.bbox
    mlon = area.m_per_deg_lon
    for part, name, hw, bridge, tunnel, construction in _split_ways(nodes, ways):
        coords = [list(nodes[nid]) for nid in part]
        mid = coords[len(coords) // 2]
        if owner_bbox and not (owner_bbox[0] <= mid[0] < owner_bbox[2] and owner_bbox[1] <= mid[1] < owner_bbox[3]):
            continue
        a = np.array(coords, dtype=float)
        seglen = np.hypot(np.diff(a[:, 0]) * M_PER_DEG_LAT, np.diff(a[:, 1]) * mlon)
        length = float(seglen.sum())
        if length < 3:
            continue
        idx = len(area.segments)
        seg = Segment(idx=idx, u=part[0], v=part[-1], coords=coords,
                      name=name, highway=hw, length_m=length, bridge=bridge, underpass=tunnel,
                      construction=construction, nodes=part)
        # sample grid cells every ~8 m along the polyline (vectorised)
        k = np.maximum(1, (seglen / 8).astype(np.int64))
        rep = np.repeat(np.arange(len(seglen)), k)
        t = (np.arange(len(rep)) - np.repeat(np.cumsum(k) - k, k) + 1) / np.repeat(k, k)
        p = np.concatenate([a[:1], a[rep] + (a[rep + 1] - a[rep]) * t[:, None]])
        r = np.clip(((n - p[:, 0]) / (n - s) * rows).astype(np.int64), 0, rows - 1)
        c = np.clip(((p[:, 1] - w) / (e - w) * cols).astype(np.int64), 0, cols - 1)
        seg.cells = np.unique(r * cols + c)
        if not seg.name:
            seg.name = f"Unnamed {hw.replace('_', ' ')} road"
        if gid_prefix:
            seg.gid = f"{gid_prefix}:{idx}"
        area.segments.append(seg)
    locs = meta.get("localities") or []
    if locs and area.segments:
        pts = np.array([[L["lat"] * M_PER_DEG_LAT, L["lon"] * mlon] for L in locs])
        q = np.array([[sg.mid[0] * M_PER_DEG_LAT, sg.mid[1] * mlon] for sg in area.segments])
        _, li = cKDTree(pts).query(q)
        for sg, i in zip(area.segments, li):
            sg.locality = locs[int(i)]["name"]
    else:
        for sg in area.segments:
            sg.locality = "Area"
    return area
