"""Record the demo video automatically: drives the running app through the whole story,
with captions burned in, and writes demo.mp4.

    pip install playwright imageio-ffmpeg pyttsx3
    python -m playwright install chromium
    python demo/record.py                       # Velachery; app at http://localhost:8080
    python demo/record.py --lat 13.0418 --lon 80.2341 --voice

What it does
  1. Prepares the app (not recorded): pre-loads the streets, starts the demo storm centred
     on --lat/--lon, waits for the demo commute, resets to the 4:10 PM nowcast, warms the
     long-trip corridor and the agent.
  2. Records the shot list from DEMO.md at 1440x900, plus a phone segment at 390x844.
  3. For the nowcast update it runs the real SAM Lambda (`sam local invoke PollerFunction`)
     so the alert really travels SNS -> SQS on LocalStack. If SAM isn't installed it runs
     the same Lambda handler directly (--update python), or the app's in-process update
     (--update api), and the caption says which one was used. Say only true things.
  4. With --voice, speaks each caption with your system's built-in voice (Windows SAPI via
     pyttsx3) and mixes it in. Without it, the video is silent; record your own voice over it.

Captions are part of the video (judges often watch muted). Everything shown is the live app.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--app", default="http://localhost:8080")
ap.add_argument("--lat", type=float, default=12.9790)
ap.add_argument("--lon", type=float, default=80.2230)
ap.add_argument("--far", default="Kanchipuram", help="a town 60-100 km away for the long-trip shot (empty to skip)")
ap.add_argument("--update", choices=["sam", "python", "api"], default="sam")
ap.add_argument("--voice", action="store_true", help="speak the captions with the system voice")
ap.add_argument("--theme", choices=["dark", "light"], default="dark")
ap.add_argument("--out", default="demo.mp4")
ap.add_argument("--skip-prep", action="store_true")
A = ap.parse_args()


# ------------------------------------------------------------------ helpers
def call(path, body=None, timeout=60):
    req = urllib.request.Request(A.app + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"null")


def wait(cond, what, timeout=600, every=3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = cond()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        print(f"\r  waiting for {what}… {int(time.time() - t0)}s ", end="", flush=True)
        time.sleep(every)
    raise SystemExit(f"\nTimed out waiting for {what}")


def ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise SystemExit("Needs ffmpeg: pip install imageio-ffmpeg")


# ------------------------------------------------------------------ the script
# (caption, minimum seconds on screen). Edit the words freely; keep them true.
LINES = {
    "title": [("July 2026. Surat: nine dead, 3,400 rescued.", 3.2),
              ("Delhi: streets under water, a building down in Rohini.", 3.2),
              ("Every monsoon, the same streets flood. Every time, it's a surprise.", 3.6)],
    "open": "Open it anywhere. Every street around you from OpenStreetMap, main roads for a hundred kilometres, on terrain from AWS open data, loaded in the background.",
    "play": "Every ten minutes, six hours ahead. Rain the drains can't take runs downhill and fills the low spots. Blue is water. Red is too deep to cross.",
    "search": "Search any street: how deep it gets, when, and what was reported there before, searched by text and location in OpenSearch.",
    "commute": "Priya rides home at six. At 4:10 her route is passable.",
    "update_sam": "At 4:25 a new nowcast lands. A Lambda built with SAM re-runs the forecast and publishes to SNS. On LocalStack it fans out to SQS, and the alert reaches her before she leaves.",
    "update_python": "At 4:25 a new nowcast lands. The poller Lambda re-runs the forecast and publishes to SNS on LocalStack, which fans out to SQS, and the alert reaches her before she leaves.",
    "update_api": "At 4:25 a new nowcast lands. The forecast re-runs, and the alert reaches her before she leaves.",
    "why": "The advice is written by an agent built with the Strands Agents SDK. Every number comes from a tool call.",
    "why_template": "The advice comes from the same three tool calls: route check, flood history, best departure.",
    "gmaps": "Then it gets out of the way. One tap, and she's in Google Maps on a route that stays dry.",
    "far": "Going further? Main roads for a hundred kilometres are already loaded, checked for when you'll reach each stretch.",
    "cedar": "Ward officers and the control room see something different, and every request is checked by Cedar policies.",
    "system": "Under the hood: tiles load by priority, from OpenStreetMap, AWS Terrain Tiles and Open-Meteo; every OpenSearch query and Cedar decision is logged.",
    "phone": "It's a map app on your phone: it installs, and works offline.",
    "end": [("Paani Kahan", 1.6), ("You keep using Google Maps. We make sure it doesn't send you into a flood.", 4.0)],
}
DEMO_BADGE_NOTE = "Demo storm: scripted rain on real streets"

OVERLAY_JS = """
(() => {
  if (document.getElementById('pk-cap')) return;
  const st = document.createElement('style');
  st.textContent = `
  #pk-cap{position:fixed;left:50%;bottom:118px;transform:translateX(-50%);z-index:9999;max-width:min(880px,86vw);
    background:rgba(5,5,5,.86);color:#fff;font:600 22px/1.35 'Inter Tight',system-ui,sans-serif;letter-spacing:-.01em;
    padding:12px 18px;border-radius:12px;text-align:center;transition:opacity .35s;pointer-events:none}
  #pk-cap:empty{opacity:0}
  #pk-cur{position:fixed;z-index:10000;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;
    background:rgba(255,255,255,.35);border:2.5px solid #1f3dff;pointer-events:none;transition:transform .12s}
  #pk-cur.down{transform:scale(.7)}
  #pk-tag{position:fixed;left:16px;bottom:12px;z-index:9999;font:600 11px system-ui;color:#fff;background:rgba(31,61,255,.9);
    padding:3px 8px;border-radius:5px;pointer-events:none}`;
  document.head.appendChild(st);
  const c = document.createElement('div'); c.id = 'pk-cap'; document.body.appendChild(c);
  const k = document.createElement('div'); k.id = 'pk-cur'; document.body.appendChild(k);
  addEventListener('mousemove', e => { k.style.left = e.clientX + 'px'; k.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => k.classList.add('down'), true);
  addEventListener('mouseup', () => k.classList.remove('down'), true);
})();
"""

CARD_HTML = """<!doctype html><html><head><meta charset=utf-8><link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@500;800&display=swap">
<style>html,body{margin:0;height:100%;background:#050505;color:#f5f5f2;font-family:'Inter Tight',system-ui,sans-serif}
body{display:grid;place-content:center;gap:22px;padding:0 10vw}
p{margin:0;font-size:34px;font-weight:500;letter-spacing:-.02em;opacity:0;transform:translateY(8px);transition:all .6s}
p.on{opacity:1;transform:none} p.big{font-size:72px;font-weight:800;letter-spacing:-.04em}
.drop{display:inline-block;width:40px;height:54px;background:#1f3dff;clip-path:path("M20 0C20 0 40 27 40 36a20 20 0 0 1-40 0C0 27 20 0 20 0Z");margin-right:18px;vertical-align:-6px}</style>
</head><body>{BODY}</body></html>"""


class Recorder:
    """Plays the shot list on a page; keeps a timeline of captions for the voice track."""

    def __init__(self, page, t0):
        self.page, self.t0, self.timeline = page, t0, []

    def now(self):
        return time.monotonic() - self.t0

    def overlay(self):
        self.page.evaluate(OVERLAY_JS)

    def say(self, text, min_s=0.0):
        """Show a caption and hold for long enough to read (and speak) it."""
        print(f"    {self.now():6.1f}s  {text[:60]}", flush=True)
        self.overlay()
        self.page.evaluate("t => { document.getElementById('pk-cap').textContent = t; }", text)
        self.timeline.append((self.now(), text))
        hold = max(min_s, 0.6 + len(text.split()) * 0.33)
        return hold

    def clear(self):
        self.page.evaluate("() => { const c = document.getElementById('pk-cap'); if (c) c.textContent = ''; }")

    def pause(self, s):
        self.page.wait_for_timeout(int(s * 1000))

    def move(self, x, y, steps=10):
        self.page.mouse.move(x, y, steps=steps)

    def click(self, selector, pause_after=0.6, nth=0, timeout=8000):
        loc = self.page.locator(selector).nth(nth)
        t = time.monotonic()
        loc.wait_for(state="visible", timeout=timeout)
        if time.monotonic() - t > 2:
            print(f"      (waited {time.monotonic() - t:.1f}s for {selector})", flush=True)
        b = loc.bounding_box()
        self.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        self.pause(0.25)
        self.page.mouse.down(); self.pause(0.08); self.page.mouse.up()
        self.pause(pause_after)

    def type(self, selector, text):
        self.click(selector, 0.3)
        for ch in text:
            self.page.keyboard.type(ch)
            self.pause(0.09)


def cards(page, rec, lines, cls=""):
    html = "".join(f'<p class="{cls if i == 0 and cls else ""}">{"<span class=drop></span>" if cls and i == 0 else ""}{t}</p>' for i, (t, _) in enumerate(lines))
    page.set_content(CARD_HTML.replace("{BODY}", html))
    page.wait_for_timeout(400)
    for i, (t, s) in enumerate(lines):
        page.evaluate(f"document.querySelectorAll('p')[{i}].classList.add('on')")
        rec.timeline.append((rec.now(), t))
        page.wait_for_timeout(int(s * 1000))


# ------------------------------------------------------------------ prepare (not recorded)
def prepare():
    print("Preparing the app (not recorded)…")
    st, w = call("/api/world")
    call("/api/focus", {"lat": A.lat, "lon": A.lon})
    wait(lambda: (lambda p: p["detail"]["ready"] + p["detail"]["empty"] + p["detail"]["failed"] >= p["detail"]["tiles"])(
        call(f"/api/progress?lat={A.lat}&lon={A.lon}")[1]["progress"]), "streets around you", timeout=900)
    print("\n  streets loaded")
    call("/api/rain", {"source": "demo", "lat": A.lat, "lon": A.lon})
    wait(lambda: call("/api/world")[1]["demo"]["state"] not in ("preparing",), "the demo commute", timeout=600)
    demo = call("/api/world")[1]["demo"]
    print(f"\n  demo commute: {demo['state']} {json.dumps((demo.get('persona') or {}).get('check'))}")
    if demo["state"] != "ready":
        raise SystemExit("No demo commute here. Try a spot a couple of km away (--lat/--lon).")
    call("/api/internal/reset", {})
    p = demo["persona"]
    wait(lambda: call("/api/route", {"mode": "two_wheeler", "depart_min": 110})[0] == 200, "the commute route")
    call("/api/advise", {"mode": "two_wheeler", "depart_min": 110}, timeout=120)   # warms the model
    if A.far:
        st, g = call(f"/api/geocode?q={urllib.request.quote(A.far)}&lat={A.lat}&lon={A.lon}")
        if g.get("results"):
            f = g["results"][0]
            print(f"  warming the trip to {f['name']}…")
            wait(lambda: call("/api/route", {"mode": "car", "depart_min": 0, "from": [A.lat, A.lon], "to": [f["lat"], f["lon"]],
                                             "from_label": "Here", "to_label": f["name"]}, timeout=120)[0] == 200,
                 f"the corridor to {f['name']}", timeout=900, every=8)
            print()
        else:
            print(f"  couldn't find {A.far}; skipping the long-trip shot")
            A.far = ""
    print(f"  ready: {p['from_label']} -> {p['to_label']}")
    return p


def run_update():
    """The 4:25 PM nowcast update, through the real event path where possible."""
    if A.update == "sam" and shutil.which("sam"):
        cmd = ["sam", "local", "invoke", "PollerFunction"]
        cwd = ROOT / "serverless"
        kind = "sam"
    elif A.update in ("sam", "python"):
        cmd = [sys.executable, str(ROOT / "serverless" / "poller" / "app.py")]
        cwd = ROOT
        kind = "python"
        os.environ.setdefault("APP_URL", A.app)
        os.environ.setdefault("AWS_ENDPOINT", "http://localhost:4566")
    else:
        return "api", subprocess.Popen([sys.executable, "-c",
                                        f"import urllib.request,json;urllib.request.urlopen(urllib.request.Request('{A.app}/api/internal/tick',data=json.dumps({{'source':'demo-video','deliver':'direct'}}).encode(),headers={{'content-type':'application/json'}},method='POST'),timeout=180).read()"])
    print(f"  update via: {' '.join(cmd)}")
    return kind, subprocess.Popen(cmd, cwd=cwd, shell=(os.name == "nt"))


# ------------------------------------------------------------------ record
def main():
    from playwright.sync_api import sync_playwright
    persona = None if A.skip_prep else prepare()
    try:
        if call("/api/world")[1]["mode"] == "demo":
            call("/api/internal/reset", {})          # the story always starts at the 4:10 PM nowcast
            persona = persona or call("/api/commute")[1]["persona"]
    except Exception:  # noqa: BLE001
        pass
    tmp = Path(tempfile.mkdtemp(prefix="pk-demo-"))
    vids, timelines = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--hide-scrollbars"])
        # -------- desktop, 1440x900
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1, color_scheme=A.theme,
                                  record_video_dir=str(tmp / "desk"), record_video_size={"width": 1440, "height": 900},
                                  geolocation={"latitude": A.lat, "longitude": A.lon}, permissions=["geolocation"])
        page = ctx.new_page()
        t0 = time.monotonic()
        rec = Recorder(page, t0)
        cards(page, rec, LINES["title"])
        page.goto(A.app + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2200)
        rec.overlay()
        # shot 2: opens on your location; zoom out to the region, back in
        print('  ' + 'shot 2: opens on your location; zoom out', flush=True)
        rec.pause(rec.say(LINES["open"]) * 0.45)
        rec.move(920, 470)
        for _ in range(3):
            page.mouse.wheel(0, 420); rec.pause(0.2)
        rec.pause(2.2)
        for _ in range(3):
            page.mouse.wheel(0, -420); rec.pause(0.2)
        rec.pause(1.2)
        # shot 3: play the six hours, then scrub to 6 PM
        print('  ' + 'shot 3: play the six hours, then scrub t', flush=True)
        page.evaluate("S.t = 0; onTimeChange()")
        h = rec.say(LINES["play"])
        rec.click("#play", 0.1)
        rec.pause(max(h, 7.5))
        page.evaluate("setPlaying(false)")
        tb = page.locator("#track").bounding_box()
        rec.move(tb["x"] + tb["width"] * 110 / 360, tb["y"] + tb["height"] / 2)
        page.mouse.down(); page.mouse.up()
        rec.pause(1.2)
        # shot 4: search a flooded street
        print('  ' + 'shot 4: search a flooded street', flush=True)
        st, near = call(f"/api/nearby?lat={persona['to'][0] if persona else A.lat}&lon={persona['to'][1] if persona else A.lon}&t=110")
        street = (near.get("streets") or [{}])[0].get("name") or "Road"
        h = rec.say(LINES["search"])
        rec.type("#q", street[: max(4, min(len(street), 9))])
        rec.pause(1.6)
        wet = page.locator(".sg", has=page.locator(".d", has_text="cm"))
        if wet.count():
            rec.click(".sg:has(.d:has-text('cm'))", 1.0)
        else:  # open the deepest street from the "Flooding near you" list instead
            page.keyboard.press("Escape"); rec.pause(0.3)
            rec.click(".li[data-gid]", 1.0)
        rec.pause(max(h - 6, 2.5))
        rec.click("#s-back", 0.6)
        # shot 5: the commute, then the nowcast update through SAM -> SNS -> SQS
        print('  ' + 'shot 5: the commute, then the nowcast up', flush=True)
        h = rec.say(LINES["commute"])
        rec.click(".peek .commute", 1.0)
        rec.pause(max(h, 4.5))
        kind, proc = run_update()
        h = rec.say(LINES[f"update_{kind}"])
        t_up = time.time()
        try:
            page.locator("#toast:not([hidden])").wait_for(timeout=180000)
        except Exception:  # noqa: BLE001
            print("  (no alert toast seen)")
        rec.pause(max(1.5, h - (time.time() - t_up)))
        proc.wait(timeout=240)
        rec.pause(1.2)
        # shot 6: why? -> the agent's tool calls
        print('  ' + "shot 6: why? -> the agent's tool calls", flush=True)
        engine = page.evaluate("S.advice ? S.advice.engine : ''")
        h = rec.say(LINES["why"] if engine.startswith("strands") else LINES["why_template"])
        try:
            rec.click("[data-act=why]", 1.0)
            rec.pause(max(h - 1, 4))
            rec.click("#drawer-close", 0.6)
        except Exception:  # noqa: BLE001
            rec.pause(h)
        # shot 7: hand off to Google Maps (hover; opening it would show your own account)
        print('  ' + 'shot 7: hand off to Google Maps (hover; ', flush=True)
        h = rec.say(LINES["gmaps"])
        gb = page.locator(".gobar a").bounding_box()
        if gb:
            rec.move(gb["x"] + gb["width"] / 2, gb["y"] + gb["height"] / 2)
        rec.pause(max(2.5, h - 2.5))
        rec.click("#s-back", 0.6)
        # shot 8: a long trip
        print('  ' + 'shot 8: a long trip', flush=True)
        if A.far:
            h = rec.say(LINES["far"])
            rec.type("#q", A.far)
            page.locator(".sugg .hd:has-text('Anywhere in India') ~ .sg, .sg").first.wait_for(timeout=20000)
            rec.pause(1.6)
            far_item = page.locator(".sg", has_text=A.far).first
            if far_item.count():
                b = far_item.bounding_box(); rec.move(b["x"] + 40, b["y"] + b["height"] / 2); page.mouse.down(); page.mouse.up()
            else:
                rec.click(".sg", 0.4)
            try:
                page.locator(".opt").first.wait_for(timeout=60000)
            except Exception:  # noqa: BLE001
                pass
            rec.pause(max(h - 2, 4))
            rec.click("#s-back", 0.6)
        # shot 9: Cedar: Ravi, Control room, restricted layer
        print('  ' + 'shot 9: Cedar: Ravi, Control room, restr', flush=True)
        h = rec.say(LINES["cedar"]); t_cedar = rec.now()
        rec.click("#avatar", 0.4); rec.click("[data-user=ravi]", 2.2)
        rec.click("#avatar", 0.4); rec.click("[data-user=control]", 2.6)
        rec.click("#avatar", 0.4); rec.click("[data-user=priya]", 0.8)
        rec.click("[data-layer=sites]", 2.2)
        try:
            rec.click("#notice [data-n=ok]", 0.4, timeout=2500)
        except Exception as e:  # noqa: BLE001
            print("      (no restricted-layer notice)", type(e).__name__)
        rec.pause(max(0.3, h - (rec.now() - t_cedar)))
        # systems beat: How it works -> System
        print('  ' + 'systems beat: How it works -> System', flush=True)
        h = rec.say(LINES["system"])
        rec.click("#avatar", 0.4); rec.click("[data-m=how]", 0.6)
        rec.click("#tabs [data-tab=system]", 1.2)
        rec.move(1200, 500)
        for _ in range(6):
            page.mouse.wheel(0, 320); rec.pause(0.55)
        rec.pause(max(0.8, h - 4.5))
        page.keyboard.press("Escape")
        rec.clear()
        timelines.append(rec.timeline)
        page.close(); ctx.close()
        vids.append(sorted((tmp / "desk").glob("*.webm"))[0])

        # -------- phone, 390x844 (shown at 2x so it's crisp)
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                                  color_scheme=A.theme, record_video_dir=str(tmp / "phone"), record_video_size={"width": 780, "height": 1688},
                                  geolocation={"latitude": A.lat, "longitude": A.lon}, permissions=["geolocation"])
        page = ctx.new_page()
        t1 = time.monotonic()
        rec = Recorder(page, t1)
        page.goto(A.app + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        h = rec.say(LINES["phone"])
        rec.pause(h * 0.5)
        try:
            rec.click(".peek .commute", 1.0)
        except Exception:  # noqa: BLE001
            pass
        rec.pause(max(3.0, h * 0.6))
        rec.clear()
        cards(page, rec, LINES["end"], cls="big")
        timelines.append(rec.timeline)
        page.close(); ctx.close()
        vids.append(sorted((tmp / "phone").glob("*.webm"))[0])
        browser.close()

    # ------------------------------------------------------------------ assemble
    ff = ffmpeg()
    parts = []
    for i, v in enumerate(vids):
        out = tmp / f"part{i}.mp4"
        # phone segment: centred on a 1440x900 canvas, like a phone on a desk
        vf = ("scale=1440:900:force_original_aspect_ratio=decrease,pad=1440:900:(ow-iw)/2:(oh-ih)/2:color=0x050505" if i == 1
              else "scale=1440:900")
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(v), "-vf", vf + ",fps=30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-preset", "medium", "-crf", "20", "-an", str(out)], check=True)
        parts.append(out)
    lst = tmp / "parts.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    silent = tmp / "silent.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)], check=True)
    if not A.voice:
        shutil.copy(silent, A.out)
        print(f"\nWrote {A.out} (no voice). Record your voiceover over it, or re-run with --voice.")
        return
    # voice: one clip per caption, placed at the moment it appeared
    import pyttsx3
    eng = pyttsx3.init()
    eng.setProperty("rate", 172)
    offset = 0.0
    clips = []
    for seg, tl in enumerate(timelines):
        for j, (t, text) in enumerate(tl):
            f = tmp / f"v{seg}_{j}.wav"
            eng.save_to_file(text, str(f))
            clips.append((offset + t, f))
    eng.runAndWait()
    # duration of the desktop part, to offset the phone captions
    probe = subprocess.run([ff, "-i", str(parts[0])], capture_output=True, text=True).stderr
    hh, mm, ss = probe.split("Duration: ")[1].split(",")[0].split(":")
    d0 = int(hh) * 3600 + int(mm) * 60 + float(ss)
    clips = [(t + (d0 if f.name.startswith("v1_") else 0), f) for t, f in clips]
    inputs, filt = [], []
    for k, (t, f) in enumerate(clips):
        inputs += ["-i", str(f)]
        filt.append(f"[{k + 1}:a]adelay={int(t * 1000)}|{int(t * 1000)}[a{k}]")
    filt.append("".join(f"[a{k}]" for k in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0[aout]")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(silent), *inputs, "-filter_complex", ";".join(filt),
                    "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", A.out], check=True)
    print(f"\nWrote {A.out} with voice.")


if __name__ == "__main__":
    main()
