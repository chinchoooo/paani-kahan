"""Tile math (Web Mercator / slippy-map scheme) and the two levels of detail."""
from __future__ import annotations

import math
from dataclasses import dataclass

M_PER_DEG_LAT = 110_574.0


@dataclass(frozen=True)
class Lod:
    name: str
    z: int
    cell_m: float      # flood-model grid resolution
    halo: float        # extra margin around the tile for hydrology, as a fraction of its size
    major_only: bool   # only main roads (motorway..secondary)


DETAIL = Lod("detail", 13, 20.0, 0.15, False)   # ~4.8 km tiles at Chennai's latitude, every street
REGION = Lod("region", 10, 150.0, 0.08, True)   # ~38 km tiles, main roads, for 100 km around you
LODS = {DETAIL.z: DETAIL, REGION.z: REGION}


@dataclass(frozen=True, order=True)
class TileKey:
    z: int
    x: int
    y: int

    @property
    def lod(self) -> Lod:
        return LODS[self.z]

    @property
    def path(self) -> str:
        return f"{self.z}/{self.x}/{self.y}"

    def __str__(self):
        return self.path

    @classmethod
    def parse(cls, s: str) -> "TileKey":
        z, x, y = (int(v) for v in s.replace("-", "/").split("/"))
        if z not in LODS:
            raise ValueError(f"unsupported zoom {z}")
        n = 2 ** z
        if not (0 <= x < n and 0 <= y < n):
            raise ValueError("tile out of range")
        return cls(z, x, y)

    @property
    def bbox(self):
        """[south, west, north, east] in degrees."""
        n = 2 ** self.z
        w = self.x / n * 360 - 180
        e = (self.x + 1) / n * 360 - 180
        north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * self.y / n))))
        south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (self.y + 1) / n))))
        return [south, w, north, e]

    def padded_bbox(self, frac=None):
        s, w, n, e = self.bbox
        f = self.lod.halo if frac is None else frac
        dy, dx = (n - s) * f, (e - w) * f
        return [s - dy, w - dx, n + dy, e + dx]

    @property
    def center(self):
        s, w, n, e = self.bbox
        return ((s + n) / 2, (w + e) / 2)

    def parent(self, z: int) -> "TileKey":
        d = self.z - z
        return TileKey(z, self.x >> d, self.y >> d)

    def neighbours(self, r=1):
        n = 2 ** self.z
        return [TileKey(self.z, (self.x + dx) % n, self.y + dy)
                for dy in range(-r, r + 1) for dx in range(-r, r + 1)
                if (dx or dy) and 0 <= self.y + dy < n]


def tile_of(lat: float, lon: float, z: int) -> TileKey:
    n = 2 ** z
    lat = max(min(lat, 85.0), -85.0)
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return TileKey(z, min(max(x, 0), n - 1), min(max(y, 0), n - 1))


def km_between(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def tiles_in_bbox(bbox, z: int):
    s, w, n, e = bbox
    a, b = tile_of(n, w, z), tile_of(s, e, z)
    return [TileKey(z, x, y) for y in range(a.y, b.y + 1) for x in range(a.x, b.x + 1)]


def bbox_around(lat, lon, km):
    dlat = km / (M_PER_DEG_LAT / 1000)
    dlon = km / (111.320 * math.cos(math.radians(lat)))
    return [lat - dlat, lon - dlon, lat + dlat, lon + dlon]


def tiles_within(lat: float, lon: float, km: float, z: int):
    """Tiles whose nearest point is within km of (lat, lon), nearest first."""
    out = []
    for k in tiles_in_bbox(bbox_around(lat, lon, km), z):
        s, w, n, e = k.bbox
        near = (min(max(lat, s), n), min(max(lon, w), e))
        d = km_between((lat, lon), near)
        if d <= km:
            out.append((d, km_between((lat, lon), k.center), k))
    out.sort()
    return [k for _, _, k in out]


def bbox_intersects(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def in_bbox(lat, lon, b):
    return b[0] <= lat <= b[2] and b[1] <= lon <= b[3]
