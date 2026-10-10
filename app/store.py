"""Search and analytics store, per tile.

OpenSearch holds what the app searches and aggregates across the whole region:

  pk-places    every named street, locality, hospital and school (search_as_you_type,
               ranked by text match and distance from you with a gauss decay)
  pk-forecast  one document per wet street segment: d0..d36 = depth at each 10-minute
               frame, peak, locality, geo_point. "Streets over 15 cm within 1.2 km at
               6 PM" is a geo_distance filter + range on d11, sorted by d11. Tiles are
               re-indexed independently as their forecasts land.
  pk-reports   flood history: documented closures, resident reports (and seed data in
               demo mode), full-text + geo
  pk-pois      hospitals and schools, for 'which sites lose road access'

Every query the app runs is recorded with its DSL and timing so the UI can show it.
If OpenSearch is not reachable, MemoryStore answers the same questions from the
in-memory tiles and the UI says 'OpenSearch offline'.
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import deque

P = os.getenv("OPENSEARCH_INDEX_PREFIX", "pk")
IDX = {k: f"{P}-{k}" for k in ("forecast", "reports", "pois", "places")}
QUERY_LOG: deque = deque(maxlen=40)
NF = 37  # frames: 0..360 min in 10-min steps


def _log(engine, op, query, ms, hits):
    QUERY_LOG.appendleft({"engine": engine, "op": op, "query": query, "ms": round(ms, 1),
                          "hits": hits, "at": time.strftime("%H:%M:%S")})


def _haversine(a_lat, a_lon, b_lat, b_lon):
    r = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def frame(t):
    return int(min(max(round(t / 10), 0), NF - 1))


def tile_places(tile):
    """Searchable places in one tile: each named street once (its longest piece), own localities, POIs."""
    a = tile.area
    best = {}
    for sg in a.segments:
        if sg.name.startswith("Unnamed"):
            continue
        if sg.name not in best or sg.length_m > best[sg.name].length_m:
            best[sg.name] = sg
    lod = tile.key.lod.name
    out = [{"name": n, "kind": "street", "sub": sg.locality, "lat": sg.mid[0], "lon": sg.mid[1], "seg": sg.gid, "lod": lod}
           for n, sg in best.items()]
    tb = tile.key.bbox
    out += [{"name": L["name"], "kind": "locality", "sub": "Locality", "lat": L["lat"], "lon": L["lon"], "seg": None, "lod": lod}
            for L in a.meta.get("localities") or [] if tb[0] <= L["lat"] < tb[2] and tb[1] <= L["lon"] < tb[3]]
    out += [{"name": p["name"], "kind": p["kind"], "sub": a.locality_of(p["lat"], p["lon"]), "lat": p["lat"], "lon": p["lon"], "seg": None, "lod": lod}
            for p in a.meta.get("pois") or []]
    return out


def q_places(text, near=None, size=8):
    fns = [{"weight": 1.0},  # base, so an exact match far away still ranks
           {"filter": {"terms": {"kind": ["locality", "hospital", "school"]}}, "weight": 1.5},
           {"filter": {"term": {"lod": "region"}}, "weight": 0.7}]
    if near:
        fns.append({"gauss": {"location": {"origin": {"lat": near[0], "lon": near[1]}, "scale": "4km", "decay": 0.5}}, "weight": 2})
    return {"size": size * 3, "query": {"function_score": {
        "query": {"bool": {"should": [
            {"multi_match": {"query": text, "type": "bool_prefix", "fields": ["name", "name._2gram", "name._3gram"]}},
            {"match": {"name": {"query": text, "fuzziness": "AUTO", "boost": 0.6}}}]}},
        "functions": fns, "score_mode": "sum", "boost_mode": "multiply"}}}


def q_nearby(t, lat, lon, radius_m, min_depth, size):
    f = f"d{frame(t)}"
    return {"size": size * 3, "_source": ["gid", "name", "locality", "mid", f], "query": {"bool": {"filter": [
        {"term": {"lod": "detail"}}, {"range": {f: {"gte": min_depth}}},
        {"geo_distance": {"distance": f"{int(radius_m)}m", "mid": {"lat": lat, "lon": lon}}}]}},
        "sort": [{f: "desc"}]}


def _bbox_filter(bbox):
    s, w, n, e = bbox
    return {"geo_bounding_box": {"mid": {"top_left": {"lat": n, "lon": w}, "bottom_right": {"lat": s, "lon": e}}}}


def q_flooded(t, min_depth, locality=None, bbox=None, size=50):
    f = f"d{frame(t)}"
    flt = [{"term": {"lod": "detail"}}, {"range": {f: {"gte": min_depth}}}]
    if locality:
        flt.append({"term": {"locality": locality}})
    if bbox:
        flt.append(_bbox_filter(bbox))
    return {"size": size, "_source": ["gid", "name", "locality", f], "query": {"bool": {"filter": flt}}, "sort": [{f: "desc"}]}


def q_locality_summary(t, bbox):
    f = f"d{frame(t)}"
    return {"size": 0, "query": {"bool": {"filter": [{"term": {"lod": "detail"}}, _bbox_filter(bbox), {"range": {f: {"gte": 5}}}]}},
            "aggs": {"by_locality": {"terms": {"field": "locality", "size": 60},
                                     "aggs": {"max_depth": {"max": {"field": f}},
                                              "impassable": {"filter": {"range": {f: {"gte": 30}}}},
                                              "worst": {"top_hits": {"size": 1, "sort": [{f: "desc"}],
                                                                     "_source": ["name", f, "gid"]}}}}}}


def q_reports(text, lat=None, lon=None, radius_m=500, size=5):
    must = [{"multi_match": {"query": text, "fields": ["text", "street^2"], "fuzziness": "AUTO"}}] if text else [{"match_all": {}}]
    flt = []
    if lat is not None:
        flt.append({"geo_distance": {"distance": f"{int(radius_m)}m", "location": {"lat": lat, "lon": lon}}})
    return {"size": size, "query": {"bool": {"must": must, "filter": flt}}, "sort": ["_score", {"date": "desc"}]}


def q_near_poi(t, lat, lon, min_depth, radius_m):
    f = f"d{frame(t)}"
    return {"size": 1, "_source": ["name", f], "query": {"bool": {"filter": [
        {"term": {"lod": "detail"}}, {"range": {f: {"gte": min_depth}}},
        {"geo_distance": {"distance": f"{int(radius_m)}m", "mid": {"lat": lat, "lon": lon}}}]}},
        "sort": [{f: "desc"}]}


def _dedupe(rows, size):
    seen, out = set(), []
    for r in rows:
        if r["name"] not in seen:
            seen.add(r["name"])
            out.append(r)
    return out[:size]


def _report_doc(r):
    return {**{k: r.get(k) for k in ("text", "street", "locality", "date", "depth_cm_reported", "source", "seg")},
            "url": r.get("url"), "location": {"lat": r["lat"], "lon": r["lon"]}}


class OpenSearchStore:
    engine = "opensearch"

    def __init__(self, url):
        from opensearchpy import OpenSearch
        self.url = url
        self.c = OpenSearch(url, timeout=30, verify_certs=False, ssl_show_warn=False)
        self.c.info()
        self.pois = {}

    def _search(self, op, index, body):
        t = time.perf_counter()
        r = self.c.search(index=index, body=body)
        _log(self.engine, op, body, (time.perf_counter() - t) * 1000, r["hits"]["total"]["value"])
        return r

    def setup(self):
        fc = {"gid": {"type": "keyword"}, "tile": {"type": "keyword"}, "lod": {"type": "keyword"}, "run_id": {"type": "keyword"},
              "ver": {"type": "long"}, "name": {"type": "keyword"}, "locality": {"type": "keyword"}, "peak": {"type": "short"},
              "mid": {"type": "geo_point"}, **{f"d{i}": {"type": "short"} for i in range(NF)}}
        maps = {
            "forecast": fc,
            "reports": {"text": {"type": "text"}, "street": {"type": "text", "fields": {"k": {"type": "keyword"}}},
                        "locality": {"type": "keyword"}, "date": {"type": "date"},
                        "depth_cm_reported": {"type": "integer"}, "location": {"type": "geo_point"},
                        "source": {"type": "keyword"}, "seg": {"type": "keyword"}, "url": {"type": "keyword", "index": False}},
            "pois": {"name": {"type": "text"}, "kind": {"type": "keyword"}, "tile": {"type": "keyword"}, "locality": {"type": "keyword"},
                     "location": {"type": "geo_point"}},
            "places": {"name": {"type": "search_as_you_type"}, "kind": {"type": "keyword"}, "sub": {"type": "keyword"},
                       "seg": {"type": "keyword"}, "tile": {"type": "keyword"}, "lod": {"type": "keyword"}, "location": {"type": "geo_point"}},
        }
        for k, props in maps.items():
            if self.c.indices.exists(index=IDX[k]):
                self.c.indices.delete(index=IDX[k])
            self.c.indices.create(index=IDX[k], body={"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "2s"},
                                                       "mappings": {"properties": props}})

    def index_tile(self, tile):
        from opensearchpy import helpers
        key = str(tile.key)
        docs = [{"_index": IDX["places"], "_id": f"{key}#{i}", "_source": {
            "name": p["name"], "kind": p["kind"], "sub": p["sub"], "seg": p["seg"], "tile": key, "lod": p["lod"],
            "location": {"lat": p["lat"], "lon": p["lon"]}}} for i, p in enumerate(tile_places(tile))]
        for i, p in enumerate(tile.area.meta.get("pois") or []):
            docs.append({"_index": IDX["pois"], "_id": f"{key}#{i}", "_source": {
                "name": p["name"], "kind": p["kind"], "tile": key, "locality": tile.area.locality_of(p["lat"], p["lon"]),
                "location": {"lat": p["lat"], "lon": p["lon"]}}})
            self.pois[f"{key}#{i}"] = p
        docs += [{"_index": IDX["reports"], "_id": r["id"], "_source": _report_doc(r)} for r in tile.documented]
        t0 = time.perf_counter()
        n, _ = helpers.bulk(self.c, docs, refresh=False)
        _log(self.engine, f"index tile {key}", {"places+pois+history": n}, (time.perf_counter() - t0) * 1000, n)

    def index_forecast(self, tile, sim):
        if sim is None:
            return 0
        from opensearchpy import helpers
        key = str(tile.key)
        ver = int(time.time() * 1000)
        sd = sim.seg_depth
        peak = sd.max(axis=0) if sd.shape[1] else []
        segs = tile.area.segments

        def gen():
            for i in (peak >= 5).nonzero()[0] if len(peak) else []:
                s = segs[i]
                col = sd[:, i]
                yield {"_index": IDX["forecast"], "_id": s.gid, "_source": {
                    "gid": s.gid, "tile": key, "lod": tile.key.lod.name, "run_id": sim.run_id, "ver": ver, "name": s.name,
                    "locality": s.locality, "peak": int(peak[i]), "mid": {"lat": s.mid[0], "lon": s.mid[1]},
                    **{f"d{f}": int(col[f]) for f in range(min(NF, len(col)))}}}
        t0 = time.perf_counter()
        n, _ = helpers.bulk(self.c, gen(), chunk_size=2000, refresh=True)
        self.c.delete_by_query(index=IDX["forecast"], body={"query": {"bool": {"filter": [{"term": {"tile": key}}, {"range": {"ver": {"lt": ver}}}]}}},
                               refresh=True, conflicts="proceed")
        _log(self.engine, f"index forecast {key}", {"run_id": sim.run_id, "wet_segments": n}, (time.perf_counter() - t0) * 1000, n)
        return n

    def add_reports(self, reports):
        from opensearchpy import helpers
        if reports:
            helpers.bulk(self.c, [{"_index": IDX["reports"], "_id": r["id"], "_source": _report_doc(r)} for r in reports], refresh=True)

    def add_report(self, r):
        t0 = time.perf_counter()
        self.c.index(index=IDX["reports"], id=r["id"], body=_report_doc(r), refresh="wait_for")
        _log(self.engine, "index resident report", _report_doc(r), (time.perf_counter() - t0) * 1000, 1)

    def search_places(self, text, near=None, size=8):
        r = self._search("place search (search_as_you_type + distance decay)", IDX["places"], q_places(text, near, size))
        rows = [{"name": h["_source"]["name"], "kind": h["_source"]["kind"], "sub": h["_source"]["sub"],
                 "seg": h["_source"]["seg"], "lat": h["_source"]["location"]["lat"], "lon": h["_source"]["location"]["lon"]}
                for h in r["hits"]["hits"]]
        return _dedupe(rows, size)

    def nearby(self, t, lat, lon, radius_m=1200, min_depth=15, size=6):
        f = f"d{frame(t)}"
        r = self._search("flooding near you (geo_distance)", IDX["forecast"], q_nearby(t, lat, lon, radius_m, min_depth, size))
        rows = [{"seg_id": h["_source"]["gid"], "name": h["_source"]["name"], "locality": h["_source"]["locality"],
                 "depth_cm": h["_source"][f]} for h in r["hits"]["hits"]]
        return _dedupe(rows, size)

    def flooded_segments(self, t, min_depth=30, locality=None, bbox=None, size=50):
        f = f"d{frame(t)}"
        r = self._search("flooded streets", IDX["forecast"], q_flooded(t, min_depth, locality, bbox, size))
        return [{"seg_id": h["_source"]["gid"], "name": h["_source"]["name"], "locality": h["_source"]["locality"],
                 "depth_cm": h["_source"][f]} for h in r["hits"]["hits"]]

    def locality_summary(self, t, bbox):
        f = f"d{frame(t)}"
        r = self._search("locality summary (aggregation)", IDX["forecast"], q_locality_summary(t, bbox))
        out = []
        for b in r["aggregations"]["by_locality"]["buckets"]:
            w = b["worst"]["hits"]["hits"]
            out.append({"locality": b["key"], "segments_wet": b["doc_count"], "impassable": b["impassable"]["doc_count"],
                        "max_depth": int(b["max_depth"]["value"] or 0),
                        "worst": {"name": w[0]["_source"]["name"], "depth_cm": w[0]["_source"][f], "seg_id": w[0]["_source"]["gid"]} if w else None})
        return sorted(out, key=lambda x: (-x["impassable"], -x["max_depth"]))

    def search_reports(self, text, lat=None, lon=None, radius_m=500, size=5):
        r = self._search("flood history search", IDX["reports"], q_reports(text, lat, lon, radius_m, size))
        return [h["_source"] | {"score": h["_score"]} for h in r["hits"]["hits"]]

    def pois_at_risk(self, t, bbox, min_depth=30, radius_m=200):
        pois = [p for p in self.pois.values() if bbox[0] <= p["lat"] <= bbox[2] and bbox[1] <= p["lon"] <= bbox[3]][:60]
        body = []
        for p in pois:
            body += [{"index": IDX["forecast"]}, q_near_poi(t, p["lat"], p["lon"], min_depth, radius_m)]
        if not body:
            return []
        t0 = time.perf_counter()
        r = self.c.msearch(body=body)
        _log(self.engine, f"vulnerable sites (msearch x{len(pois)})", body[1], (time.perf_counter() - t0) * 1000, len(pois))
        f = f"d{frame(t)}"
        out = []
        for p, resp in zip(pois, r["responses"]):
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                out.append({**p, "street": hits[0]["_source"]["name"], "depth": hits[0]["_source"][f]})
        return sorted(out, key=lambda x: -x["depth"])


class MemoryStore:
    """Same questions, answered from the in-memory tiles (when OpenSearch is down)."""
    engine = "memory"

    def __init__(self, world):
        self.world = world
        self.reports = []

    def setup(self):
        pass

    def index_tile(self, tile):
        _log(self.engine, f"tile {tile.key} in memory", {}, 0, len(tile.area.segments))

    def index_forecast(self, tile, sim):
        return 0

    def add_reports(self, reports):
        self.reports += reports

    def add_report(self, r):
        self.reports.append(r)
        _log(self.engine, "index resident report", {"street": r["street"], "depth_cm": r["depth_cm_reported"]}, 0, 1)

    def _tiles(self, lod="detail"):
        return [t for t in list(self.world.tiles.values()) if t.status == "ready" and t.key.lod.name == lod]

    def search_places(self, text, near=None, size=8):
        t0 = time.perf_counter()
        q = (text or "").lower().strip()
        out = []
        for t in self._tiles("detail") + self._tiles("region"):
            for pl in tile_places(t):
                n = pl["name"].lower()
                sc = 3 if n.startswith(q) else 2 if any(w.startswith(q) for w in n.split()) else 1 if q in n else 0
                if not sc:
                    continue
                sc *= 1.5 if pl["kind"] != "street" else 1
                if near:
                    sc *= 1 + 2 * math.exp(-(_haversine(near[0], near[1], pl["lat"], pl["lon"]) / 4000) ** 2)
                out.append((sc, pl))
        out.sort(key=lambda x: -x[0])
        _log(self.engine, "place search", q_places(text, near, size), (time.perf_counter() - t0) * 1000, len(out))
        return _dedupe([pl for _, pl in out], size)

    def _rows(self, t, min_depth, bbox=None):
        f = frame(t)
        rows = []
        for tl in self._tiles():
            if bbox and not (tl.key.bbox[0] <= bbox[2] and tl.key.bbox[2] >= bbox[0] and tl.key.bbox[1] <= bbox[3] and tl.key.bbox[3] >= bbox[1]):
                continue
            fc, _ = tl.latest_fc(self.world.rain.run.id)
            if fc is None or not fc.seg_depth.shape[1]:
                continue
            row = fc.seg_depth[min(f, fc.seg_depth.shape[0] - 1)]
            segs = tl.area.segments
            for i in (row >= min_depth).nonzero()[0]:
                s = segs[i]
                rows.append({"seg_id": s.gid, "name": s.name, "locality": s.locality, "depth_cm": int(row[i]), "mid": s.mid})
        return rows

    def nearby(self, t, lat, lon, radius_m=1200, min_depth=15, size=6):
        t0 = time.perf_counter()
        from .world.tiles import bbox_around
        rows = [r for r in self._rows(t, min_depth, bbox_around(lat, lon, radius_m / 1000)) if _haversine(lat, lon, *r["mid"]) <= radius_m]
        rows.sort(key=lambda r: -r["depth_cm"])
        _log(self.engine, "flooding near you (geo_distance)", q_nearby(t, lat, lon, radius_m, min_depth, size), (time.perf_counter() - t0) * 1000, len(rows))
        return _dedupe(rows, size)

    def flooded_segments(self, t, min_depth=30, locality=None, bbox=None, size=50):
        t0 = time.perf_counter()
        rows = [r for r in self._rows(t, min_depth, bbox) if (not locality or r["locality"] == locality)
                and (not bbox or (bbox[0] <= r["mid"][0] <= bbox[2] and bbox[1] <= r["mid"][1] <= bbox[3]))]
        rows.sort(key=lambda r: -r["depth_cm"])
        _log(self.engine, "flooded streets", q_flooded(t, min_depth, locality, bbox, size), (time.perf_counter() - t0) * 1000, len(rows))
        return rows[:size]

    def locality_summary(self, t, bbox):
        t0 = time.perf_counter()
        agg = {}
        for r in self._rows(t, 5, bbox):
            if not (bbox[0] <= r["mid"][0] <= bbox[2] and bbox[1] <= r["mid"][1] <= bbox[3]):
                continue
            a = agg.setdefault(r["locality"], {"locality": r["locality"], "segments_wet": 0, "impassable": 0, "max_depth": 0, "worst": None})
            a["segments_wet"] += 1
            a["impassable"] += r["depth_cm"] >= 30
            if r["depth_cm"] > a["max_depth"]:
                a["max_depth"], a["worst"] = r["depth_cm"], {"name": r["name"], "depth_cm": r["depth_cm"], "seg_id": r["seg_id"]}
        _log(self.engine, "locality summary (aggregation)", q_locality_summary(t, bbox), (time.perf_counter() - t0) * 1000, len(agg))
        return sorted(agg.values(), key=lambda x: (-x["impassable"], -x["max_depth"]))

    def search_reports(self, text, lat=None, lon=None, radius_m=500, size=5):
        t0 = time.perf_counter()
        words = [w for w in (text or "").lower().split() if len(w) > 2]
        out = []
        for r in self.world.all_reports() + self.reports:
            if lat is not None and _haversine(lat, lon, r["lat"], r["lon"]) > radius_m:
                continue
            hay = (r["text"] + " " + (r.get("street") or "") * 2).lower()
            score = sum(hay.count(w) for w in words) if words else 1
            if score:
                out.append({**_report_doc(r), "score": score})
        out.sort(key=lambda r: (r["score"], r["date"] or ""), reverse=True)
        seen, uniq = set(), []
        for r in out:
            if r["text"] not in seen:
                seen.add(r["text"])
                uniq.append(r)
        _log(self.engine, "flood history search", q_reports(text, lat, lon, radius_m, size), (time.perf_counter() - t0) * 1000, len(out))
        return uniq[:size]

    def pois_at_risk(self, t, bbox, min_depth=30, radius_m=200):
        t0 = time.perf_counter()
        rows = self._rows(t, min_depth, bbox)
        out = []
        for tl in self._tiles():
            for p in tl.area.meta.get("pois") or []:
                if not (bbox[0] <= p["lat"] <= bbox[2] and bbox[1] <= p["lon"] <= bbox[3]):
                    continue
                near = [r for r in rows if abs(r["mid"][0] - p["lat"]) < 0.003 and _haversine(p["lat"], p["lon"], *r["mid"]) <= radius_m]
                if near:
                    w = max(near, key=lambda r: r["depth_cm"])
                    out.append({**p, "street": w["name"], "depth": w["depth_cm"]})
        _log(self.engine, "vulnerable sites", {}, (time.perf_counter() - t0) * 1000, len(out))
        return sorted(out, key=lambda x: -x["depth"])


class StoreManager:
    """Starts on the in-memory store, connects to OpenSearch in the background, then
    switches over and re-indexes every tile already loaded. The app never waits on it."""

    def __init__(self, world, url, wait_s=180.0):
        self.world = world
        self.url = url
        self.warning = None
        world.store = MemoryStore(world)
        if url:
            threading.Thread(target=self._connect, args=(wait_s,), daemon=True).start()
        else:
            self.warning = "OPENSEARCH_URL not set; using in-memory store"

    @property
    def store(self):
        return self.world.store

    def _connect(self, wait_s):
        deadline, err = time.time() + wait_s, None
        while time.time() < deadline:
            try:
                s = OpenSearchStore(self.url)
                s.setup()
                old = self.world.store
                self.world.store = s
                for t in [t for t in list(self.world.tiles.values()) if t.status == "ready"]:
                    s.index_tile(t)
                    fc, _ = t.latest_fc(self.world.rain.run.id)
                    s.index_forecast(t, fc)
                s.add_reports([r for r in self.world.resident if r.get("kind") != "closed"] + getattr(old, "reports", []))
                self.warning = None
                print(f"[paani] OpenSearch connected at {self.url}", flush=True)
                return
            except Exception as e:  # noqa: BLE001
                err = e
                self.warning = f"Connecting to OpenSearch at {self.url}… (using in-memory store meanwhile)"
                time.sleep(3)
        self.warning = f"OpenSearch at {self.url} not reachable ({type(err).__name__}); using in-memory store"
