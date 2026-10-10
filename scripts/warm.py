"""Pre-load a region into the cache before a demo (optional; the app does this by itself).

    python3 scripts/warm.py                       # Velachery, Chennai
    python3 scripts/warm.py 13.0827 80.2707       # any lat lon
    python3 scripts/warm.py --app http://localhost:8080 12.97 80.22

Tells the running app to focus there, then prints progress until every street-level
tile and every main-road tile is loaded and forecast. Re-runs are instant: tiles are
cached on disk under data/world/.
"""
import argparse
import json
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("lat", nargs="?", type=float, default=12.979)
ap.add_argument("lon", nargs="?", type=float, default=80.223)
ap.add_argument("--app", default="http://localhost:8080")
a = ap.parse_args()


def call(path, body=None):
    req = urllib.request.Request(a.app + path, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"}, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


call("/api/focus", {"lat": a.lat, "lon": a.lon})
t0 = time.time()
while True:
    p = call(f"/api/progress?lat={a.lat}&lon={a.lon}")["progress"]
    d, r = p["detail"], p["region"]
    done_d, done_r = d["ready"] + d["empty"] + d["failed"], r["ready"] + r["empty"] + r["failed"]
    print(f"\r{time.time() - t0:5.0f}s  streets {done_d}/{d['tiles']} (forecast {d['forecast']})  "
          f"main roads {done_r}/{r['tiles']} (forecast {r['forecast']})   ", end="", flush=True)
    if done_d >= d["tiles"] and done_r >= r["tiles"] and d["forecast"] >= d["ready"]:
        print("\nDone. Failed tiles retry by themselves." if d["failed"] + r["failed"] else "\nDone.")
        break
    time.sleep(3)
