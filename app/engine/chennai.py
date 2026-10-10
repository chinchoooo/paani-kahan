"""Chennai context for the flood model, from real data.

  climate     30+ years of hourly rainfall (ERA5 reanalysis via the Open-Meteo archive)
              at the area. Annual maxima -> Gumbel fit -> return-period intensities.
              Storm drains are sized for roughly a 2-year storm (CPHEEO practice), so the
              2-year hourly intensity sets the drain capacity instead of a guess.
  antecedent  Rain in the last 3 and 7 days and current soil moisture (Open-Meteo).
              Soaked ground runs off more; silted, already-full drains carry less.
  tide        Sea level forecast off the coast (Open-Meteo marine). Around high tide,
              low-lying drains that empty into the Adyar, Cooum and the sea back up.
  history     Documented subway and road closures in past Chennai rain events, with
              sources. Used as real flood history instead of invented reports.
  replays     Recorded hourly rain for past storms, to run the model on them.

Caveats, shown in the app: ERA5 is a 25 km reanalysis; it smooths short cloudbursts, so
return periods are indicative. Marine tide data is ~8 km and coarse at the coast.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np

from .rain import TZ, _grid_points

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"

# Documented closures in past Chennai rain events (names as reported).
HISTORY = [
    {"date": "2021-12-30", "event": "30 Dec 2021 cloudburst",
     "source": "https://www.thenewsminute.com/article/subways-closed-chennai-due-water-logging-traffic-moving-slowly-159328",
     "subways": ["Gengu Reddy", "Gangureddy", "Madley", "Aranganathan", "RBI"],
     "areas": ["Chepauk", "Triplicane", "T Nagar", "Velachery", "Madipakkam", "Adyar"], "roads": []},
    {"date": "2023-12-04", "event": "Cyclone Michaung",
     "source": "https://thenewsminute.com/tamil-nadu/chennai-rains-list-of-subways-closed-and-traffic-updates",
     "subways": ["Aranganathan", "Gengureddy", "Madley", "Rangarajapuram", "Pazhavanthangal", "Thillai Ganga Nagar",
                 "C.P. Road", "Villivakkam", "Sembium", "Ganesapuram", "Vyasarpadi", "Manickam Nagar", "Duraisamy", "RBI"],
     "areas": ["Thirumangalam", "Perungudi", "Santhome", "St Thomas Mount", "Arumbakkam"],
     "roads": ["Manjambakkam", "Vadaperumbakkam", "Estate Road", "Park Road", "Five Furlong", "Santhome"]},
    {"date": "2023-12-06", "event": "Cyclone Michaung (aftermath)",
     "source": "https://www.thenewsminute.com/tamil-nadu/chennai-floods-traffic-subways-and-train-updates-for-dec-6",
     "subways": ["Ganesapuram", "Sembium", "Villivakkam", "Duraisamy", "Rangarajapuram", "Vyasarpadi", "Manickam Nagar",
                 "Loyola", "Aranganathan", "Kathirvedu"],
     "areas": [], "roads": ["Manjambakkam", "Vadaperumbakkam"]},
]

REPLAYS = {
    "michaung2023": {"name": "Cyclone Michaung", "start": "2023-12-02", "end": "2023-12-05"},
    "dec2015": {"name": "December 2015 floods", "start": "2015-11-30", "end": "2015-12-03"},
    "nov2021": {"name": "November 2021 rains", "start": "2021-11-06", "end": "2021-11-08"},
    "dec2021": {"name": "30 Dec 2021 cloudburst", "start": "2021-12-29", "end": "2021-12-31"},
}


def _get(url, params, timeout=20):
    import httpx
    r = httpx.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ climate
def gumbel_fit(maxima):
    x = np.asarray(maxima, dtype=float)
    alpha = math.sqrt(6) * x.std(ddof=1) / math.pi
    u = x.mean() - 0.5772 * alpha
    return u, alpha


def gumbel_value(u, alpha, T):
    return u - alpha * math.log(-math.log(1 - 1 / T))


def return_period(value, u, alpha):
    if alpha <= 0:
        return None
    p = math.exp(-math.exp(-(value - u) / alpha))
    return None if p >= 1 else 1 / (1 - p)


def fetch_climate(lat, lon, cache: Path, years=(1991, 2025)):
    if cache.exists():
        return json.loads(cache.read_text())
    j = _get(ARCHIVE, {"latitude": lat, "longitude": lon, "start_date": f"{years[0]}-01-01", "end_date": f"{years[1]}-12-31",
                       "hourly": "precipitation", "timezone": "Asia/Kolkata", "models": "era5"}, timeout=90)
    times = j["hourly"]["time"]
    p = np.array([v if v is not None else np.nan for v in j["hourly"]["precipitation"]], dtype=float)
    yrs = np.array([int(t[:4]) for t in times])
    hourly_max, daily_max = [], []
    for y in range(years[0], years[1] + 1):
        v = p[yrs == y]
        if np.isnan(v).mean() > 0.2 or not len(v):
            continue
        v = np.nan_to_num(v)
        hourly_max.append(float(v.max()))
        d24 = np.convolve(v, np.ones(24), mode="valid")
        daily_max.append(float(d24.max()))
    u1, a1 = gumbel_fit(hourly_max)
    u24, a24 = gumbel_fit(daily_max)
    clim = {
        "source": "ERA5 reanalysis (Open-Meteo archive), hourly", "years": len(hourly_max), "period": f"{years[0]}-{years[1]}",
        "hourly": {"u": u1, "alpha": a1, **{f"T{T}": round(gumbel_value(u1, a1, T), 1) for T in (2, 5, 10, 25, 100)}},
        "daily": {"u": u24, "alpha": a24, **{f"T{T}": round(gumbel_value(u24, a24, T), 1) for T in (2, 5, 10, 25, 100)}},
        "wettest_hour": round(max(hourly_max), 1), "wettest_day": round(max(daily_max), 1),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(clim, indent=1))
    return clim


def drain_capacity_from_climate(clim, runoff=0.85):
    """Drains carry the runoff of a ~2-year storm: capacity ~= C x I(2-yr, 1 h)."""
    return float(np.clip(runoff * clim["hourly"]["T2"], 12, 40))


# ---------------------------------------------------------------- antecedent
def fetch_antecedent(lat, lon):
    j = _get(FORECAST, {"latitude": lat, "longitude": lon, "daily": "precipitation_sum", "past_days": 7, "forecast_days": 1,
                        "hourly": "soil_moisture_0_to_1cm", "timezone": "Asia/Kolkata"})
    d = [v or 0 for v in j["daily"]["precipitation_sum"]][:7]
    r7, r3 = float(sum(d)), float(sum(d[-3:]))
    sm = [v for v in (j.get("hourly", {}).get("soil_moisture_0_to_1cm") or []) if v is not None]
    soil = float(sm[min(len(sm) - 1, 7 * 24)]) if sm else None
    return antecedent_effect(r3, r7, soil)


def antecedent_effect(r3, r7, soil=None):
    """Wet ground: more runoff. Recent heavy rain: drains silted and partly full."""
    wet = np.clip(r7 / 150, 0, 1)
    if soil is not None:
        wet = max(wet, np.clip((soil - 0.25) / 0.2, 0, 1))
    runoff = 0.78 + 0.17 * wet                 # 0.78 dry ground -> 0.95 saturated
    drain_eff = 1 - 0.25 * np.clip(r3 / 120, 0, 1)  # up to 25% less after 120 mm in 3 days
    label = "dry" if wet < 0.2 else "damp" if wet < 0.6 else "saturated"
    return {"rain_3d": round(r3, 1), "rain_7d": round(r7, 1), "soil": soil, "runoff": round(float(runoff), 3),
            "drain_eff": round(float(drain_eff), 3), "ground": label}


# --------------------------------------------------------------------- tide
def fetch_tide(lat, lon, coast_lon=80.30):
    """Sea level just off the coast east of the area."""
    j = _get(MARINE, {"latitude": lat, "longitude": max(lon + 0.06, coast_lon), "hourly": "sea_level_height_msl",
                      "past_days": 1, "forecast_days": 1, "timezone": "Asia/Kolkata"})
    now = dt.datetime.now(TZ)
    t, v = [], []
    for ts, val in zip(j["hourly"]["time"], j["hourly"]["sea_level_height_msl"]):
        if val is None:
            continue
        t.append((dt.datetime.fromisoformat(ts).replace(tzinfo=TZ) - now).total_seconds() / 60)
        v.append(float(val))
    if not v:
        return None
    return tide_from_series(np.array(t), np.array(v))


def tide_from_series(t_min, level):
    lo, hi = float(level.min()), float(level.max())
    amp = max(hi - lo, 0.05)
    nxt = [(tt, lv) for tt, lv in zip(t_min, level) if 0 <= tt <= 360]
    high = max(nxt, key=lambda x: x[1]) if nxt else None
    return {"t_min": t_min.tolist(), "level": level.tolist(), "low": lo, "high": hi, "amp": amp,
            "next_high_min": round(high[0]) if high else None, "next_high_m": round(high[1], 2) if high else None}


def tide_factor(tide, t_min, strength=0.4):
    """Drain capacity multiplier for low-lying cells: 1 at low tide, 1-strength at high tide."""
    if not tide:
        return 1.0
    lv = float(np.interp(t_min, tide["t_min"], tide["level"]))
    frac = (lv - tide["low"]) / tide["amp"]
    return 1 - strength * float(np.clip(frac, 0, 1))


# ------------------------------------------------------------------ history
def documented_reports(area):
    """Real past closures that match streets in this area, as flood-history records."""
    import re
    out = []
    segs = area.segments
    norm = lambda x: re.sub(r"[^a-z ]", "", x.lower().replace(".", ""))
    for ev in HISTORY:
        for key in ev["subways"] + ev["roads"]:
            is_subway = key in ev["subways"]
            base = norm(key).replace(" road", "").strip()
            if is_subway:
                # the subway itself: an underpass, or a street named "<key> subway"
                pat = re.compile(rf"\b{re.escape(base)}\b")
                hits = [s for s in segs if pat.search(norm(s.name)) and (s.underpass or "subway" in norm(s.name))]
            elif " " in norm(key) or key.endswith("Road"):
                pat = re.compile(rf"^{re.escape(base)} (road|rd|salai|street|st)\b")  # "Park Road", not "Tech Park Rd"
                hits = [s for s in segs if pat.search(norm(s.name))]
            else:
                pat = re.compile(rf"\b{re.escape(base)}\b")
                hits = [s for s in segs if pat.search(norm(s.name))]
            if not hits:
                continue
            best = max(hits, key=lambda s: (s.underpass, s.length_m))
            kind = "subway" if is_subway else "road"
            out.append({"id": f"h-{ev['date']}-{base}", "text": f"{key}{'' if key.endswith('Road') else ' ' + kind} closed due to waterlogging ({ev['event']})",
                        "street": best.name, "locality": best.locality, "date": ev["date"], "depth_cm_reported": 45,
                        "lat": best.mid[0], "lon": best.mid[1], "seg": best.idx, "source": "documented", "url": ev["source"]})
        for a in ev["areas"]:
            locs = [L for L in (area.meta.get("localities") or []) if a.lower() in L["name"].lower()]
            for L in locs:
                out.append({"id": f"h-{ev['date']}-{a}", "text": f"Waterlogging reported in {a} ({ev['event']})",
                            "street": "", "locality": L["name"], "date": ev["date"], "depth_cm_reported": 30,
                            "lat": L["lat"], "lon": L["lon"], "seg": None, "source": "documented", "url": ev["source"]})
    return out


# ------------------------------------------------------------------ replays
def fetch_replay(bbox, key, n=3):
    """Recorded hourly rain for a past storm at an n x n grid, centred on its wettest 6 hours."""
    ev = REPLAYS[key]
    start = (dt.date.fromisoformat(ev["start"]) - dt.timedelta(days=7)).isoformat()  # include the week before
    lats, lons = _grid_points(bbox, n)
    pts = [(la, lo) for la in lats for lo in lons]
    js = _get(ARCHIVE, {"latitude": ",".join(f"{p[0]:.4f}" for p in pts), "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
                        "start_date": start, "end_date": ev["end"], "hourly": "precipitation", "timezone": "Asia/Kolkata"}, timeout=40)
    js = js if isinstance(js, list) else [js]
    times = js[0]["hourly"]["time"]
    arr = np.nan_to_num(np.array([[v if v is not None else np.nan for v in d["hourly"]["precipitation"]] for d in js], dtype=float))
    mean = arr.mean(axis=0)
    first = next(i for i, t in enumerate(times) if t[:10] >= ev["start"])
    win = np.convolve(mean, np.ones(6), mode="valid")
    i0 = first + int(win[first:].argmax())
    now_i = max(first, i0 - 1)  # 'now' = an hour before the wettest 6 hours begin
    before = mean[:now_i]
    ante = antecedent_effect(float(before[-72:].sum()), float(before[-168:].sum()))
    t0 = dt.datetime.fromisoformat(times[now_i])
    t_min = np.array([(dt.datetime.fromisoformat(t) - t0).total_seconds() / 60 - 30 for t in times])
    return {"mmph": arr, "t_min": t_min, "n": n, "step": 60, "fetched": t0.strftime("%-d %b %Y, %-I:%M %p"),
            "now": (t0.hour, t0.minute), "points": pts, "replay": ev["name"], "date": t0.strftime("%-d %b %Y"),
            "total_6h": round(float(win[first:].max()), 1), "antecedent": ante}
