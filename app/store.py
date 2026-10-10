"""Search and analytics store.

OpenSearch is the primary store: geo-indexed street segments, the time-indexed flood
forecast, past flood reports (full-text + geo), and points of interest. Every query
the app runs is recorded with its DSL and timing so the UI can show it.

If OpenSearch is not reachable, MemoryStore answers the same questions in Python and
the UI status shows 'OpenSearch offline'. The demo should be recorded with OpenSearch up.
"""
from __future__ import annotations

import math
import os
import time
from collections import deque

P = os.getenv("OPENSEARCH_INDEX_PREFIX", "pk")
IDX = {k: f"{P}-{k}" for k in ("segments", "forecast", "reports", "pois", "places")}
QUERY_LOG: deque = deque(maxlen=40)


def _log(engine, op, query, ms, hits):
    QUERY_LOG.appendleft({"engine": engine, "op": op, "query": query, "ms": round(ms, 1),
                          "hits": hits, "at": time.strftime("%H:%M:%S")})


def _haversine(a_lat, a_lon, b_lat, b_lon):
    r = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def build_places(area):
    """Searchable places: every named street (once), localities, hospitals and schools."""
    best = {}
    for sg in area.segments:
        if sg.name.startswith("Unnamed"):
            continue
        if sg.name not in best or sg.length_m > best[sg.name].length_m:
            best[sg.name] = sg
    out = [{"name": n, "kind": "street", "sub": sg.locality, "lat": sg.mid[0], "lon": sg.mid[1], "seg": sg.idx}
           for n, sg in best.items()]
    city = area.meta.get("city") if area.meta.get("city") not in (None, "", "synthetic") else "Locality"
    out += [{"name": L["name"], "kind": "locality", "sub": city, "lat": L["lat"], "lon": L["lon"], "seg": None}
            for L in area.meta.get("localities") or []]
    out += [{"name": p["name"], "kind": p["kind"], "sub": area.locality_of(p["lat"], p["lon"]), "lat": p["lat"], "lon": p["lon"], "seg": None}
            for p in area.meta.get("pois") or []]
    return out


def q_places(text, size=8):
    return {"size": size, "query": {"function_score": {
        "query": {"bool": {"should": [
            {"multi_match": {"query": text, "type": "bool_prefix", "fields": ["name", "name._2gram", "name._3gram"]}},
            {"match": {"name": {"query": text, "fuzziness": "AUTO", "boost": 0.6}}}]}},
        "functions": [{"filter": {"terms": {"kind": ["locality", "hospital", "school"]}}, "weight": 1.6}],
        "boost_mode": "multiply"}}}


# ---------------------------------------------------------------- shared queries
def q_flooded(run_id, t, min_depth, locality=None, size=50):
    f = [{"term": {"run_id": run_id}}, {"term": {"t_min": t}}, {"range": {"depth_cm": {"gte": min_depth}}}]
    if locality:
        f.append({"term": {"locality": locality}})
    return {"size": size, "query": {"bool": {"filter": f}}, "sort": [{"depth_cm": "desc"}]}


def q_nearby(run_id, t, lat, lon, radius_m, min_depth, size):
    return {"size": size * 3, "query": {"bool": {"filter": [
        {"term": {"run_id": run_id}}, {"term": {"t_min": t}}, {"range": {"depth_cm": {"gte": min_depth}}},
        {"geo_distance": {"distance": f"{int(radius_m)}m", "mid": {"lat": lat, "lon": lon}}}]}},
        "sort": [{"depth_cm": "desc"}]}


def _dedupe(rows, size):
    seen, out = set(), []
    for r in rows:
        if r["name"] not in seen:
            seen.add(r["name"])
            out.append(r)
    return out[:size]


def q_locality_summary(run_id, t):
    return {"size": 0, "query": {"bool": {"filter": [{"term": {"run_id": run_id}}, {"term": {"t_min": t}}]}},
            "aggs": {"by_locality": {"terms": {"field": "locality", "size": 50},
                                     "aggs": {"max_depth": {"max": {"field": "depth_cm"}},
                                              "impassable": {"filter": {"range": {"depth_cm": {"gte": 30}}}},
                                              "worst": {"top_hits": {"size": 1, "sort": [{"depth_cm": "desc"}],
                                                                     "_source": ["name", "depth_cm", "seg_id"]}}}}}}


def q_reports(text, lat=None, lon=None, radius_m=500, size=5):
    must = [{"multi_match": {"query": text, "fields": ["text", "street^2"], "fuzziness": "AUTO"}}] if text else [{"match_all": {}}]
    flt = []
    if lat is not None:
        flt.append({"geo_distance": {"distance": f"{int(radius_m)}m", "location": {"lat": lat, "lon": lon}}})
    return {"size": size, "query": {"bool": {"must": must, "filter": flt}}, "sort": ["_score", {"date": "desc"}]}


def q_near_poi(run_id, t, lat, lon, min_depth, radius_m):
    return {"size": 1, "query": {"bool": {"filter": [
        {"term": {"run_id": run_id}}, {"term": {"t_min": t}}, {"range": {"depth_cm": {"gte": min_depth}}},
        {"geo_distance": {"distance": f"{int(radius_m)}m", "mid": {"lat": lat, "lon": lon}}}]}},
        "sort": [{"depth_cm": "desc"}]}


class OpenSearchStore:
    engine = "opensearch"

    def __init__(self, url):
        from opensearchpy import OpenSearch
        self.url = url
        self.c = OpenSearch(url, timeout=30, verify_certs=False, ssl_show_warn=False)
        self.c.info()
        self.run_id = None

    def _search(self, op, index, body):
        t = time.perf_counter()
        r = self.c.search(index=index, body=body)
        _log(self.engine, op, body, (time.perf_counter() - t) * 1000, r["hits"]["total"]["value"])
        return r

    def setup(self, area, reports):
        from opensearchpy import helpers
        maps = {
            "segments": {"seg_id": {"type": "integer"}, "name": {"type": "text", "fields": {"k": {"type": "keyword"}}},
                         "highway": {"type": "keyword"}, "locality": {"type": "keyword"},
                         "length_m": {"type": "float"}, "geometry": {"type": "geo_shape"}, "mid": {"type": "geo_point"}},
            "forecast": {"run_id": {"type": "keyword"}, "t_min": {"type": "integer"}, "seg_id": {"type": "integer"},
                         "name": {"type": "keyword"}, "locality": {"type": "keyword"},
                         "depth_cm": {"type": "integer"}, "mid": {"type": "geo_point"}},
            "reports": {"text": {"type": "text"}, "street": {"type": "text", "fields": {"k": {"type": "keyword"}}},
                        "locality": {"type": "keyword"}, "date": {"type": "date"},
                        "depth_cm_reported": {"type": "integer"}, "location": {"type": "geo_point"},
                        "source": {"type": "keyword"}, "seg": {"type": "integer"}, "url": {"type": "keyword", "index": False}},
            "pois": {"name": {"type": "text"}, "kind": {"type": "keyword"}, "locality": {"type": "keyword"},
                     "location": {"type": "geo_point"}},
            "places": {"name": {"type": "search_as_you_type"}, "kind": {"type": "keyword"}, "sub": {"type": "keyword"},
                       "seg": {"type": "integer"}, "location": {"type": "geo_point"}},
        }
        for k, props in maps.items():
            if self.c.indices.exists(index=IDX[k]):
                self.c.indices.delete(index=IDX[k])
            self.c.indices.create(index=IDX[k], body={"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                                                       "mappings": {"properties": props}})
        docs = []
        for s in area.segments:
            m = s.mid
            docs.append({"_index": IDX["segments"], "_id": s.idx, "_source": {
                "seg_id": s.idx, "name": s.name, "highway": s.highway, "locality": s.locality, "length_m": s.length_m,
                "geometry": {"type": "LineString", "coordinates": [[c[1], c[0]] for c in s.coords]},
                "mid": {"lat": m[0], "lon": m[1]}}})
        for r in reports:
            docs.append({"_index": IDX["reports"], "_id": r["id"], "_source": {
                **{k: r[k] for k in ("text", "street", "locality", "date", "depth_cm_reported", "source", "seg")},
                "url": r.get("url"), "location": {"lat": r["lat"], "lon": r["lon"]}}})
        for i, p in enumerate(area.meta.get("pois") or []):
            docs.append({"_index": IDX["pois"], "_id": i, "_source": {
                "name": p["name"], "kind": p["kind"], "locality": area.locality_of(p["lat"], p["lon"]),
                "location": {"lat": p["lat"], "lon": p["lon"]}}})
        for i, pl in enumerate(build_places(area)):
            docs.append({"_index": IDX["places"], "_id": i, "_source": {
                "name": pl["name"], "kind": pl["kind"], "sub": pl["sub"], "seg": pl["seg"],
                "location": {"lat": pl["lat"], "lon": pl["lon"]}}})
        helpers.bulk(self.c, docs, refresh=True)
        self.pois = area.meta.get("pois") or []

    def add_report(self, r):
        t0 = time.perf_counter()
        doc = {**{k: r[k] for k in ("text", "street", "locality", "date", "depth_cm_reported", "source", "seg")},
               "location": {"lat": r["lat"], "lon": r["lon"]}}
        self.c.index(index=IDX["reports"], id=r["id"], body=doc, refresh="wait_for")
        _log(self.engine, "index resident report", doc, (time.perf_counter() - t0) * 1000, 1)

    def search_places(self, text, size=8):
        r = self._search("place search (search_as_you_type)", IDX["places"], q_places(text, size))
        return [{"name": h["_source"]["name"], "kind": h["_source"]["kind"], "sub": h["_source"]["sub"],
                 "seg": h["_source"]["seg"], "lat": h["_source"]["location"]["lat"],
                 "lon": h["_source"]["location"]["lon"]} for h in r["hits"]["hits"]]

    def index_forecast(self, area, sim):
        from opensearchpy import helpers
        segs = area.segments

        def gen():
            for fi, t in enumerate(sim.times):
                row = sim.seg_depth[fi]
                for si in (row >= 5).nonzero()[0]:
                    s = segs[si]
                    m = s.mid
                    yield {"_index": IDX["forecast"], "_source": {
                        "run_id": sim.run_id, "t_min": t, "seg_id": int(si), "name": s.name,
                        "locality": s.locality, "depth_cm": int(row[si]), "mid": {"lat": m[0], "lon": m[1]}}}
        t0 = time.perf_counter()
        n, _ = helpers.bulk(self.c, gen(), chunk_size=2000, refresh=True)
        self.c.delete_by_query(index=IDX["forecast"], body={"query": {"bool": {"must_not": {"term": {"run_id": sim.run_id}}}}},
                               refresh=True, conflicts="proceed")
        _log(self.engine, "bulk index forecast", {"run_id": sim.run_id, "docs": n}, (time.perf_counter() - t0) * 1000, n)
        self.run_id = sim.run_id
        return n

    def flooded_segments(self, t, min_depth=30, locality=None, size=50):
        r = self._search("flooded streets", IDX["forecast"], q_flooded(self.run_id, t, min_depth, locality, size))
        return [h["_source"] for h in r["hits"]["hits"]]

    def nearby(self, t, lat, lon, radius_m=1200, min_depth=15, size=6):
        r = self._search("flooding near you (geo_distance)", IDX["forecast"], q_nearby(self.run_id, t, lat, lon, radius_m, min_depth, size))
        return _dedupe([h["_source"] for h in r["hits"]["hits"]], size)

    def locality_summary(self, t):
        r = self._search("locality summary (aggregation)", IDX["forecast"], q_locality_summary(self.run_id, t))
        out = []
        for b in r["aggregations"]["by_locality"]["buckets"]:
            w = b["worst"]["hits"]["hits"]
            out.append({"locality": b["key"], "segments_wet": b["doc_count"], "impassable": b["impassable"]["doc_count"],
                        "max_depth": int(b["max_depth"]["value"] or 0), "worst": w[0]["_source"] if w else None})
        return sorted(out, key=lambda x: (-x["impassable"], -x["max_depth"]))

    def search_reports(self, text, lat=None, lon=None, radius_m=500, size=5):
        r = self._search("flood history search", IDX["reports"], q_reports(text, lat, lon, radius_m, size))
        return [h["_source"] | {"score": h["_score"]} for h in r["hits"]["hits"]]

    def pois_at_risk(self, t, min_depth=30, radius_m=200):
        body = []
        for p in self.pois:
            body += [{"index": IDX["forecast"]}, q_near_poi(self.run_id, t, p["lat"], p["lon"], min_depth, radius_m)]
        if not body:
            return []
        t0 = time.perf_counter()
        r = self.c.msearch(body=body)
        _log(self.engine, f"vulnerable sites (msearch x{len(self.pois)})", body[1], (time.perf_counter() - t0) * 1000, len(self.pois))
        out = []
        for p, resp in zip(self.pois, r["responses"]):
            hits = resp["hits"]["hits"]
            if hits:
                out.append({**p, "street": hits[0]["_source"]["name"], "depth": hits[0]["_source"]["depth_cm"]})
        return sorted(out, key=lambda x: -x["depth"])


class MemoryStore:
    engine = "memory"

    def __init__(self):
        self.run_id = None

    def setup(self, area, reports):
        self.area, self.reports, self.pois = area, reports, area.meta.get("pois") or []
        self.places = build_places(area)

    def add_report(self, r):
        self.reports.append(r)
        _log(self.engine, "index resident report", {"street": r["street"], "depth_cm": r["depth_cm_reported"]}, 0, 1)

    def search_places(self, text, size=8):
        t0 = time.perf_counter()
        q = (text or "").lower().strip()
        out = []
        for pl in self.places:
            n = pl["name"].lower()
            if n.startswith(q):
                sc = 3
            elif any(w.startswith(q) for w in n.split()):
                sc = 2
            elif q in n:
                sc = 1
            else:
                continue
            out.append((sc * (1.6 if pl["kind"] != "street" else 1), pl))
        out.sort(key=lambda x: -x[0])
        _log(self.engine, "place search (search_as_you_type)", q_places(text, size), (time.perf_counter() - t0) * 1000, len(out))
        return [pl for _, pl in out[:size]]

    def index_forecast(self, area, sim):
        self.sim, self.run_id = sim, sim.run_id
        _log(self.engine, "load forecast in memory", {"run_id": sim.run_id}, 0, int((sim.seg_depth >= 5).sum()))

    def _rows(self, t, min_depth):
        fi = self.sim.frame_at(t)
        row = self.sim.seg_depth[fi]
        segs = self.area.segments
        return [{"seg_id": int(i), "name": segs[i].name, "locality": segs[i].locality, "depth_cm": int(row[i]),
                 "t_min": t, "mid": {"lat": segs[i].mid[0], "lon": segs[i].mid[1]}}
                for i in (row >= min_depth).nonzero()[0]]

    def flooded_segments(self, t, min_depth=30, locality=None, size=50):
        t0 = time.perf_counter()
        rows = [r for r in self._rows(t, min_depth) if not locality or r["locality"] == locality]
        rows.sort(key=lambda r: -r["depth_cm"])
        _log(self.engine, "flooded streets", q_flooded(self.run_id, t, min_depth, locality, size), (time.perf_counter() - t0) * 1000, len(rows))
        return rows[:size]

    def nearby(self, t, lat, lon, radius_m=1200, min_depth=15, size=6):
        t0 = time.perf_counter()
        rows = [r for r in self._rows(t, min_depth) if _haversine(lat, lon, r["mid"]["lat"], r["mid"]["lon"]) <= radius_m]
        rows.sort(key=lambda r: -r["depth_cm"])
        _log(self.engine, "flooding near you (geo_distance)", q_nearby(self.run_id, t, lat, lon, radius_m, min_depth, size), (time.perf_counter() - t0) * 1000, len(rows))
        return _dedupe(rows, size)

    def locality_summary(self, t):
        t0 = time.perf_counter()
        agg = {}
        for r in self._rows(t, 5):
            a = agg.setdefault(r["locality"], {"locality": r["locality"], "segments_wet": 0, "impassable": 0, "max_depth": 0, "worst": None})
            a["segments_wet"] += 1
            a["impassable"] += r["depth_cm"] >= 30
            if r["depth_cm"] > a["max_depth"]:
                a["max_depth"], a["worst"] = r["depth_cm"], {"name": r["name"], "depth_cm": r["depth_cm"], "seg_id": r["seg_id"]}
        _log(self.engine, "locality summary (aggregation)", q_locality_summary(self.run_id, t), (time.perf_counter() - t0) * 1000, len(agg))
        return sorted(agg.values(), key=lambda x: (-x["impassable"], -x["max_depth"]))

    def search_reports(self, text, lat=None, lon=None, radius_m=500, size=5):
        t0 = time.perf_counter()
        words = [w for w in (text or "").lower().split() if len(w) > 2]
        out = []
        for r in self.reports:
            if lat is not None and _haversine(lat, lon, r["lat"], r["lon"]) > radius_m:
                continue
            hay = (r["text"] + " " + r["street"] * 2).lower()
            score = sum(hay.count(w) for w in words) if words else 1
            if score:
                out.append({**{k: r[k] for k in ("text", "street", "locality", "date", "depth_cm_reported", "source", "seg")}, "url": r.get("url"),
                            "location": {"lat": r["lat"], "lon": r["lon"]}, "score": score})
        out.sort(key=lambda r: (r["score"], r["date"]), reverse=True)
        _log(self.engine, "flood history search", q_reports(text, lat, lon, radius_m, size), (time.perf_counter() - t0) * 1000, len(out))
        return out[:size]

    def pois_at_risk(self, t, min_depth=30, radius_m=200):
        t0 = time.perf_counter()
        rows = self._rows(t, min_depth)
        out = []
        for p in self.pois:
            near = [r for r in rows if _haversine(p["lat"], p["lon"], r["mid"]["lat"], r["mid"]["lon"]) <= radius_m]
            if near:
                w = max(near, key=lambda r: r["depth_cm"])
                out.append({**p, "street": w["name"], "depth": w["depth_cm"]})
        _log(self.engine, f"vulnerable sites (x{len(self.pois)})", {}, (time.perf_counter() - t0) * 1000, len(out))
        return sorted(out, key=lambda x: -x["depth"])


def connect(url: str | None, wait_s: float = 60):
    """Try OpenSearch for up to wait_s seconds, then fall back to memory."""
    if url:
        deadline = time.time() + wait_s
        err = None
        while time.time() < deadline:
            try:
                return OpenSearchStore(url), None
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(2)
        return MemoryStore(), f"OpenSearch at {url} not reachable ({type(err).__name__}); using in-memory store"
    return MemoryStore(), "OPENSEARCH_URL not set; using in-memory store"
