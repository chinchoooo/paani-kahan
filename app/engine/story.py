"""Pick a demo commute for an area.

The demo needs one commute where the 4:10 PM nowcast says 'watch', the updated
nowcast says 'flooded', and a dry route exists. We search candidate trips across
the area and keep the best one. This is run once when an area is prepared.
"""
from __future__ import annotations

import random

from .rain import RainModel
from .routing import LIMIT_CM

DEPART = 110  # 6:00 PM, minutes after the 4:10 PM 'now'


def pick_story(area, model, router, tries=700, seed=3):
    rng = random.Random(seed)
    sims = [model.simulate(RainModel(*area.dem.shape, area.cell_m, version=v), f"story{v}") for v in (0, 1)]
    nodes = router.node_ids
    best, best_score = None, -1e9
    mode = "two_wheeler"
    lim = LIMIT_CM[mode]
    found = 0
    for k in range(tries):
        if found >= 3 and k > 150:
            break
        a, b = rng.choice(nodes), rng.choice(nodes)
        pa, pb = area.nodes[a], area.nodes[b]
        dist = area.dist_m(pa, pb)
        if not 2800 < dist < 5000:
            continue
        try:
            p0 = router.plan(pa, pb, mode, DEPART, sims[0])
            p1 = router.plan(pa, pb, mode, DEPART, sims[1])
        except Exception:
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
        "from": list(pa), "from_label": area.locality_of(*pa),
        "to": list(pb), "to_label": area.locality_of(*pb),
        "check": {"v0_cm": d0, "v1_cm": d1, "dry_cm": dd},
    }
