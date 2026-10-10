"""Fetch a real area: OpenStreetMap streets + AWS Terrain Tiles elevation.

    python scripts/fetch_area.py                 # Velachery, Chennai (default)
    python scripts/fetch_area.py --id adyar --bbox 12.995,80.240,13.020,80.270 --name "Adyar, Chennai"

Sources (both free, no account):
  - OpenStreetMap via the Overpass API (streets, lakes, localities, hospitals, schools)
  - Terrain Tiles on the AWS Registry of Open Data (s3://elevation-tiles-prod, terrarium
    encoding, SRTM-derived in India). https://registry.opendata.aws/terrain-tiles/

Writes data/<id>/{area.json, dem.npy, streets.json}. The app picks it up on next start.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engine.hydro import _point_in_polys  # noqa: E402

OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
MAJOR = "motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link|secondary_link"
HIGHWAYS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|"
            "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link")
UA = {"User-Agent": "paani-kahan/1.0 (hackathon flood demo)"}


def http(url, data=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def overpass(bbox, major_only=False):
    s, w, n, e = bbox
    b = f"{s},{w},{n},{e}"
    roads = MAJOR if major_only else HIGHWAYS
    q = f"""[out:json][timeout:120];
way["highway"~"^({roads})$"]({b})->.roads;
.roads out body;
.roads >;
out skel qt;
way["highway"="construction"]({b})->.cons;
.cons out body;
.cons >;
out skel qt;
node(w.roads)["barrier"];
out;
way["natural"="water"]({b});
out geom;
way["landuse"="reservoir"]({b});
out geom;
way["natural"="wetland"]({b});
out geom;
node["place"~"^(suburb|neighbourhood|quarter)$"]({b});
out;
nwr["amenity"~"^(hospital|clinic|school)$"]({b});
out center;"""
    last = None
    for url in OVERPASS:
        try:
            print(f"  Overpass: {url}")
            return json.loads(http(url, urllib.parse.urlencode({"data": q}).encode()))
        except Exception as ex:  # noqa: BLE001
            last = ex
            print(f"    failed: {ex}")
            time.sleep(2)
    raise SystemExit(f"All Overpass mirrors failed: {last}")


def parse_osm(osm):
    nodes, ways, water, places, pois, barriers = {}, [], [], [], [], {}
    for el in osm["elements"]:
        if el["type"] == "node" and "lat" in el and not el.get("tags"):
            nodes[str(el["id"])] = [round(el["lat"], 6), round(el["lon"], 6)]
    for el in osm["elements"]:
        t = el.get("tags", {})
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
        elif el["type"] == "way" and "geometry" in el and (t.get("natural") in ("water", "wetland") or t.get("landuse") == "reservoir"):
            poly = [[round(p["lat"], 6), round(p["lon"], 6)] for p in el["geometry"]]
            if len(poly) >= 4:
                water.append(poly)
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
        if el["type"] == "node" and "lat" in el:
            nodes.setdefault(str(el["id"]), [round(el["lat"], 6), round(el["lon"], 6)])
    used = {n for w in ways for n in w["nodes"]}
    nodes = {k: v for k, v in nodes.items() if k in used}
    # localities: prefer suburbs, de-duplicate names, cap at 10
    seen, locs = set(), []
    for p in sorted(places, key=lambda p: {"suburb": 0, "quarter": 1, "neighbourhood": 2}[p["kind"]]):
        if p["name"] not in seen and len(locs) < 10:
            seen.add(p["name"])
            locs.append({"name": p["name"], "lat": p["lat"], "lon": p["lon"]})
    return nodes, ways, water, locs, pois[:40], barriers


def deg2tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def terrarium_dem(bbox, rows, cols, z=14):
    s, w, n, e = bbox
    x0, y0 = deg2tile(n, w, z)
    x1, y1 = deg2tile(s, e, z)
    tiles = {}
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            url = TILES.format(z=z, x=tx, y=ty)
            print(f"  terrain tile {z}/{tx}/{ty}")
            img = np.asarray(Image.open(io.BytesIO(http(url))).convert("RGB")).astype(np.float64)
            tiles[(tx, ty)] = img[..., 0] * 256 + img[..., 1] + img[..., 2] / 256 - 32768
    dem = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        lat = n - (r + 0.5) / rows * (n - s)
        for c in range(cols):
            lon = w + (c + 0.5) / cols * (e - w)
            fx, fy = deg2tile(lat, lon, z)
            tx, ty = int(fx), int(fy)
            px, py = min(int((fx - tx) * 256), 255), min(int((fy - ty) * 256), 255)
            dem[r, c] = tiles[(tx, ty)][py, px]
    return dem


def build(area_id, bbox, name, city, cell_m=20.0, major_only=False, persona=None):
    s, w, n, e = bbox
    lat0 = (s + n) / 2
    rows = int((n - s) * 110_574 / cell_m)
    cols = int((e - w) * 111_320 * math.cos(math.radians(lat0)) / cell_m)
    print(f"[1/3] OpenStreetMap for {name} {bbox}")
    nodes, ways, water, locs, pois, barriers = parse_osm(overpass(bbox, major_only))
    if major_only:  # keep big water bodies only, and the larger places
        water = [w for w in water if len(w) > 30]
    print(f"      {len(ways)} roads ({sum(w.get('tunnel', False) for w in ways)} underpasses, {sum(w.get('construction', False) for w in ways)} under construction), "
          f"{len(barriers)} barriers, {len(water)} water bodies, {len(locs)} localities, {len(pois)} POIs")
    if not ways:
        raise SystemExit("No roads found in that box.")
    print(f"[2/3] AWS Terrain Tiles -> {rows}x{cols} grid at {cell_m:.0f} m")
    dem = terrarium_dem(bbox, rows, cols, z=14 if cell_m <= 25 else 13 if cell_m <= 50 else 12)
    dem = gaussian_filter(dem, 1.2)  # SRTM in cities is noisy (rooftops, trees)
    if water:
        rr, cc = np.mgrid[0:rows, 0:cols]
        lat = n - (rr + 0.5) / rows * (n - s)
        lon = w + (cc + 0.5) / cols * (e - w)
        mask = _point_in_polys(lat, lon, water)
        dem[mask] -= 1.0
    print(f"      elevation {dem.min():.1f} to {dem.max():.1f} m")
    out = ROOT / "data" / area_id
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "dem.npy", dem.astype(np.float32))
    (out / "streets.json").write_text(json.dumps({"nodes": nodes, "ways": ways, "barriers": barriers}))
    meta = {"name": name, "city": city, "synthetic": False, "bbox": list(bbox), "localities": locs, "pois": pois,
            "water": water, "persona": persona, "corridor": major_only, "cell_m": cell_m,
            "sources": ["© OpenStreetMap contributors (ODbL)", "Terrain Tiles, AWS Registry of Open Data (SRTM)"]}
    (out / "area.json").write_text(json.dumps(meta, indent=1))
    print(f"[3/3] Wrote {out}. The demo commute is chosen automatically on first start.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="velachery")
    ap.add_argument("--bbox", default="12.962,80.200,12.996,80.246", help="south,west,north,east")
    ap.add_argument("--name", default="Velachery")
    ap.add_argument("--city", default="Chennai")
    a = ap.parse_args()
    build(a.id, [float(v) for v in a.bbox.split(",")], a.name, a.city)
