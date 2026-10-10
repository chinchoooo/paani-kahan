# Architecture

Paani Kahan forecasts water depth on every street within reach of you, six hours ahead, and checks your route against it. v3 is built around one idea: **the map is tiles, and every tile loads, builds and forecasts on its own, in the background, by priority.** Nothing blocks the person using it.

```
             ┌──────────── browser (PWA) ─────────────┐
             │ tile manager · canvas renderer · views │
             │ SW cache (offline) · SSE · LRU memory  │
             └───────┬──────────────▲─────────────────┘
      GET /api/tile/z/x/y           │ SSE: tiles ready / forecast ready / run / alert
      GET …/forecast · …/terrain.png│
      POST /api/focus · /api/route  │
             ┌───────▼──────────────┴─────────────────┐
             │ FastAPI (thin)  →  World               │
             │   Scheduler: keyed, prioritised jobs   │
             │   ├─ net pool:  fetch, index           │
             │   └─ cpu pool:  build, forecast        │
             │   RainService (seamless rain field)    │
             │   ContextService (climate, ground, tide)│
             │   Composite router (across tiles)      │
             └──┬─────────┬────────────┬──────────────┘
      Overpass  │ Terrain │ Open-Meteo │ OpenSearch · Cedar · Strands · SNS→SQS
      (OSM)     │ Tiles   │ (+ ERA5)   │
                ▼ disk cache (data/world/)
```

## Tiles and levels of detail

Web Mercator tiles, two levels:

| Level | Zoom | Size at Chennai | Roads | Model grid | Loaded |
|---|---|---|---|---|---|
| detail | 13 | ~4.8 km | every street | 20 m | within `DETAIL_KM` (10 km) of you, plus whatever is on screen, plus route corridors |
| region | 10 | ~38 km | motorway to secondary | 150 m | within `REGION_KM` (100 km) of you |

Both levels come out at ~300 × 300 cells per tile, so every job costs about the same. Region tiles give the zoomed-out view and long-trip routing; detail tiles replace them wherever they're ready. The client draws region tiles clipped to the area that detail tiles don't cover yet, so the map is never blank while detail streams in.

**Seams.** Neighbouring tiles fetch the same OSM ways. Each street segment is owned by exactly one tile (the one containing its midpoint), so tiles join with no duplicates and no gaps. Segment ids are global: `13/5921/3798:412`. Routing graphs from many tiles join on OSM node ids, which are the same across tiles and levels.

**Halo.** Water doesn't stop at a tile edge. Each tile's flood model runs on the tile plus a 15% halo of terrain on every side (8% for region tiles), so water flowing in from upslope is counted, then the result is cropped to the tile.

## The pipeline (per tile)

```
fetch (net) ──► build (cpu) ──► forecast for run R (cpu) ──► index (net)
OSM + terrain    Area + model    rain(R) + context → depth   OpenSearch
```

Each step is a keyed job on `app/world/scheduler.py`:

- **Keyed.** Asking twice does nothing; a job already queued just has its priority improved.
- **Prioritised** by `(class, distance from you)`. Classes: `VISIBLE` (the client asked for this tile in the last 2 min) > `ROUTE` (a route or the commute passes through) > `NEAR` (street level near you) > `REGION` > `IDLE`.
- **Pools.** `net` for I/O (fetches, indexing), `cpu` for building and forecasting, so a slow upstream never stalls the model and vice versa.
- **Backoff.** Failed jobs retry with exponential backoff (15 s … 5 min, then hourly).

**Stale-while-revalidate.** A tile keeps its last three forecasts. When a new rain run starts, the newest finished forecast is served (flagged `stale`) while the new one computes, visible tiles first. The client is told by SSE when each tile's forecast lands and refetches just that tile.

## Data sources and caching

`app/world/net.py` wraps every upstream in an `Upstream`: per-host concurrency and pacing (Overpass gets one query at a time), retries with backoff, and a **circuit breaker** that opens after repeated server-side failures (not after 4xx, which are our fault), so a failing service degrades one layer instead of the whole app.

| Data | Source | Cache |
|---|---|---|
| Streets, water, localities, hospitals, schools | OpenStreetMap via Overpass (3 mirrors, failover) | per tile, gzip JSON on disk, 30 days; a stale copy is used if a refresh fails |
| Elevation | AWS Terrain Tiles (Registry of Open Data), terrarium PNG | per PNG on disk, forever, shared by every tile that overlaps it |
| Rain (live) | Open-Meteo 15-minute forecast on a fixed 0.125° lattice | in memory per lattice point; refreshed every 15 min within 30 km of you, hourly further out |
| Rain (replays) | ERA5 hourly via Open-Meteo archive | per lattice point per storm, on disk, forever |
| Drain sizing | 35 years of hourly ERA5 per 0.25° cell, Gumbel fit | on disk, forever |
| Wet ground, tide | Open-Meteo forecast and marine | per 38 km zone, 1 h |
| Closures | TomTom Traffic Incidents (optional key) | refreshed with the forecast |

**Why a lattice for rain.** Every tile asks one service for rain on its own grid, interpolated from the same fixed lattice, so neighbouring tiles always agree at their seams and each point is fetched once however many tiles use it. Points are fetched only where tiles need them, in batches of 60.

**Payloads.** Tile geometry is integer micro-degree deltas (about 3× smaller than raw JSON before gzip), immutable per build and ETag'd. Forecasts are base64 `uint8` arrays: depth per segment per 10-minute frame, a half-resolution depth raster, coarse rain. A detail tile is ~30 KB of geometry over the wire.

## The flood model

`app/engine/hydro.py`. Priority-flood terrain fill (D8 receivers), depressions labelled with their spill level, then every 10 minutes: rain × runoff − drain capacity → runoff routed downhill; depressions store water until they spill; ponded depth from stored volume and depression shape; moving-water depth from Manning's equation. Street depth = deepest point along the street.

**v3 vectorisation.** Flow accumulation runs by topological level: every cell at level *k* has all its donors at lower levels, so a level is one `np.add.at` scatter. Cells in a filling depression redirect to a virtual node for that depression. Ponding is a vectorised bisection over all depressions at once. Results are identical to the v2 reference loop (tested) and ~2.5× faster; a 6-hour forecast for a 300 × 300 tile takes about 0.5 s on one core.

## Routing

The router is built over a **composite** of the tiles a trip needs: every street-level tile in the corridor for trips up to 14 km; for longer trips, street level at both ends and main-road tiles in between (main-road segments are dropped wherever a street-level tile covers them). Routers are cached by tile set. Depth is read at the time you'd reach each segment, from each tile's forecast. If the tiles aren't ready, the API answers `202 Preparing` with progress, schedules them at `ROUTE` priority, and the client retries.

## The demo storm

The demo storm is a geographic rain band (an elevated storm: peaks of 60–85 mm/h, a rare, very heavy downpour) centred where you are. It crosses from the south-east and is timed to reach you around 6 PM. When it's switched on, the 3 × 3 street-level tiles around you are forecast for both nowcasts (4:10 PM and the 4:25 PM update), and a commute is picked that tells the story: watch at 4:10, flooded at 4:25, with a dry route. The update is precomputed, so `tick` is instant. Only the rain is scripted; streets, terrain, drains and routing are real.

## Search and analytics (OpenSearch)

| Index | Document | Used for |
|---|---|---|
| `pk-places` | each named street (once per tile), locality, hospital, school | `search_as_you_type` + `gauss` distance decay from you |
| `pk-forecast` | **one document per wet street segment** with `d0…d36` = depth at each frame | "streets over 15 cm within 1.2 km at 6 PM" = `geo_distance` + `range d11`, sorted by `d11`; locality aggregations for ward ops; `msearch` for hospitals |
| `pk-reports` | documented closures, resident reports, seed data (demo) | full-text + geo flood history |
| `pk-pois` | hospitals, schools | vulnerable sites |

One document per street (not per street per frame) keeps the index ~37× smaller and lets each tile be re-indexed independently: bulk upsert by segment id, then `delete_by_query` on that tile's older versions. The app starts on an in-memory store and switches to OpenSearch when it connects, re-indexing what is already loaded.

## Client

No framework, no build step. `app/web/app.js`:

- **Tile manager.** Every 350 ms: work out the visible tiles at both levels, fetch geometry (4 in flight, nearest the centre first) and forecasts (4 in flight), keep ~100 tiles in memory (LRU by last on-screen time).
- **Renderer.** Canvas 2D. Per tile: `Path2D` per road class, a spatial hash for hit-testing, depth raster and rain canvases rebuilt only when time, mode or theme changes, wet-street paths bucketed by depth. Terrain shading is multiplied in (light) or screened inverted (dark).
- **Focus.** On pan or zoom end and on a GPS fix, `POST /api/focus` tells the server where you are, which re-prioritises the queue.
- **Offline.** The service worker serves the app shell cache-first, tiles and forecasts network-first with fallback, and terrain PNGs cache-first. On Chromium, Periodic Background Sync re-checks the commute about every 15 minutes and notifies.
- **No boot screen.** The map shows immediately and fills in; a 2 px bar at the top shows tile progress, and the sheet shows what's still loading.

## Failure modes

| Failure | What happens |
|---|---|
| Overpass down or rate-limited | mirror failover; cached tiles still served (stale if needed); new tiles retry with backoff; the System tab shows the upstream state |
| Open-Meteo down | the circuit opens; tiles keep their last forecast (flagged stale); the label says so |
| ERA5 archive slow or missing | drains use the 18 mm/h default and say so; replays report "unavailable" |
| OpenSearch not up yet | in-memory store; switches over when it connects |
| Ollama or the model unavailable | template advice from the same tool calls, labelled |
| LocalStack missing | in-process alert delivery, labelled |
| Phone offline | last tiles and forecast from the service worker, "Offline · saved 4:25 PM" |

## Testing

- `tests/fake_upstreams.py` serves Overpass JSON, terrarium PNGs and Open-Meteo in their real wire formats from the synthetic world, so the production fetch, parse, cache and forecast paths run end to end without the internet.
- `DATA_SOURCE=synthetic` runs the whole app on an invented, seamless world (labelled in the UI).
- The vectorised model is checked against the v2 reference implementation (identical output).
