"""Local context for the flood model: climate (drain sizing), wet ground and tide.

  climate     35 years of hourly ERA5 rain per 0.25° cell (the reanalysis' own grid),
              Gumbel-fitted once and cached on disk forever. Drains = 2-year storm.
  ground      rain in the last 3 and 7 days + soil moisture (live), per ~38 km zone, 3 h cache.
  tide        sea level off the coast (live), per zone, 1 h cache.

All fetches run in the background; until they land the model uses defaults and says so.
"""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from ..engine import chennai

CLIMATE_DEG = 0.25


class ContextService:
    def __init__(self, cache_root: Path, submit):
        self.root = cache_root
        self.submit = submit            # submit(key, fn, prio, pool)
        self.lock = threading.Lock()
        self.climate = {}               # cell -> clim | None (None = failed)
        self.zone = {}                  # zone key -> {"ante":..., "tide":..., "at":...}

    def _cell(self, lat, lon):
        return (math.floor(lat / CLIMATE_DEG), math.floor(lon / CLIMATE_DEG))

    def climate_for(self, lat, lon, prio=(3, 0)):
        c = self._cell(lat, lon)
        with self.lock:
            if c in self.climate:
                return self.climate[c]
        path = self.root / "climate" / f"{c[0]}_{c[1]}.json"
        if path.exists():
            clim = chennai.fetch_climate(0, 0, path)
            with self.lock:
                self.climate[c] = clim
            return clim

        def job():
            la, lo = (c[0] + 0.5) * CLIMATE_DEG, (c[1] + 0.5) * CLIMATE_DEG
            try:
                clim = chennai.fetch_climate(la, lo, path)
            except Exception:
                with self.lock:
                    self.climate[c] = None
                raise
            with self.lock:
                self.climate[c] = clim
        self.submit(f"climate:{c[0]}_{c[1]}", job, prio, "net")
        return None

    def nearest_climate(self, lat, lon):
        """The climate for this cell, or the nearest cell we already have (region tiles)."""
        clim = self.climate_for(lat, lon, prio=(4, 0))
        if clim:
            return clim
        with self.lock:
            have = [(k, v) for k, v in self.climate.items() if v]
        if not have:
            return None
        c = self._cell(lat, lon)
        return min(have, key=lambda kv: (kv[0][0] - c[0]) ** 2 + (kv[0][1] - c[1]) ** 2)[1]

    def live(self, zone_key: str, lat, lon, prio=(2, 0)):
        """Wet-ground and tide context for a zone; refreshes in the background."""
        with self.lock:
            z = self.zone.get(zone_key)
        if not z or time.time() - z["at"] > 3600:
            def job():
                out = {"at": time.time(), "ante": None, "tide": None}
                try:
                    out["ante"] = chennai.fetch_antecedent(lat, lon)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    out["tide"] = chennai.fetch_tide(lat, lon)
                except Exception:  # noqa: BLE001
                    pass
                with self.lock:
                    self.zone[zone_key] = out
            self.submit(f"zone:{zone_key}:{int(time.time() // 3600)}", job, prio, "net")
        return z or {"ante": None, "tide": None}
