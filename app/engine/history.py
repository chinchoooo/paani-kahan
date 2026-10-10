"""Seed flood reports.

Real resident complaint records are not public in machine-readable form, so we seed a
history of reports at places the model floods under a heavier past storm. Every report
carries source="synthetic-seed" and the UI labels it as seed data. In production these
would be ingested from Namma Chennai / GCC complaint exports.
"""
from __future__ import annotations

import random

from .rain import RainModel

TEMPLATES = [
    "Knee-deep water on {street}, two-wheelers stalling near the junction",
    "{street} completely waterlogged, autos refusing to go",
    "Water entered ground floor houses along {street}",
    "Drain overflowing on {street}, cannot see the road edge",
    "{street} underpass flooded, traffic diverted",
    "Ankle-deep water on {street} for 2 days after the rain stopped",
    "Open manhole hidden under water on {street}, very dangerous",
    "Bus stuck on {street}, water up to the steps",
]
DATES = ["2023-12-04", "2024-10-15", "2024-11-30", "2025-10-22", "2025-12-02"]


def seed_reports(area, model, n=70, seed=11):
    rng = random.Random(seed)
    sim = model.simulate(RainModel(*area.dem.shape, area.cell_m, version=1), "history")
    peak = sim.seg_depth.max(axis=0).astype(int)
    candidates = [s for s in area.segments if peak[s.idx] >= 20 and not s.name.startswith("Unnamed")]
    if not candidates:
        candidates = sorted(area.segments, key=lambda s: -peak[s.idx])[:40]
    reports = []
    for i in range(n):
        s = rng.choices(candidates, weights=[peak[c.idx] for c in candidates])[0]
        lat, lon = s.mid
        reports.append({
            "id": f"r{i}",
            "text": rng.choice(TEMPLATES).format(street=s.name),
            "street": s.name,
            "locality": s.locality,
            "date": rng.choice(DATES),
            "depth_cm_reported": int(min(120, peak[s.idx] * rng.uniform(0.7, 1.3))),
            "lat": lat + rng.uniform(-0.0002, 0.0002),
            "lon": lon + rng.uniform(-0.0002, 0.0002),
            "seg": s.idx,
            "source": "synthetic-seed",
        })
    return reports
