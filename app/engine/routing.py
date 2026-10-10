"""Flood-aware routing with a Google Maps handoff.

We never do turn-by-turn navigation. We find the route that stays dry, then hand the
person to Google Maps with waypoints that pin it to that route.
"""
from __future__ import annotations

from urllib.parse import urlencode

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

# km/h in monsoon traffic, by road class
SPEED = {  # 3 = motorway / trunk (highways between cities)
    "two_wheeler": {3: 50, 0: 26, 1: 22, 2: 16},
    "car": {3: 60, 0: 24, 1: 19, 2: 14},
    "walk": {3: 4.5, 0: 4.5, 1: 4.5, 2: 4.5},
}
# depth (cm) at which a mode should not attempt a street
LIMIT_CM = {"two_wheeler": 30, "car": 45, "walk": 25}
MODE_LABEL = {"two_wheeler": "Two-wheeler", "car": "Car", "walk": "Walking"}
GMAPS_MODE = {"two_wheeler": "driving", "car": "driving", "walk": "walking"}
ROAD_CLASS = {"motorway": 3, "trunk": 3, "primary": 0, "secondary": 1, "tertiary": 1}
# barriers on OpenStreetMap: these stop cars and two-wheelers but not people on foot
BLOCK_MOTOR = {"bollard", "block", "jersey_barrier", "cycle_barrier", "chain", "fence", "wall", "kissing_gate", "turnstile", "stile"}
MAYBE_CLOSED = {"gate", "lift_gate", "swing_gate", "sliding_gate", "toll_booth"}  # often staffed or open: penalty
SUBWAY_CLOSE_CM = 30  # the city closes subways around this depth


class Router:
    def __init__(self, area):
        self.area = area
        self.G = nx.Graph()
        bar = getattr(area, "barriers", {}) or {}
        segs = area.segments.values() if isinstance(area.segments, dict) else area.segments
        self.segs = {(s.gid or s.idx): s for s in segs}
        for s in segs:
            cls = ROAD_CLASS.get(s.highway, 2)
            if self.G.has_edge(s.u, s.v) and self.G[s.u][s.v]["length"] <= s.length_m:
                continue
            b = next((bar[n] for n in (s.nodes or []) if n in bar), None)
            self.G.add_edge(s.u, s.v, seg=s.gid or s.idx, length=s.length_m, cls=cls, construction=s.construction,
                            underpass=s.underpass, barrier=(b or {}).get("type"), barrier_access=(b or {}).get("access", ""))
        self.closed = set()   # segments closed right now (live traffic incidents, resident reports)
        self.slow = {}        # segment -> time factor (roadworks)
        # keep the largest connected component
        big = max(nx.connected_components(self.G), key=len)
        self.G = self.G.subgraph(big).copy()
        self.node_ids = list(self.G.nodes)
        pts = np.array([[area.nodes[n][0] * 110_574, area.nodes[n][1] * area.m_per_deg_lon] for n in self.node_ids])
        self.tree = cKDTree(pts)

    def nearest(self, lat, lon):
        _, i = self.tree.query([lat * 110_574, lon * self.area.m_per_deg_lon])
        return self.node_ids[i]

    def _minutes(self, e, mode):
        return e["length"] / 1000 / SPEED[mode][e["cls"]] * 60

    def _blocked(self, e, mode):
        """Is this street shut for this mode, regardless of water?"""
        if e.get("construction") or e["seg"] in self.closed:
            return True
        b = e.get("barrier")
        if b and mode != "walk" and (b in BLOCK_MOTOR or e.get("barrier_access") in ("no", "private")):
            return True
        return False

    def _factor(self, e, mode):
        f = self.slow.get(e["seg"], 1.0)
        if e.get("barrier") in MAYBE_CLOSED and mode != "walk":
            f *= 4
        return f

    # ----------------------------------------------------------------- profile
    def profile(self, path, mode, depart_min, sim):
        """Walk a path in time and read the forecast depth when you'd reach each street."""
        out, t = [], depart_min
        for a, b in zip(path, path[1:]):
            e = self.G[a][b]
            t_mid = t + self._minutes(e, mode) / 2
            d = int(sim.depth(e["seg"], sim.frame_at(t_mid)))
            out.append({"seg": e["seg"], "depth": d, "t": round(t_mid, 1)})
            t += self._minutes(e, mode)
        return out, t - depart_min

    def summarise(self, path, mode, depart_min, sim):
        prof, mins = self.profile(path, mode, depart_min, sim)
        limit = LIMIT_CM[mode]
        worst = max(prof, key=lambda p: p["depth"]) if prof else None
        segs = self.segs
        stretches, cur = [], None
        for p in prof:
            if p["depth"] >= limit:
                name = segs[p["seg"]].name
                if cur and cur["name"] == name:
                    cur["depth"] = max(cur["depth"], p["depth"])
                else:
                    cur = {"name": name, "depth": p["depth"], "t": p["t"], "seg": p["seg"]}
                    stretches.append(cur)
            else:
                cur = None
        coords = []
        for a, b in zip(path, path[1:]):
            s = segs[self.G[a][b]["seg"]]
            c = s.coords if s.u == a else s.coords[::-1]
            coords.extend(c if not coords else c[1:])
        return {
            "minutes": round(mins),
            "km": round(sum(self.G[a][b]["length"] for a, b in zip(path, path[1:])) / 1000, 1),
            "max_depth": worst["depth"] if worst else 0,
            "worst": {"name": segs[worst["seg"]].name, "seg": worst["seg"], "depth": worst["depth"],
                      "t": worst["t"], "locality": segs[worst["seg"]].locality} if worst else None,
            "flooded": stretches,
            "passable": not stretches,
            "coords": coords,
            "profile": [[p["seg"], p["depth"]] for p in prof],
            "nodes": path,
        }

    # ------------------------------------------------------------------ routes
    def usual_path(self, o, d, mode, respect_closures=True):
        def w(a, b, e):
            if respect_closures and self._blocked(e, mode):
                return None
            return self._minutes(e, mode) * self._factor(e, mode)
        return nx.shortest_path(self.G, o, d, weight=w)

    def dry_path(self, o, d, mode, depart_min, sim, eta_guess):
        limit = LIMIT_CM[mode]
        frames = sorted({sim.frame_at(depart_min), sim.frame_at(depart_min + eta_guess / 2), sim.frame_at(depart_min + eta_guess)})

        def w(a, b, e):
            if self._blocked(e, mode):
                return None
            dd = max(sim.depth(e["seg"], f) for f in frames)
            if dd >= limit * 0.8 or (e.get("underpass") and dd >= SUBWAY_CLOSE_CM):
                return None  # too deep, or a subway the city will have closed
            return self._minutes(e, mode) * self._factor(e, mode) * (1 + 3 * (dd / limit) ** 2)
        try:
            return nx.shortest_path(self.G, o, d, weight=w)
        except nx.NetworkXNoPath:
            return None

    def best_departure(self, o, d, mode, sim, start=0, end=300):
        path = self.usual_path(o, d, mode)
        rows = []
        for dep in range(start, end + 1, 10):
            s = self.summarise(path, mode, dep, sim)
            rows.append({"depart": dep, "max_depth": s["max_depth"], "passable": s["passable"]})
        return rows

    def gmaps_link(self, usual, dry, mode, origin, dest):
        params = {"api": 1, "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
                  "destination": f"{dest[0]:.6f},{dest[1]:.6f}", "travelmode": GMAPS_MODE[mode]}
        if dry and usual:
            on_usual = set(usual)
            diverge = [n for n in dry[1:-1] if n not in on_usual]
            if diverge:
                k = min(3, len(diverge))
                picks = [diverge[int((i + 0.5) * len(diverge) / k)] for i in range(k)]
                params["waypoints"] = "|".join(f"{self.area.nodes[n][0]:.6f},{self.area.nodes[n][1]:.6f}" for n in picks)
        return "https://www.google.com/maps/dir/?" + urlencode(params, safe=",|")

    def plan(self, origin, dest, mode, depart_min, sim):
        o, d = self.nearest(*origin), self.nearest(*dest)
        usual_p = self.usual_path(o, d, mode)
        avoided = []
        try:
            raw = self.usual_path(o, d, mode, respect_closures=False)
            if raw != usual_p:
                for a, b in zip(raw, raw[1:]):
                    e = self.G[a][b]
                    if self._blocked(e, mode):
                        avoided.append({"seg": e["seg"], "name": self.segs[e["seg"]].name,
                                        "why": "under construction" if e.get("construction") else
                                               "closed" if e["seg"] in self.closed else f"{e.get('barrier')} on the road"})
        except nx.NetworkXNoPath:
            pass
        usual = self.summarise(usual_p, mode, depart_min, sim)
        dry = None
        if not usual["passable"] or usual["max_depth"] >= LIMIT_CM[mode] * 0.6:
            dp = self.dry_path(o, d, mode, depart_min, sim, usual["minutes"])
            if dp and dp != usual_p:
                dry = self.summarise(dp, mode, depart_min, sim)
                dry["nodes_"] = dp
        status = "clear"
        if usual["max_depth"] >= LIMIT_CM[mode] * 0.6:
            status = "watch"
        if not usual["passable"]:
            status = "alert"
        link = self.gmaps_link(usual_p, dry and dry.pop("nodes_"), mode, origin, dest)
        link_usual = self.gmaps_link(None, None, mode, origin, dest)
        for r in (usual, dry):
            if r:
                r.pop("nodes", None)
        subways = [p for p in usual["profile"] if self.segs[p[0]].underpass and p[1] >= SUBWAY_CLOSE_CM]
        return {"status": status, "mode": mode, "mode_label": MODE_LABEL[mode], "limit_cm": LIMIT_CM[mode],
                "avoided": avoided[:5], "subways_closed": [self.segs[s_].name for s_, _ in subways][:3],
                "depart_min": depart_min, "usual": usual, "dry": dry, "gmaps": link, "gmaps_usual": link_usual}
