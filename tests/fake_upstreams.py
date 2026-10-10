"""Fake Overpass, AWS Terrain Tiles and Open-Meteo for end-to-end tests without the internet.

    python tests/fake_upstreams.py 8765
    OVERPASS_URLS=http://127.0.0.1:8765/overpass TERRAIN_URL=http://127.0.0.1:8765/terrarium/{z}/{x}/{y}.png \
    OPEN_METEO_URL=http://127.0.0.1:8765/forecast uvicorn app.server:app

It serves the synthetic world in the real wire formats (Overpass JSON elements, terrarium
PNGs, Open-Meteo multi-location JSON), so the production code paths run unchanged.
"""
import datetime as dt
import io
import json
import math
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.world.providers import SyntheticProvider  # noqa: E402
from app.world.tiles import tile_of  # noqa: E402

SYN = SyntheticProvider()
CALLS = {"overpass": 0, "terrain": 0, "meteo": 0}


def overpass(q):
    b = [float(v) for v in re.search(r'way\["highway"~"\^\(([^)]*)\)\$"\]\(([^)]*)\)', q).group(2).split(",")]
    major = "residential" not in q
    lat, lon = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    key = tile_of(lat, lon, 10 if major else 13)
    data, _ = SYN.osm(key)
    els = []
    for k, (la, lo) in data["nodes"].items():
        els.append({"type": "node", "id": k, "lat": la, "lon": lo})
    for w in data["ways"]:
        tags = {"highway": "construction" if w["construction"] else w["highway"], "name": w["name"]}
        if w["construction"]:
            tags["construction"] = w["highway"]
        if w["tunnel"]:
            tags["tunnel"] = "yes"
        els.append({"type": "way", "id": w["id"], "nodes": w["nodes"], "tags": tags})
    for i, poly in enumerate(data["water"]):
        els.append({"type": "way", "id": f"water{i}", "tags": {"natural": "water"}, "geometry": [{"lat": a, "lon": o} for a, o in poly]})
    for i, L in enumerate(data["localities"]):
        els.append({"type": "node", "id": f"pl{i}", "lat": L["lat"], "lon": L["lon"], "tags": {"place": "suburb", "name": L["name"]}})
    for i, p in enumerate(data["pois"]):
        els.append({"type": "node", "id": f"poi{i}", "lat": p["lat"], "lon": p["lon"], "tags": {"amenity": p["kind"], "name": p["name"]}})
    return {"elements": els}


def terrarium(z, x, y):
    n = 2 ** z
    lat_of = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))  # noqa: E731
    bbox = [lat_of(y + 1), x / n * 360 - 180, lat_of(y), (x + 1) / n * 360 - 180]
    dem, _ = SYN.dem(bbox, 256, 256, 10)
    v = dem.astype(np.float64) + 32768
    r = np.floor(v / 256)
    g = np.floor(v - r * 256)
    b = np.round((v - r * 256 - g) * 256).clip(0, 255)
    img = Image.fromarray(np.stack([r, g, b], -1).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def forecast(qs):
    lats = [float(v) for v in qs["latitude"][0].split(",")]
    lons = [float(v) for v in qs["longitude"][0].split(",")]
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    now -= dt.timedelta(minutes=now.minute % 15)
    times = [(now + dt.timedelta(minutes=15 * k)).strftime("%Y-%m-%dT%H:%M") for k in range(-12, 28)]
    out = []
    for la, lo in zip(lats, lons):
        # a storm peaking ~2.5 h from now, heavier to the south-east
        vals = [round(max(0.0, 14 * math.exp(-((k - 10) / 4.5) ** 2) * (1 + 0.6 * math.sin(la * 40 + lo * 30))), 2) for k in range(-12, 28)]
        out.append({"latitude": la, "longitude": lo, "minutely_15": {"time": times, "precipitation": vals}})
    return out[0] if len(out) == 1 else out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = parse_qs(self.rfile.read(n).decode())
        if self.path.startswith("/overpass"):
            CALLS["overpass"] += 1
            return self._send(200, overpass(body["data"][0]))
        self._send(404, {})

    def do_GET(self):
        u = urlparse(self.path)
        m = re.match(r"/terrarium/(\d+)/(\d+)/(\d+)\.png", u.path)
        if m:
            CALLS["terrain"] += 1
            return self._send(200, terrarium(*map(int, m.groups())), "image/png")
        if u.path == "/forecast":
            CALLS["meteo"] += 1
            qs = parse_qs(u.query)
            if "minutely_15" in qs:
                return self._send(200, forecast(qs))
            return self._send(400, {"error": True, "reason": "fake: only minutely_15"})
        if u.path == "/calls":
            return self._send(200, CALLS)
        self._send(404, {"error": True})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"fake upstreams on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
