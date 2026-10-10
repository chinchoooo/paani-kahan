"""Live road closures, flooding and roadworks from the TomTom Traffic Incidents API (v5).

Needs a free TomTom developer key in TOMTOM_API_KEY. Without one, this is skipped and the
app says so. https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-incidents/incident-details
"""
from __future__ import annotations

import os

import numpy as np
from scipy.spatial import cKDTree

CATEGORY = {1: "Accident", 6: "Jam", 7: "Lane closed", 8: "Road closed", 9: "Roadworks", 11: "Flooding", 14: "Broken-down vehicle"}
CLOSES = {8, 11}          # treat as impassable
SLOWS = {1: 1.5, 7: 1.4, 9: 1.6, 14: 1.3}


def key():
    return os.getenv("TOMTOM_API_KEY", "").strip()


def fetch_incidents(bbox, timeout=10):
    from ..world import net
    s, w, n, e = bbox
    fields = "{incidents{type,geometry{type,coordinates},properties{id,iconCategory,events{description},startTime,endTime,from,to}}}"
    r = net.TOMTOM.get(os.getenv("TOMTOM_URL", "https://api.tomtom.com/traffic/services/5/incidentDetails"), retries=1, params={
        "key": key(), "bbox": f"{w},{s},{e},{n}", "fields": fields, "language": "en-GB",
        "categoryFilter": "1,6,7,8,9,11,14", "timeValidityFilter": "present"})
    r.raise_for_status()
    return r.json().get("incidents", [])


class SegmentIndex:
    """Nearest street segment for a lat/lon, using every vertex of every segment."""

    def __init__(self, area):
        pts, owner = [], []
        segs = area.segments.values() if isinstance(area.segments, dict) else area.segments
        for sg in segs:
            for la, lo in sg.coords:
                pts.append((la * 110_574, lo * area.m_per_deg_lon))
                owner.append(sg.gid or sg.idx)
        self.mlon = area.m_per_deg_lon
        self.tree = cKDTree(np.array(pts)) if pts else None
        self.owner = np.array(owner, dtype=object)

    def nearest(self, lat, lon, max_m=40):
        if self.tree is None:
            return []
        d, i = self.tree.query([lat * 110_574, lon * self.mlon])
        return [self.owner[i]] if d <= max_m else []

    def near(self, lat, lon, radius_m=25):
        if self.tree is None:
            return []
        idx = self.tree.query_ball_point([lat * 110_574, lon * self.mlon], radius_m)
        return sorted(set(self.owner[idx].tolist()), key=str)


def to_closures(incidents, index):
    """-> (closed segment set, slow factors, closure records for the map)."""
    closed, slow, records = set(), {}, []
    for inc in incidents:
        p = inc.get("properties", {})
        cat = p.get("iconCategory")
        g = inc.get("geometry", {})
        coords = g.get("coordinates") or []
        if g.get("type") == "Point":
            coords = [coords]
        segs = set()
        if g.get("type") == "LineString" and len(coords) >= 2:
            # sample between vertices so a closure doesn't leak onto streets at its end junctions
            for a, b in zip(coords, coords[1:]):
                segs.update(index.near((a[1] + b[1]) / 2, (a[0] + b[0]) / 2, 20))
        else:
            for c in coords:
                if len(c) >= 2:
                    segs.update(index.nearest(c[1], c[0]))
        if not segs or cat not in CATEGORY:
            continue
        desc = "; ".join(ev.get("description", "") for ev in p.get("events") or [] if ev.get("description")) or CATEGORY[cat]
        if cat in CLOSES:
            closed |= segs
        elif cat in SLOWS:
            for sg in segs:
                slow[sg] = max(slow.get(sg, 1.0), SLOWS[cat])
        else:
            continue
        records.append({"segs": sorted(segs), "kind": CATEGORY[cat], "closes": cat in CLOSES, "desc": desc,
                        "from": p.get("from"), "to": p.get("to"), "until": p.get("endTime"), "source": "TomTom"})
    return closed, slow, records
