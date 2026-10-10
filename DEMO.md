# Demo plan: 2 minutes 50 seconds

The video is the only thing judges see. Rules that shape this plan:

- **Under 3:00.** Aim for 2:50 so the upload never trips the limit.
- **Every AWS tool must be visible on screen, running.** A tool named only in the voiceover doesn't count.
- **No intro slide, no team slide, no music under the voice.** Open on the problem.
- Say only true things. If the agent ran as a template, don't call it the agent. If you used `make tick-py` instead of `make tick`, don't say SAM.

The story: **Priya rides home at 6 PM. At 4:10 her route is fine. At 4:25 a new nowcast lands, her route goes under water, and her phone tells her before she leaves.**

---

## Setup (30 minutes before recording)

```bash
make up && make data          # real Velachery; wait for "Ready" in: make logs
# then in the app: How it works → System → Rain data → Demo storm  (live rain is unpredictable; record the story on the demo storm)
make model                    # ollama pull llama3.2:3b, and keep Ollama running
make reset                    # back to the 4:10 PM nowcast
```

Then:

1. Open `http://localhost:8080` in Chrome at **1440 × 900** (DevTools device toolbar, or a window that size). Zoom 100%. Hide bookmarks bar. Use a guest profile so no personal tabs or autofill show.
2. Account menu → **How it works** → System: check OpenSearch says *connected*, the agent says *ready*, and alerts say *LocalStack SNS → SQS*. Fix anything else first (README → Troubleshooting).
3. Pick your story numbers: on the **Home at 6:00 PM** row, note the 6:00 PM depth now (4:10 nowcast) and after `make tick`. Run `make tick` once to rehearse, then `make reset`. Write the street name and both depths on a sticky note for the voiceover.
4. Warm the agent: open the commute directions once so Ollama has the model loaded (the first call is slow).
5. Terminal for the split screen: large font (18 pt+), dark theme, `cd serverless`, clear history.
6. Phone or second browser window signed into Google Maps for the handoff shot (optional but strong).

Record the screen and the voice **separately**. Capture each shot below as its own clip, then cut them to the voiceover. Burn in captions; judges may watch muted.

---

## Shot list

Record on a laptop at 1440 × 900 (dark theme looks best on video), and grab two or three shots on a phone (Chrome DevTools device mode is fine) to show it's a real mobile app.

| # | Time | On screen | Voiceover |
|---|---|---|---|
| 1 | 0:00 – 0:09 | Black. White text, one line at a time: *"July 9, 2026. Surat: 9 dead, 3,400 rescued."* / *"Delhi: streets underwater, a building down in Rohini."* / *"Every monsoon, the same streets flood. Every time, it's a surprise."* | "Every monsoon, the same streets flood." |
| 2 | 0:09 – 0:20 | The app opens: the boot lines tick, the map draws in, and the blue dot finds you. | "This is Velachery in Chennai. Every street from OpenStreetMap, on real terrain from AWS open data. We forecast how deep the water will be on each one." |
| 3 | 0:20 – 0:40 | Press **Space**. The rain sweeps in; streets turn blue, then amber, then red; the timeline fills. Scrub back to 6:00. | "Every ten minutes, six hours ahead. Rain the drains can't take runs downhill, fills the low spots, and spills over. Red means too deep to cross." |
| 4 | 0:40 – 0:55 | Type a street in the search bar: results show their depth. Tap one: the sheet shows its depth curve and past reports, labelled OpenSearch. | "Search any street. It shows how deep it'll get and when, and what residents reported there before, searched by text and location in OpenSearch." |
| 5 | 0:55 – 1:22 | Tap **Home at 6:00 PM**: directions, Clear. **Split screen:** terminal runs `sam local invoke PollerFunction` → `published alert for priya`. The notification drops in ("via LocalStack SNS → SQS"); the route turns red, the green dry route appears. | "Priya rides home at six. At 4:10 her route is clear. At 4:25 a new nowcast lands. A Lambda built with SAM re-runs the forecast and publishes to SNS; on LocalStack it fans out to SQS, and the alert reaches her before she leaves." |
| 6 | 1:22 – 1:40 | Tap **why?**: the agent's tool calls: `check_route`, `flood_history`, `best_departure`. | "The advice is written by an agent built with the Strands Agents SDK. Every number comes from a tool call." |
| 7 | 1:40 – 1:58 | Tap **Start in Google Maps**. Cut to Google Maps on a phone with the route through the waypoints. | "Then it gets out of the way. One tap and she's navigating in Google Maps, on a route that stays dry." |
| 8 | 1:58 – 2:15 | Phone: search a town 60–100 km away → **Build trip forecast** → the corridor appears with its main roads, the route checked at the time you reach each stretch. | "Going further? It builds a forecast for the whole corridor, and checks each stretch for when you'll actually get there." |
| 9 | 2:15 – 2:35 | Account menu → **Ravi**: his ward only, others hidden by policy. **Control room**: hospitals and schools losing access pulse on the map. Tap the Hospitals chip as Priya: *Restricted layer*. Flash `paani.cedar`. | "The people who fix floods see something different, and every request is checked by Cedar policies. Who sees what is a policy edit, not a code change." |
| 10 | 2:35 – 2:50 | Terminal: `finch compose up`. Then the phone: installed app icon, opening offline. End card: **Paani Kahan** / *"You keep using Google Maps. We make sure it doesn't send you into a flood."* | "It runs on a laptop with Finch, installs on your phone, and works offline." |

**Optional live beat (10 s, between shots 7 and 8):** switch Rain data to *Live*, show "Live · Open-Meteo 15-min · updated 5:07 PM", tap **Report water**, choose *Knee*, and the street turns red on the map. Say: "In live mode it uses the real forecast, refreshed every 15 minutes, and anyone can report water; their reports correct the forecast nearby." Say it was switched on; never imply the demo storm was real.

**Optional Chennai beat (10 s):** How it works → System → replay **Cyclone Michaung**. Say: "This is the recorded rain from Cyclone Michaung, replayed on today's streets." Tap a subway: *likely closed*.

If time is tight, cut shot 8 (long trips) first; it needs a network connection and a minute to build.

Voiceover is about 320 words: comfortable at a normal pace with room to breathe.

---

## Craft notes

- **First ten seconds decide whether a judge leans in.** Don't start on a logo or a terminal.
- **Let shot 3 breathe.** The rain sweep is the money shot; don't talk over its first two seconds.
- **Zoom in a little** (scroll) before shot 4 so the street labels and pins are readable on a phone screen.
- **In shot 5, make the terminal and notification land within a second of each other.** If the agent is slow, cut out the waiting frames; don't speed up the UI.
- **Fonts:** if the terminal text isn't readable on a phone screen, it's too small.
- **Export** at 1080p, upload to YouTube as **Unlisted**, then open the link in a signed-out / incognito window before submitting (the rules require this).

## If something breaks while recording

| Problem | Do this |
|---|---|
| Agent dot amber / card says "Template" | `ollama serve`, `make model`, reload. If it still fails, record with the template and say "the advice" rather than "the agent" in shot 5, and skip shot 6. |
| `sam` not installed | `make tick-py` runs the same handler without SAM. Then say "a Lambda function" and don't show the SAM command. Better: install SAM CLI, it takes two minutes. |
| LocalStack dot amber | `finch compose restart localstack`, wait 10 s. The app re-wires SNS → SQS by itself. |
| The story numbers are weak on your area (depth barely changes) | Delete `"persona"` from `data/velachery/area.json` (set it to `null`) and restart; a new commute is chosen. Or set `DEMO_NOW` / pick a different departure chip. |
| Need to redo a take | `make reset` returns to 4:10 PM in about a second. |
