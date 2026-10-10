# Paani Kahan: street-level flood forecasts for your commute

**Track 02: Heat and Water** (monsoon waterlogging, floods)

## The problem

India's 2026 monsoon ran above normal, and cities paid for it. On July 8–9 alone, Surat lost nine people and more than 3,400 were rescued; Delhi's streets went under and a building collapsed in Rohini. Most Indian urban areas have no scientifically designed stormwater drainage, so even a short heavy spell floods the same streets every year.

People still get caught out, because the warnings they get are city-wide ("heavy rain expected") and the map they use shows traffic, not water. The question someone actually has at 4 PM on a rainy day is narrower: *will the road I take home at 6 still be passable, and if not, which way is?* Nothing answers it.

## What we built

Paani Kahan forecasts water depth on every street around you, every 10 minutes, six hours ahead. Open it anywhere: street-level detail loads within 10 km, main roads and terrain for 100 km, in the background, nearest first, cached on disk and on the phone. It checks a saved commute at the time you'd actually reach each street, against a safe depth for how you travel (30 cm on a two-wheeler, 45 cm by car, 25 cm on foot). When a new rainfall nowcast puts the route under water, it sends one notification, then opens a dry route in Google Maps with waypoints that pin it there. People keep using the navigation app they already use; we only change which streets it sends them down.

Ward officers get pump priorities for their own locality. Emergency control sees which hospitals and schools are about to lose road access.

**How it loads:** the map is cut into tiles (~5 km street-level, ~38 km main roads). Each tile is fetched, built and forecast independently by a priority scheduler (what's on screen, then your route, then near you, then the region), with per-service rate limits, retries and circuit breakers, stale-while-revalidate forecasts, and server-sent events telling the phone which tiles are ready. Rain comes from a fixed lattice shared by all tiles, so they agree at their seams. See ARCHITECTURE.md.

**The model:** priority-flood the terrain to find every depression and every cell's downhill direction; each 10 minutes, rain above drain capacity becomes runoff; route it downhill, store it in depressions until they spill; convert stored volume and discharge to depth. A street's depth is the deepest point along it. Drain capacity per locality is calibrated from past flood reports. Flow accumulation is vectorised by topological level; a tile's 6-hour forecast takes about half a second.

**Tuned to Chennai:** drain capacity comes from 35 years of hourly rainfall at the area (ERA5, Gumbel fit, sized for a 2-year storm); runoff and drain efficiency follow the last week's rain and soil moisture; drains in low-lying streets slow down around high tide; subways are modelled as pumped depressions and treated as closed above 30 cm, as the city closes them; documented closures from the Dec 2021 cloudburst and Cyclone Michaung are shown as flood history; and real past storms can be replayed. Routes avoid roads under construction and barriers (OpenStreetMap), live closures and flooding (TomTom, optional key) and residents' "road closed" reports.

## Where AWS fits

- **OpenSearch**: place search across every loaded tile (search-as-you-type with distance decay), the forecast as one document per wet street with depth per 10-minute frame (re-indexed tile by tile), past flood reports (full-text + geo) and facilities. It answers "flooding near you at 6 PM", the per-locality aggregation behind pump priority, flood history near a street, and which hospitals have a flooded street within 200 m.
- **Strands Agents SDK**: the advisory agent. It calls three tools (route check, flood history search, best departure time) and writes the notification. Runs on a local model through Ollama.
- **Cedar**: authorizes every request. Route checks are public, a saved commute is visible only to its owner, officers see only their own locality, vulnerable-site data is for emergency control.
- **AWS SAM CLI + LocalStack**: the nowcast poller is a SAM-defined Lambda on a 15-minute schedule. It re-runs the forecast and publishes alerts to SNS, which fans out to the SQS queue the app listens on. Runs locally with `sam local invoke` against LocalStack.
- **Finch**: `finch compose up` runs the whole stack on a laptop.
- **AWS Registry of Open Data**: Terrain Tiles for elevation, fetched per tile and cached.

## Honest limits

Elevation is SRTM-derived and coarse for dense cities; production would use CartoDEM or LiDAR plus drain-network maps. In live mode the rain comes from Open-Meteo (15-minute forecast on a grid over the area, refreshed every 15 minutes) and flood reports come from residents using the app, which correct the forecast nearby; there is no public live feed of flood complaints to seed it. The video uses the demo storm: an elevated, scripted rain band centred on the streets on screen (labelled in the app), so it is reproducible on a dry day; IMD radar nowcasts would be a better production source. Depths are forecasts, not guarantees.

## AI tools used

<!-- List every AI coding tool you used. Edit this line before submitting. -->
Claude (Anthropic) for code generation and documentation.

## Links

- Repository: <!-- your repo URL -->
- Demo video: <!-- your YouTube URL (unlisted is fine; check it opens signed-out) -->
