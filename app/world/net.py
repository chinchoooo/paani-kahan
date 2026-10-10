"""Resilient HTTP for upstream data: retries with backoff, per-host circuit breakers,
per-host concurrency and pacing limits, and an on-disk cache with freshness.

Upstreams (Overpass, AWS Terrain Tiles, Open-Meteo, TomTom) are public and rate
limited. Every call goes through `Upstream`, so a slow or failing service degrades one
layer of the map instead of the whole app, and we never hammer a free API.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import threading
import time
from collections import deque
from pathlib import Path

import httpx

UA = {"User-Agent": "paani-kahan/3.0 (street flood forecast; hackathon project)"}
NET_LOG: deque = deque(maxlen=60)   # recent upstream calls, shown in How it works -> System


class CircuitOpen(Exception):
    pass


class Upstream:
    """One upstream service. Thread-safe."""

    def __init__(self, name, max_concurrent=2, min_interval_s=0.0, fail_threshold=4, cooloff_s=60.0, timeout=30.0):
        self.name = name
        self.sem = threading.BoundedSemaphore(max_concurrent)
        self.min_interval = min_interval_s
        self.fail_threshold = fail_threshold
        self.cooloff = cooloff_s
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last = 0.0
        self._fails = 0
        self._open_until = 0.0
        self.calls = 0
        self.errors = 0
        self.busy = 0
        self.bytes = 0
        self.client = httpx.Client(headers=UA, timeout=timeout, follow_redirects=True)

    @property
    def state(self):
        if time.time() < self._open_until:
            return "open"
        return "degraded" if self._fails else "ok"

    def _pace(self):
        with self._lock:
            wait = self._last + self.min_interval - time.time()
            self._last = max(time.time(), self._last + self.min_interval)
        if wait > 0:
            time.sleep(wait)

    def request(self, method, url, retries=3, **kw):
        if time.time() < self._open_until:
            raise CircuitOpen(f"{self.name} is cooling off after repeated failures")
        last = None
        for attempt in range(retries + 1):
            with self._lock:
                self.busy += 1
            with self.sem:
                self._pace()
                t0 = time.time()
                try:
                    r = self.client.request(method, url, **kw)
                    ms = (time.time() - t0) * 1000
                    if r.status_code in (429, 502, 503, 504):
                        raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    with self._lock:
                        self._fails = 0
                        self.calls += 1
                        self.bytes += len(r.content)
                    NET_LOG.appendleft({"at": time.strftime("%H:%M:%S"), "up": self.name, "ok": True,
                                        "ms": round(ms), "kb": round(len(r.content) / 1024, 1), "url": url[:90]})
                    return r
                except Exception as e:  # noqa: BLE001
                    last = e
                    status = getattr(getattr(e, "response", None), "status_code", 0)
                    client_error = bool(status) and 400 <= status < 500 and status != 429
                    with self._lock:
                        self.errors += 1
                        if not client_error:  # only an unhealthy service trips the breaker, not a bad request
                            self._fails += 1
                            if self._fails >= self.fail_threshold:
                                self._open_until = time.time() + self.cooloff
                    NET_LOG.appendleft({"at": time.strftime("%H:%M:%S"), "up": self.name, "ok": False,
                                        "ms": round((time.time() - t0) * 1000), "err": f"{type(e).__name__}: {e}"[:80], "url": url[:90]})
                    if client_error:
                        break  # our fault; retrying won't help
                finally:
                    with self._lock:
                        self.busy -= 1
            if attempt < retries:
                back = (2 ** attempt) * (6 if "429" in str(last) else 1.2)
                time.sleep(back + random.random() * 0.5)
        raise last

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def stats(self):
        return {"name": self.name, "state": self.state, "calls": self.calls, "errors": self.errors,
                "mb": round(self.bytes / 1e6, 2)}


def env_list(name, default):
    v = os.getenv(name)
    return [s.strip() for s in v.split(",") if s.strip()] if v else default


# Shared upstreams. Overpass asks for at most a couple of parallel queries per user.
OVERPASS = Upstream("OpenStreetMap (Overpass)", max_concurrent=1, min_interval_s=1.0, fail_threshold=6, cooloff_s=45, timeout=180)
OVERPASS_MIRRORS = {}  # url -> Upstream: each public mirror has its own rate limit, so we use them in parallel


def overpass_mirror(url):
    if url not in OVERPASS_MIRRORS:
        host = url.split("/")[2]
        conc = 2 if host == "overpass-api.de" else 1   # overpass-api.de allows a couple of slots per user
        OVERPASS_MIRRORS[url] = Upstream(f"Overpass · {host}", max_concurrent=conc, min_interval_s=1.0,
                                         fail_threshold=3, cooloff_s=120, timeout=180)
    return OVERPASS_MIRRORS[url]
TERRAIN = Upstream("AWS Terrain Tiles", max_concurrent=8, timeout=20)
METEO = Upstream("Open-Meteo", max_concurrent=2, min_interval_s=0.2, timeout=30)
METEO_ARCHIVE = Upstream("Open-Meteo archive (ERA5)", max_concurrent=1, min_interval_s=0.5, timeout=90)
TOMTOM = Upstream("TomTom Traffic", max_concurrent=1, min_interval_s=1.0, timeout=10)
NOMINATIM = Upstream("Nominatim", max_concurrent=1, min_interval_s=1.0, timeout=8)
ALL = [TERRAIN, METEO, METEO_ARCHIVE, TOMTOM, NOMINATIM]


def all_upstreams():
    return list(OVERPASS_MIRRORS.values()) + ALL


# ------------------------------------------------------------------ disk cache
class DiskCache:
    """JSON documents on disk (gzip), with write-then-rename so readers never see half a file."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, *parts, ext=".json.gz"):
        p = self.root.joinpath(*[str(x) for x in parts])
        return p.with_name(p.name + ext)

    def get(self, *parts, max_age_s=None):
        p = self.path(*parts)
        if not p.exists():
            return None
        try:
            doc = json.loads(gzip.decompress(p.read_bytes()))
        except Exception:  # noqa: BLE001  (corrupt: treat as a miss)
            return None
        if max_age_s is not None and time.time() - doc.get("_at", 0) > max_age_s:
            doc["_stale"] = True
        return doc

    def put(self, doc, *parts):
        p = self.path(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{threading.get_ident()}")
        tmp.write_bytes(gzip.compress(json.dumps({**doc, "_at": time.time()}, separators=(",", ":")).encode(), 5))
        tmp.replace(p)

    def get_bytes(self, *parts, ext):
        p = self.path(*parts, ext=ext)
        return p.read_bytes() if p.exists() else None

    def put_bytes(self, data: bytes, *parts, ext):
        p = self.path(*parts, ext=ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{threading.get_ident()}")
        tmp.write_bytes(data)
        tmp.replace(p)
