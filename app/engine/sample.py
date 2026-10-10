"""Synthetic sample area, used when real OpenStreetMap data has not been fetched.

Everything here is invented: street names, localities and terrain. The UI labels it
"Sample area" so it is never mistaken for a real place. Run scripts/fetch_area.py to
replace it with real Velachery data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

BBOX = [12.965, 80.200, 12.995, 80.245]  # south, west, north, east
CELL_M = 20.0


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def make_sample(out_dir: Path, seed: int = 7):
    rng = np.random.default_rng(seed)
    s, w, n, e = BBOX
    lat0 = (s + n) / 2
    mlat, mlon = 110_574.0, 111_320.0 * math.cos(math.radians(lat0))
    H, W = (n - s) * mlat, (e - w) * mlon
    rows, cols = int(H / CELL_M), int(W / CELL_M)

    # ---- terrain -------------------------------------------------------------
    yy, xx = np.mgrid[0:rows, 0:cols]
    xn, yn = xx / cols, yy / rows  # yn: 0 north -> 1 south

    def bump(cx, cy, sx, sy, amp):
        return amp * np.exp(-(((xn - cx) / sx) ** 2 + ((yn - cy) / sy) ** 2) / 2)

    dem = 9.0 - 3.0 * xn - 2.2 * yn
    dem += bump(0.92, 0.95, 0.16, 0.16, -2.6)       # marsh, south-east
    dem += bump(0.24, 0.33, 0.05, 0.07, -2.4)       # lake
    dem += bump(0.555, 0.50, 0.012, 0.035, -0.5)    # low ground by the railway subway
    dem += bump(0.40, 0.66, 0.03, 0.03, -0.9)       # low colony near station
    for _ in range(10):
        dem += bump(rng.uniform(.05, .95), rng.uniform(.05, .95),
                    rng.uniform(.012, .035), rng.uniform(.012, .035), -rng.uniform(.4, 1.0))
    dem += gaussian_filter(rng.normal(0, 1, (rows, cols)), 6) * 1.6
    dem = dem.astype(np.float32)

    # ---- streets: jittered lattice with arterials -------------------------------
    sp = 140.0
    ni, nj = int(W // sp), int(H // sp)
    pos = {}

    def to_ll(x, y):
        return [round(n - y / mlat, 6), round(w + x / mlon, 6)]

    lake_c, lake_r = (0.24 * W, 0.33 * H), 230
    marsh_c, marsh_r = (0.97 * W, 1.0 * H), 700
    for i in range(ni):
        for j in range(nj):
            x = 70 + i * sp + rng.uniform(-28, 28)
            y = 70 + j * sp + rng.uniform(-28, 28)
            if math.dist((x, y), lake_c) < lake_r or math.dist((x, y), marsh_c) < marsh_r:
                continue
            pos[(i, j)] = (x, y)

    art_rows = {5: "Lake View Rd", 12: "Station Rd", 19: "Old Town Rd"}
    art_cols = {9: "Temple St", 19: "Canal Rd", 28: "Tech Park Rd"}
    street_names = ["Gandhi", "Nehru", "Bharathi", "Kamarajar", "Periyar", "Anna", "Vivekananda",
                    "Tagore", "Patel", "Thiru Vi Ka", "Rajaji", "Ambedkar", "Kalki", "Pari", "Valluvar"]
    nodes, ways = {}, []
    sid = [0]

    def nid(key):
        k = f"n{key[0]}_{key[1]}"
        nodes[k] = to_ll(*pos[key])
        return k

    def add_way(a, b, name, hw):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        ids = [nid(a)]
        bend = rng.uniform(-9, 9)
        for t in (1 / 3, 2 / 3):
            mx, my = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            dx, dy = y2 - y1, x1 - x2
            L = math.hypot(dx, dy) or 1
            k = f"s{sid[0]}"
            sid[0] += 1
            nodes[k] = to_ll(mx + dx / L * bend, my + dy / L * bend)
            ids.append(k)
        ids.append(nid(b))
        ways.append({"id": len(ways), "name": name, "highway": hw, "nodes": ids})

    for (i, j) in list(pos):
        for (di, dj) in ((1, 0), (0, 1)):
            b = (i + di, j + dj)
            if b not in pos:
                continue
            if dj == 0 and j in art_rows:
                add_way((i, j), b, art_rows[j], "primary")
            elif di == 0 and i in art_cols:
                add_way((i, j), b, art_cols[i], "secondary")
            elif rng.random() < 0.80:
                if dj == 0:
                    add_way((i, j), b, f"{_ordinal(j + 1)} Cross St", "residential")
                else:
                    add_way((i, j), b, f"{street_names[i % len(street_names)]} St", "residential")
    # the main road dips under the railway here: a subway (matches the low strip in the terrain)
    ux, uy = 0.555 * W, 0.50 * H
    def mid_xy(w):
        a, b = nodes[w["nodes"][0]], nodes[w["nodes"][-1]]
        la, lo = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return (lo - (w_ := BBOX[1])) * mlon, (BBOX[2] - la) * mlat
    best = min((wy for wy in ways if wy["highway"] in ("primary", "secondary")), key=lambda wy: math.dist(mid_xy(wy), (ux, uy)))
    best["tunnel"] = True
    best["name"] = "Railway Subway"
    # one street closed for drain works
    cons = min((wy for wy in ways if wy["highway"] == "residential"), key=lambda wy: math.dist(mid_xy(wy), (0.35 * W, 0.42 * H)))
    cons["construction"] = True
    # a diagonal link road
    i, j = 3, 21
    while (i + 1, j - 1) in pos and (i, j) in pos and j > 13:
        add_way((i, j), (i + 1, j - 1), "Marsh Link Rd", "tertiary")
        i, j = i + 1, j - 1

    # ---- localities, POIs, water --------------------------------------------
    def ll(fx, fy):
        return to_ll(fx * W, fy * H)

    localities = [{"name": nm, "lat": ll(fx, fy)[0], "lon": ll(fx, fy)[1]} for nm, fx, fy in [
        ("Lakeside", .22, .25), ("Station Colony", .42, .64), ("Marsh End", .78, .82),
        ("Tech Park", .84, .22), ("Old Town", .14, .80), ("Canal Side", .56, .40)]]
    keys = list(pos)
    pois = []
    for k, (nm, kind) in enumerate([("City General Hospital", "hospital"), ("Lakeside Clinic", "hospital"),
                                    ("Marsh End Health Centre", "hospital"), ("St. Mary's School", "school"),
                                    ("Govt. Higher Secondary School", "school"), ("Canal Side Primary School", "school"),
                                    ("Station Colony School", "school"), ("Tech Park Creche", "school")]):
        key = keys[rng.integers(len(keys))]
        lat, lon = to_ll(*pos[key])
        pois.append({"name": nm, "kind": kind, "lat": lat, "lon": lon})

    def circle(c, r, npts=48):
        return [to_ll(c[0] + r * math.cos(t), c[1] + r * math.sin(t))
                for t in np.linspace(0, 2 * math.pi, npts)]

    meta = {
        "name": "Sample area", "city": "synthetic", "synthetic": True, "bbox": BBOX,
        "localities": localities, "pois": pois,
        "water": [circle(lake_c, lake_r - 40), circle(marsh_c, marsh_r - 60, 64)],
        "persona": None,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "dem.npy", dem)
    (out_dir / "streets.json").write_text(json.dumps({"nodes": nodes, "ways": ways}))
    (out_dir / "area.json").write_text(json.dumps(meta, indent=1))
    return out_dir
