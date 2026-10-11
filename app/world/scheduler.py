"""A small priority scheduler for background work.

Jobs are keyed (so asking twice does nothing), carry a priority that can only improve
(a tile that scrolls into view jumps the queue), and run on named pools: 'net' for
upstream fetches and 'cpu' for building and forecasting. Failed jobs back off and can
be retried. Everything the UI shows about loading progress comes from here.
"""
from __future__ import annotations

import heapq
import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field

# priority classes (lower runs first)
URGENT, VISIBLE, ROUTE, NEAR, REGION, IDLE = -1, 0, 1, 2, 3, 4  # URGENT: the demo's streets around you


@dataclass
class Job:
    key: str
    fn: object
    prio: tuple
    pool: str
    state: str = "queued"     # queued | running | done | failed
    error: str | None = None
    tries: int = 0
    not_before: float = 0.0
    done_at: float = 0.0
    callbacks: list = field(default_factory=list)


class Scheduler:
    def __init__(self, pools=None):
        self.pools = pools or {"net": 2, "cpu": 2}
        self.cv = threading.Condition()
        self.jobs: dict[str, Job] = {}
        self.queues = {p: [] for p in self.pools}
        self.seq = itertools.count()
        self.stats = {"done": 0, "failed": 0}
        self.recent = []
        for pool, n in self.pools.items():
            for i in range(n):
                threading.Thread(target=self._worker, args=(pool,), name=f"{pool}-{i}", daemon=True).start()

    def submit(self, key, fn, prio, pool="cpu", retry_failed=True, on_done=None):
        """Queue fn under key. Returns the Job. If already queued, the priority improves."""
        with self.cv:
            j = self.jobs.get(key)
            if j and j.state in ("queued", "running"):
                if on_done:
                    j.callbacks.append(on_done)
                if j.state == "queued" and prio < j.prio:
                    j.prio = prio
                    heapq.heappush(self.queues[j.pool], (prio, next(self.seq), key))
                    self.cv.notify_all()
                return j
            if j and j.state == "done":
                return j
            if j and j.state == "failed" and (not retry_failed or time.time() < j.not_before):
                return j
            tries = j.tries if j else 0
            j = Job(key, fn, prio, pool, tries=tries, callbacks=[on_done] if on_done else [])
            self.jobs[key] = j
            heapq.heappush(self.queues[pool], (prio, next(self.seq), key))
            self.cv.notify_all()
            return j

    def forget(self, prefix):
        """Drop finished jobs whose key starts with prefix (so they can run again)."""
        with self.cv:
            for k in [k for k, j in self.jobs.items() if k.startswith(prefix) and j.state in ("done", "failed")]:
                del self.jobs[k]

    def state(self, key):
        j = self.jobs.get(key)
        return j.state if j else None

    def _worker(self, pool):
        q = self.queues[pool]
        while True:
            with self.cv:
                job = None
                while job is None:
                    while not q:
                        self.cv.wait()
                    prio, _, key = heapq.heappop(q)
                    j = self.jobs.get(key)
                    if j and j.state == "queued" and j.prio == prio and j.pool == pool:
                        job = j
                    elif j and j.state == "queued" and j.prio == prio:
                        job = j
                job.state = "running"
            t0 = time.time()
            try:
                job.fn()
                job.state = "done"
                self.stats["done"] += 1
            except Exception as e:  # noqa: BLE001
                job.tries += 1
                job.error = f"{type(e).__name__}: {e}"[:200]
                job.state = "failed"
                job.not_before = time.time() + (3600 if job.tries >= 4 else min(300, 15 * 2 ** job.tries))
                self.stats["failed"] += 1
                if job.tries <= 1:
                    if "httpx" in type(e).__module__ or type(e).__name__ in ("CircuitOpen", "RuntimeError", "ProxyError"):
                        print(f"[paani] job {job.key} failed: {job.error}", flush=True)
                    else:
                        traceback.print_exc()
            job.done_at = time.time()
            self.recent.insert(0, {"key": job.key, "state": job.state, "s": round(job.done_at - t0, 2),
                                   "at": time.strftime("%H:%M:%S"), "error": job.error})
            del self.recent[40:]
            for cb in job.callbacks:
                try:
                    cb(job)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()

    def summary(self):
        with self.cv:
            out = {"queued": 0, "running": 0}
            for j in self.jobs.values():
                if j.state in out:
                    out[j.state] += 1
            return {**out, **self.stats, "pools": self.pools}
