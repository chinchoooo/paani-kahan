"""Pick the demo commute on the streets where you are.

The demo needs one commute where the 4:10 PM nowcast says 'watch', the updated
nowcast says 'flooded', and a dry route exists. We search candidate trips across the
tiles around you and keep the best one.
"""
from __future__ import annotations

import random

from .routing import LIMIT_CM

DEPART = 110  # 6:00 PM, minutes after the 4:10 PM 'now'


def pick_story(router, sim0, sim1, locality_of, center=None, tries=900, seed=3):
    rng = random.Random(seed)
    nodes = router.node_ids
    area = router.area
    if center:  # prefer trips that start and end near the middle of the map
        nodes = sorted(nodes, key=lambda n: area.dist_m(area.nodes[n], center))[: max(400, len(nodes) // 3)]
    best, best_score = None, -1e9
    mode = "two_wheeler"
    lim = LIMIT_CM[mode]
    found = 0
    for k in range(tries):
        if found >= 3 and k > 200:
            break
        a, b = rng.choice(nodes), rng.choice(nodes)
        pa, pb = area.nodes[a], area.nodes[b]
        dist = area.dist_m(pa, pb)
        if not 2600 < dist < 5200:
            continue
        try:
            p0 = router.plan(pa, pb, mode, DEPART, sim0)
            p1 = router.plan(pa, pb, mode, DEPART, sim1)
        except Exception:  # noqa: BLE001
            continue
        d0, d1 = p0["usual"]["max_depth"], p1["usual"]["max_depth"]
        if not p1["dry"]:
            continue
        overhead = p1["dry"]["minutes"] / max(p1["usual"]["minutes"], 1)
        score = 0.0
        score -= abs(d0 - lim * 0.75) * 1.0           # 4:10 PM: close to the limit, but OK
        score += min(d1, 80) * 1.2                     # update: clearly flooded
        score -= max(0, overhead - 1.35) * 120          # dry route not absurd
        score -= p1["dry"]["max_depth"] * 0.8
        if d0 >= lim:
            score -= 60
        if d1 >= lim + 5 and d0 < lim and p1["dry"]["max_depth"] < lim * 0.5:
            score += 1000  # meets the full demo story
            found += 1
        if score > best_score:
            best_score, best = score, (pa, pb, d0, d1, p1["dry"]["max_depth"])
    if not best:
        return None
    pa, pb, d0, d1, dd = best
    return {
        "name": "Priya", "mode": mode, "depart_min": DEPART, "depart_label": "6:00 PM",
        "from": list(pa), "from_label": locality_of(*pa),
        "to": list(pb), "to_label": locality_of(*pb),
        "check": {"v0_cm": d0, "v1_cm": d1, "dry_cm": dd, "full_story": best_score > 500},
    }
