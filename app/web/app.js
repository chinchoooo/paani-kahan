/* Paani Kahan v3: map app client. No framework, no build step.

   Data model
   - The map is made of server tiles (Web Mercator): street-level "detail" tiles (z13,
     ~5 km) near you and main-road "region" tiles (z10, ~38 km) for 100 km around.
   - Each tile has immutable geometry (/api/tile/z/x/y) and a forecast that changes with
     each rain run (/api/tile/z/x/y/forecast). Both are fetched by priority (centre of the
     screen first), kept in memory (LRU), and cached by the service worker for offline.
   - Street segments have global ids "<z>/<x>/<y>:<i>". Region tiles are drawn wherever
     a detail tile isn't loaded yet, so the map is never blank while detail streams in.
   - Server-sent events say which tiles became ready or got a new forecast. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const root = document.documentElement;
const FONT = "'Inter Tight', 'Inter', system-ui, sans-serif";

/* ------------------------------------------------------------- network */
async function api(path, body, opts = {}) {
  try {
    const r = await fetch(path, body ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal: opts.signal } : { signal: opts.signal });
    const cached = r.headers.get("x-pk-cache");
    if (cached) markOffline(cached); else if (r.ok && !body) markOnline();
    let j = null;
    try { j = await r.json(); } catch { /* empty body */ }
    return { ok: r.ok, status: r.status, j, cached: !!cached };
  } catch (e) {
    if (e?.name === "AbortError") return { ok: false, status: -1, j: null };
    markOffline();
    return { ok: false, status: 0, j: null };
  }
}
function markOffline(when) {
  S.offline = true;
  const c = $("#offline");
  let t = "Offline";
  if (when && when !== "1") { const d = new Date(when); if (!isNaN(d)) t += ` · saved ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`; }
  c.textContent = t; c.hidden = false;
}
function markOnline() { if (S.offline) { S.offline = false; $("#offline").hidden = true; } }

/* --------------------------------------------------------------- prefs */
const PREF_KEY = "pk-prefs-v4";
const LAYERS0 = { flood: true, rain: true, terrain: false, sites: false, closures: true };
const prefs = Object.assign({ mode: null, theme: "auto", recent: [], notify: false, last: null }, (() => {
  try { return JSON.parse(localStorage.getItem(PREF_KEY) || "{}"); } catch { return {}; }
})());
try { if (!prefs.last) { const old = JSON.parse(localStorage.getItem("pk-prefs-v3") || "{}"); prefs.last = old.last; prefs.theme = old.theme || prefs.theme; prefs.mode = old.mode || prefs.mode; } } catch { /* blocked */ }
prefs.layers = Object.assign({}, LAYERS0, prefs.layers || {});
function savePrefs() {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch { /* storage blocked */ }
  navigator.serviceWorker?.controller?.postMessage({ type: "prefs", prefs: { mode: S.mode, depart_min: S.persona?.depart_min ?? 0 } });
}

/* --------------------------------------------------------------- state */
const MODES = [["two_wheeler", "2-wheeler"], ["car", "Car"], ["walk", "Walk"]];
const LEAVE = [0, 20, 50, 80, 110, 140, 170, 200, 230];
const S = {
  cfg: null, sum: null, user: "priya", mode: prefs.mode || "two_wheeler", depart: 110, t: 0, playing: false,
  persona: null, commute: null, commuteWait: null, plan: null, planWait: null, advice: null, adviceLoading: false,
  view: "home", place: null, trip: null, sel: "best", searchFor: "dest",
  me: null, nearby: null, ops: null, vuln: null, status: null,
  drawer: false, tab: "why", hover: null, fresh: false, offline: false,
  personaLoaded: false, draft: null, reportAt: null, reports: [], closures: [], closedSegs: new Set(), demo: { state: "off" },
};

/* --------------------------------------------------------------- theme */
let T = {};
function hexRGB(h) {
  h = (h || "#000").replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function applyTheme() {
  if (prefs.theme === "auto") root.removeAttribute("data-theme"); else root.dataset.theme = prefs.theme;
  const cs = getComputedStyle(root), g = (n) => cs.getPropertyValue(n).trim();
  const bg = g("--map-bg"), [r, gg, b] = hexRGB(bg);
  T = {
    bg, s: [g("--map-street-0"), g("--map-street-1"), g("--map-street-2")], casing: g("--map-casing"),
    water: g("--map-water"), waterEdge: g("--map-water-edge"), label: g("--map-label"), halo: g("--map-label-halo"),
    locality: g("--map-locality"), alt: g("--map-alt"), routeCasing: g("--map-route-casing"), rain: g("--map-rain"),
    me: g("--map-me"), accent: g("--accent"), text: g("--text"), terrain: g("--map-terrain") || "multiply",
    w: [hexRGB(g("--w0")), hexRGB(g("--w1")), hexRGB(g("--w2"))],
    dark: (0.299 * r + 0.587 * gg + 0.114 * b) < 128,
  };
  $$('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", g("--bg")));
  for (const tl of TILES.values()) { tl.rasKey = null; tl.wetKey = null; }
  routeCache.key = null;
  if (S.sum) renderTimeline();
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { applyTheme(); render(); });
function waterRGB(d) {
  const r = d / limit(), [c0, c1, c2] = T.w;
  if (r < 0.5) return c0;
  if (r < 1) { const u = (r - 0.5) / 0.5; return c0.map((v, i) => Math.round(lerp(v, c1[i], u))); }
  return c2;  // over the limit: signal red, no in-between
}
const rgb = (a) => `rgb(${a[0]},${a[1]},${a[2]})`;
const statusOf = (d) => d >= limit() ? "alert" : d >= limit() * 0.6 ? "watch" : "clear";

/* ---------------------------------------------------------------- time */
function clockAt(min) {
  const [h, m] = S.sum?.now || S.cfg?.run?.now || [0, 0];
  const tot = h * 60 + m + Math.round(min);
  const hh = Math.floor(tot / 60) % 24, mm = ((tot % 60) + 60) % 60;
  return `${((hh + 11) % 12) + 1}:${String(mm).padStart(2, "0")} ${hh < 12 ? "AM" : "PM"}`;
}
const limit = () => (S.cfg?.limits || { two_wheeler: 30, car: 45, walk: 25 })[S.mode];
const modeLabel = (m) => MODES.find((x) => x[0] === m)?.[1] || m;

/* ------------------------------------------------------------ geometry */
// One fixed equirectangular projection around the session origin (metres).
// Web Mercator scaled to metres at the session origin: the same projection as the basemap tiles,
// so streets, water and the basemap line up exactly at every zoom.
const RAD = Math.PI / 180, R_E = 6378137;
const G = { lat0: 13, lon0: 80.2, s: R_E * Math.cos(13 * RAD), y0: 0 };
const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + lat * RAD / 2));
const proj = (lat, lon) => [(lon - G.lon0) * RAD * G.s, -(mercY(lat) - G.y0) * G.s];
const unproj = (x, y) => [(2 * Math.atan(Math.exp(G.y0 - y / G.s)) - Math.PI / 2) / RAD, G.lon0 + x / G.s / RAD];
function setOrigin(lat, lon) {
  G.lat0 = lat; G.lon0 = lon; G.s = R_E * Math.cos(lat * RAD); G.y0 = mercY(lat);
  TILES.clear(); SEGS.clear(); routeCache.key = null;
}
function haversine(a, b, c, d) {
  const R = 6371000, p1 = a * Math.PI / 180, p2 = c * Math.PI / 180, dp = p2 - p1, dl = (d - b) * Math.PI / 180;
  return 2 * R * Math.asin(Math.sqrt(Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2));
}
// slippy tiles
function tileXY(lat, lon, z) {
  const n = 2 ** z, x = Math.floor((lon + 180) / 360 * n);
  const y = Math.floor((1 - Math.asinh(Math.tan(lat * Math.PI / 180)) / Math.PI) / 2 * n);
  return [clamp(x, 0, n - 1), clamp(y, 0, n - 1)];
}
function tileBBox(z, x, y) {
  const n = 2 ** z, lat = (yy) => Math.atan(Math.sinh(Math.PI * (1 - 2 * yy / n))) * 180 / Math.PI;
  return [lat(y + 1), x / n * 360 - 180, lat(y), (x + 1) / n * 360 - 180];
}

/* ------------------------------------------------------------- basemap */
// A raster basemap (CARTO Positron / Dark Matter, from OpenStreetMap) that loads straight from a CDN,
// independent of our server. Whatever state the flood data is in, there is always a real map.
const BASE = {
  cfg: { light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", dark: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    subdomains: "abcd", attribution: "© OpenStreetMap contributors © CARTO", maxZoom: 19 },
  imgs: new Map(), loaded: 0, failed: 0, inflight: 0,
};
const baseOK = () => !!BASE.cfg && (BASE.loaded > 0 || BASE.failed < 6);
function baseUrl(z, x, y) {
  const c = BASE.cfg, sub = c.subdomains ? c.subdomains[(x + y) % c.subdomains.length] : "";
  return (T.dark ? c.dark : c.light).replace("{s}", sub).replace("{z}", z).replace("{x}", x).replace("{y}", y).replace("{r}", DPR > 1 ? "@2x" : "");
}
function baseImg(z, x, y, load) {
  const url = baseUrl(z, x, y);
  let e = BASE.imgs.get(url);
  if (!e && load && BASE.inflight < 16) {
    const img = new Image();
    e = { img, ok: false, used: 0 };
    BASE.inflight++;
    img.onload = () => { e.ok = true; BASE.loaded++; BASE.inflight--; };
    img.onerror = () => { e.bad = true; BASE.failed++; BASE.inflight--; setTimeout(() => BASE.imgs.delete(url), 15000); };
    img.src = url;
    BASE.imgs.set(url, e);
    if (BASE.imgs.size > 400) {  // LRU
      const old = [...BASE.imgs.entries()].sort((a, b) => a[1].used - b[1].used).slice(0, 100);
      for (const [u] of old) BASE.imgs.delete(u);
    }
  }
  if (e) e.used = performance.now();
  return e && e.ok ? e.img : null;
}
function drawBasemap() {
  if (!BASE.cfg) return;
  const mpp = 1 / V.k, n0 = 2 * Math.PI * G.s / 256;          // metres per tile pixel at z0 (in our units)
  const z = clamp(Math.round(Math.log2(n0 / mpp)), 2, BASE.cfg.maxZoom || 19), n = 2 ** z;
  const [ax, ay] = toWorld(0, 0), [bx, by] = toWorld(W, H);
  const [lat1, lon0] = unproj(ax, ay), [lat0, lon1] = unproj(bx, by);
  const [x0, y0] = tileXY(Math.min(lat1, 85), lon0, z), [x1, y1] = tileXY(Math.max(lat0, -85), lon1, z);
  if ((x1 - x0 + 1) * (y1 - y0 + 1) > 120) return;
  ctx.imageSmoothingEnabled = true;
  for (let ty = y0; ty <= y1; ty++) for (let tx = x0; tx <= x1; tx++) {
    const bb = tileBBox(z, tx, ty), [px0, py0] = proj(bb[2], bb[1]), [px1, py1] = proj(bb[0], bb[3]);
    let img = baseImg(z, tx, ty, true);
    if (img) { ctx.drawImage(img, px0, py0, px1 - px0, py1 - py0); continue; }
    // not loaded yet: stretch the nearest loaded ancestor so there's never a hole
    for (let up = 1; up <= 6 && z - up >= 0; up++) {
      const az = z - up, ax_ = tx >> up, ay_ = ty >> up, a = baseImg(az, ax_, ay_, up === 1);
      if (!a) continue;
      const span = 2 ** up, sw = a.naturalWidth / span, sx = (tx - (ax_ << up)) * sw, sy = (ty - (ay_ << up)) * sw;
      ctx.drawImage(a, sx, sy, sw, sw, px0, py0, px1 - px0, py1 - py0);
      break;
    }
  }
}

/* --------------------------------------------------------------- tiles */
const TILES = new Map();   // "z/x/y" -> tile
const SEGS = new Map();    // gid -> segment
const DZ = 13, RZ = 10;
function tileObj(key) {
  let tl = TILES.get(key);
  if (!tl) {
    const [z, x, y] = key.split("/").map(Number);
    const bb = tileBBox(z, x, y), [x0, y0] = proj(bb[2], bb[1]), [x1, y1] = proj(bb[0], bb[3]);
    tl = { key, z, x, y, lod: z === DZ ? "detail" : "region", bbox: bb, wb: { x0, y0, x1, y1 }, status: "new", geo: null, fc: null,
      tries: 0, next: 0, fcNext: 0, fcWant: null, born: 0, used: 0, rasKey: null, wetKey: null, terrain: null };
    TILES.set(key, tl);
  }
  return tl;
}
function prepareTile(tl, g) {
  tl.geo = g;
  if (g.empty) { tl.status = "empty"; return; }
  const segs = [], byName = new Map(), base = [new Path2D(), new Path2D(), new Path2D()], hash = new Map(), CELL = 80;
  const [olat, olon] = g.origin;
  g.segments.forEach(([cls, nameI, locI, flat, flags], i) => {
    const pts = new Float32Array(flat.length);
    let la = Math.round(olat * 1e6), lo = Math.round(olon * 1e6);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let k = 0; k < flat.length; k += 2) {
      la += flat[k]; lo += flat[k + 1];
      const [x, y] = proj(la / 1e6, lo / 1e6); pts[k] = x; pts[k + 1] = y;
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    const p = base[cls];
    p.moveTo(pts[0], pts[1]);
    let len = 0;
    for (let k = 2; k < pts.length; k += 2) { p.lineTo(pts[k], pts[k + 1]); len += Math.hypot(pts[k] - pts[k - 2], pts[k + 1] - pts[k - 1]); }
    for (let cx = Math.floor(x0 / CELL); cx <= Math.floor(x1 / CELL); cx++) for (let cy = Math.floor(y0 / CELL); cy <= Math.floor(y1 / CELL); cy++) {
      const key = cx + "," + cy; let arr = hash.get(key); if (!arr) hash.set(key, (arr = [])); arr.push(i);
    }
    const mid = Math.floor(pts.length / 4) * 2, name = g.names[nameI], gid = `${tl.key}:${i}`;
    const sg = { gid, i, tile: tl, cls, name, loc: g.localities[locI]?.name || "", pts, mx: pts[mid], my: pts[mid + 1], len,
      bridge: !!(flags & 1), underpass: !!(flags & 2), construction: !!(flags & 4) };
    if (!name.startsWith("Unnamed") && len > 70) { if (!byName.has(name)) byName.set(name, []); byName.get(name).push(sg); }
    segs.push(sg); SEGS.set(gid, sg);
  });
  const labels = [];
  for (const [name, list] of byName) {
    list.sort((p, q) => q.len - p.len);
    const kept = [];
    for (const sg of list) {
      if (kept.length >= 3) break;
      if (kept.some((k) => Math.hypot(k.mx - sg.mx, k.my - sg.my) < 600)) continue;
      kept.push(sg);
    }
    for (const sg of kept) labels.push({ name, cls: sg.cls, sg });
  }
  labels.sort((p, q) => p.cls - q.cls || q.sg.len - p.sg.len);
  const water = new Path2D();
  for (const poly of g.water) {
    poly.forEach(([la, lo], k) => { const [x, y] = proj(la, lo); k ? water.lineTo(x, y) : water.moveTo(x, y); });
    water.closePath();
  }
  Object.assign(tl, { segs, base, hash, CELL, labels, water, status: "ready", born: performance.now(),
    locs: (g.own_localities || []).map((i) => ({ name: g.localities[i].name, xy: proj(g.localities[i].lat, g.localities[i].lon) })),
    pois: g.pois || [] });
  if (prefs.layers.terrain) loadTerrain(tl);
}
function dropTile(tl) {
  if (tl.segs) for (const sg of tl.segs) SEGS.delete(sg.gid);
  TILES.delete(tl.key);
}
function loadTerrain(tl) {
  if (tl.terrain || tl.status !== "ready") return;
  const img = new Image();
  img.onload = () => { tl.terrain = img; };
  img.onerror = () => { tl.terrain = null; tl.terrainFail = performance.now(); };
  tl.terrain = "loading";
  img.src = `/api/tile/${tl.key}/terrain.png?b=${tl.geo?.built || 0}`;
}
function b64(s) { const bin = atob(s); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; }
function setTileForecast(tl, j) {
  const fc = { ...j, seg: b64(j.seg_depth), ras: b64(j.raster), rain: b64(j.rain), n: j.frames };
  delete fc.seg_depth; delete fc.raster;
  const [s, w, n, e] = j.raster_bbox, [x0, y0] = proj(n, w), [x1, y1] = proj(s, e);
  fc.rb = { x0, y0, x1, y1 };
  const first = !tl.fc;
  tl.fc = fc; tl.rasKey = null; tl.wetKey = null; tl.fcBorn = first ? performance.now() : tl.fcBorn;
}

/* fetch scheduling */
const NET = { geo: 0, fc: 0, maxGeo: 4, maxFc: 4 };
let tilesDirty = true;
function viewBBox(pad = 0) {
  const p = pads(), [ax, ay] = toWorld(p.l - pad, p.t - pad), [bx, by] = toWorld(W - p.r + pad, H - p.b + pad);
  const [n, w] = unproj(ax, ay), [s, e] = unproj(bx, by);
  return [Math.min(s, n), Math.min(w, e), Math.max(s, n), Math.max(w, e)];
}
const mpp = () => 1 / V.k;  // metres per CSS pixel
const showDetail = () => mpp() < 28;
function wantedTiles() {
  const out = [];
  const bb = viewBBox(80), [cx, cy] = [V.cx, V.cy];
  const add = (z) => {
    const [x0, y0] = tileXY(bb[2], bb[1], z), [x1, y1] = tileXY(bb[0], bb[3], z);
    if ((x1 - x0 + 1) * (y1 - y0 + 1) > 64) return;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const tl = tileObj(`${z}/${x}/${y}`), wb = tl.wb;
      tl.dist = Math.hypot((wb.x0 + wb.x1) / 2 - cx, (wb.y0 + wb.y1) / 2 - cy) + (z === RZ ? 2000 : 0);
      out.push(tl);
    }
  };
  if (showDetail()) add(DZ);
  add(RZ);
  return out.sort((a, b) => a.dist - b.dist);
}
async function fetchGeo(tl) {
  NET.geo++; tl.status = "loading";
  const r = await api(`/api/tile/${tl.key}`);
  NET.geo--;
  if (r.status === 200 && r.j) { prepareTile(tl, r.j); tl.tries = 0; tilesDirty = true; maybeRefreshViews(); }
  else { tl.status = "pending"; tl.tries++; tl.next = performance.now() + Math.min(15000, 1500 * tl.tries); tl.serverStatus = r.j?.status; }
}
async function fetchFc(tl) {
  NET.fc++; tl.fcLoading = true;
  const r = await api(`/api/tile/${tl.key}/forecast`);
  NET.fc--; tl.fcLoading = false; tl.fcWant = null;
  if (r.status === 200 && r.j) { setTileForecast(tl, r.j); tl.fcNext = 0; maybeRefreshViews(); }
  else tl.fcNext = performance.now() + 2500;
}
function pumpTiles() {
  const now = performance.now(), want = wantedTiles(), keep = new Set();
  for (const tl of want) {
    keep.add(tl.key); tl.used = now;
    if ((tl.status === "new" || tl.status === "pending") && now >= tl.next && NET.geo < NET.maxGeo) fetchGeo(tl);
    if (tl.status === "ready" && !tl.fcLoading && NET.fc < NET.maxFc && now >= tl.fcNext && (!tl.fc || tl.fcWant)) fetchFc(tl);
    if (tl.status === "ready" && prefs.layers.terrain && !tl.terrain && (!tl.terrainFail || now - tl.terrainFail > 20000)) loadTerrain(tl);
  }
  // keep memory bounded: drop far tiles that haven't been on screen for a while
  if (TILES.size > 120) {
    const old = [...TILES.values()].filter((t) => !keep.has(t.key)).sort((a, b) => a.used - b.used);
    for (const t of old.slice(0, TILES.size - 100)) dropTile(t);
  }
  updateLoadbar(want);
}
setInterval(pumpTiles, 350);
function updateLoadbar(want) {
  const vis = want.filter((t) => t.lod === (showDetail() ? "detail" : "region") && t.status !== "empty");
  const done = vis.filter((t) => t.status === "ready" && t.fc).length;
  const bar = $("#loadbar"), frac = vis.length ? done / vis.length : 1;
  bar.classList.toggle("idle", frac >= 1);
  $("i", bar).style.width = `${Math.round(frac * 100)}%`;
}
let focusTimer = null, lastFocus = null;
function sendFocus(force) {
  clearTimeout(focusTimer);
  focusTimer = setTimeout(async () => {
    const [lat, lon] = S.me ? [S.me.lat, S.me.lon] : unproj(V.cx, V.cy);
    const key = `${lat.toFixed(3)},${lon.toFixed(3)},${V.k.toFixed(3)}`;
    if (!force && key === lastFocus) return;
    lastFocus = key;
    const r = await api("/api/focus", { lat, lon, view: viewBBox() });
    if (r.ok) S.progress = r.j.progress;
    // the 6-hour rain picture is for a place: refresh it when the map moves somewhere else
    if (!S.sumAt || haversine(lat, lon, S.sumAt[0], S.sumAt[1]) > 3000) loadSummary();
    prefs.last = [lat, lon]; savePrefs();
  }, force ? 0 : 700);
}

/* ------------------------------------------------------------ forecast */
function segFc(sg) { return sg?.tile?.fc; }
function depthAt(gid, t) {
  const sg = typeof gid === "string" ? SEGS.get(gid) : gid, fc = segFc(sg);
  if (!fc || !fc.nseg) return 0;
  const f = clamp(t / 10, 0, fc.n - 1), a = Math.floor(f), b = Math.min(a + 1, fc.n - 1), N = fc.nseg;
  return lerp(fc.seg[a * N + sg.i], fc.seg[b * N + sg.i], f - a);
}
function tileAt(x, y, lod) {
  for (const tl of TILES.values()) if ((!lod || tl.lod === lod) && tl.fc && x >= tl.wb.x0 && x <= tl.wb.x1 && y >= tl.wb.y0 && y <= tl.wb.y1) return tl;
  return null;
}
function rainAt(t, x, y) {
  const tl = tileAt(x, y, "detail") || tileAt(x, y, "region");
  if (!tl) { const f = clamp(Math.round(t / 10), 0, (S.sum?.rain_mean.length || 1) - 1); return S.sum?.rain_mean[f] || 0; }
  const fc = tl.fc, [rr, cc] = fc.rain_shape, { x0, y0, x1, y1 } = fc.rb;
  const c = clamp(Math.floor((x - x0) / (x1 - x0) * cc), 0, cc - 1), r = clamp(Math.floor((y - y0) / (y1 - y0) * rr), 0, rr - 1);
  const f = clamp(t / 10, 0, fc.n - 1), a = Math.floor(f), b = Math.min(a + 1, fc.n - 1), sz = rr * cc;
  return lerp(fc.rain[a * sz + r * cc + c], fc.rain[b * sz + r * cc + c], f - a);
}
async function loadSummary() {
  const [lat, lon] = S.me ? [S.me.lat, S.me.lon] : unproj(V.cx, V.cy);
  const r = await api(`/api/summary?lat=${lat}&lon=${lon}`);
  if (r.ok) {
    S.sumAt = [lat, lon];
    const changed = S.sum?.run_id !== r.j.run_id;
    S.sum = r.j; S.progress = r.j.progress; S.demo = r.j.demo || S.demo;
    $("#demo-badge").hidden = r.j.mode !== "demo";
    if (changed) for (const tl of TILES.values()) if (tl.fc && tl.fc.run_id !== r.j.run_id) tl.fcWant = true;
    renderTimeline();
  }
}

/* -------------------------------------------------------------- canvas */
const cv = $("#map"), ctx = cv.getContext("2d");
let W = 0, H = 0, DPR = 1;
const V = { cx: 0, cy: 0, k: 0.3 };
const K_MIN = 1 / 300, K_MAX = 6;
const anim = { from: null, to: null, t0: 0, dur: 0 };
const isDesk = () => innerWidth >= 900;
function resize() {
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  W = innerWidth; H = innerHeight;
  cv.width = W * DPR; cv.height = H * DPR;
  applySheet();
  if (S.sum) renderTimeline();
}
addEventListener("resize", resize);
function pads() {
  if (isDesk()) return { l: 424, r: 80, t: 70, b: 96 };
  const sv = parseFloat(getComputedStyle(root).getPropertyValue("--sheet-visible")) || 0;
  return { l: 16, r: 64, t: 120, b: Math.min(sv, H * 0.5) + 84 };
}
function centreOn(x, y, k, dur = 900) {
  const p = pads();
  k = clamp(k || V.k, K_MIN, K_MAX);
  const to = { cx: x - ((p.l - p.r) / 2) / k, cy: y - ((p.t - p.b) / 2) / k, k };
  if (!dur) { Object.assign(V, to); return; }
  Object.assign(anim, { from: { ...V }, to, t0: performance.now(), dur });
}
const flyTo = (x, y, k) => centreOn(x, y, k || Math.max(V.k, 0.6));
function fitPoints(pts) {
  if (!pts.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  const p = pads(), k = clamp(Math.min((W - p.l - p.r) / Math.max(x1 - x0, 300), (H - p.t - p.b) / Math.max(y1 - y0, 300)) * 0.9, K_MIN, 2);
  centreOn((x0 + x1) / 2, (y0 + y1) / 2, k, 800);
}
const toScreen = (x, y) => [(x - V.cx) * V.k + W / 2, (y - V.cy) * V.k + H / 2];
const toWorld = (sx, sy) => [(sx - W / 2) / V.k + V.cx, (sy - H / 2) / V.k + V.cy];

function buildTileRaster(tl) {
  const fc = tl.fc, key = `${fc.run_id}|${fc.observations}|${S.mode}|${Math.round(S.t)}|${T.dark}`;
  if (key === tl.rasKey) return;
  tl.rasKey = key;
  const [rr, cc] = fc.raster_shape, sz = rr * cc;
  const f = clamp(S.t / 10, 0, fc.n - 1), a = Math.floor(f), b = Math.min(a + 1, fc.n - 1), u = f - a;
  tl.rasCv = tl.rasCv || document.createElement("canvas");
  tl.rasCv.width = cc; tl.rasCv.height = rr;
  const rc = tl.rasCv.getContext("2d"), img = rc.createImageData(cc, rr), amax = T.dark ? 190 : 150, L = limit();
  for (let i = 0; i < sz; i++) {
    const d = lerp(fc.ras[a * sz + i], fc.ras[b * sz + i], u);
    if (d < 6) continue;
    const c = waterRGB(d);
    img.data[i * 4] = c[0]; img.data[i * 4 + 1] = c[1]; img.data[i * 4 + 2] = c[2];
    img.data[i * 4 + 3] = Math.round(Math.pow(clamp((d - 6) / (L * 1.3), 0, 1), 0.75) * amax);
  }
  rc.putImageData(img, 0, 0);
  const [r2, c2] = fc.rain_shape, s2 = r2 * c2, [rr0, rg0, rb0] = T.rain.split(",").map(Number);
  tl.rainCv = tl.rainCv || document.createElement("canvas");
  tl.rainCv.width = c2; tl.rainCv.height = r2;
  const rc2 = tl.rainCv.getContext("2d"), im2 = rc2.createImageData(c2, r2);
  for (let i = 0; i < s2; i++) {
    const v = lerp(fc.rain[a * s2 + i], fc.rain[b * s2 + i], u);
    im2.data[i * 4] = rr0; im2.data[i * 4 + 1] = rg0; im2.data[i * 4 + 2] = rb0;
    im2.data[i * 4 + 3] = Math.round(clamp((v - 8) / 80, 0, 1) * (T.dark ? 40 : 46));
  }
  rc2.putImageData(im2, 0, 0);
}
function buildWet(tl) {
  const fc = tl.fc, key = `${fc.run_id}|${fc.observations}|${S.mode}|${Math.round(S.t / 2)}`;
  if (key === tl.wetKey) return tl.wet;
  tl.wetKey = key;
  const buckets = new Map(), L = limit();
  for (const sg of tl.segs) {
    const d = depthAt(sg, S.t);
    if (d < 6) continue;
    const lvl = Math.min(11, Math.floor(d / L * 6));
    let b = buckets.get(lvl); if (!b) buckets.set(lvl, (b = { p: new Path2D(), d: 0, w: 0 }));
    segPath(sg, b.p);
    b.d = Math.max(b.d, (lvl + 0.5) / 6 * L); b.w = Math.max(b.w, BW[sg.cls]);
  }
  tl.wet = [...buckets].sort((p, q) => p[0] - q[0]);
  return tl.wet;
}

const drops = Array.from({ length: 320 }, () => ({ x: Math.random(), y: Math.random(), s: 0.6 + Math.random() * 0.8, l: 6 + Math.random() * 10 }));
let lastTs = 0, dash = 0;
function frame(ts) {
  const dt = Math.min(50, ts - (lastTs || ts)); lastTs = ts;
  if (anim.to) {
    const u = clamp((ts - anim.t0) / anim.dur, 0, 1), e = 1 - Math.pow(1 - u, 3);
    V.cx = lerp(anim.from.cx, anim.to.cx, e); V.cy = lerp(anim.from.cy, anim.to.cy, e);
    V.k = Math.exp(lerp(Math.log(anim.from.k), Math.log(anim.to.k), e));
    if (u >= 1) { anim.to = null; sendFocus(); }
  }
  if (S.playing) {
    S.t += dt / 1000 * 26;
    if (S.t >= 360) { S.t = 360; setPlaying(false); }
    onTimeChange();
  }
  dash -= dt * 0.012;
  draw(ts, dt);
  requestAnimationFrame(frame);
}
function segPath(sg, p = new Path2D()) {
  p.moveTo(sg.pts[0], sg.pts[1]);
  for (let q = 2; q < sg.pts.length; q += 2) p.lineTo(sg.pts[q], sg.pts[q + 1]);
  return p;
}
const BW = [1.9, 1.25, 0.75];
function visibleTiles() {
  const p = pads(), [ax, ay] = toWorld(0, 0), [bx, by] = toWorld(W, H);
  const out = [];
  for (const tl of TILES.values()) {
    if (tl.status !== "ready") continue;
    const wb = tl.wb;
    if (wb.x1 < ax || wb.x0 > bx || wb.y1 < ay || wb.y0 > by) continue;
    out.push(tl);
  }
  return out;
}
function draw(ts, dt) {
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.fillStyle = T.bg; ctx.fillRect(0, 0, W, H);
  const k = V.k, zw = Math.sqrt(clamp(k / 0.25, 0.35, 8)), px = 1 / k;
  ctx.setTransform(DPR * k, 0, 0, DPR * k, DPR * (W / 2 - V.cx * k), DPR * (H / 2 - V.cy * k));
  drawBasemap();
  if (!S.cfg) { ctx.setTransform(DPR, 0, 0, DPR, 0, 0); drawMe(ts); return; }
  const vis = visibleTiles(), det = showDetail() ? vis.filter((t) => t.lod === "detail") : [], reg = vis.filter((t) => t.lod === "region");
  ctx.setTransform(DPR * k, 0, 0, DPR * k, DPR * (W / 2 - V.cx * k), DPR * (H / 2 - V.cy * k));
  // region underlay, clipped to where street-level tiles aren't loaded yet
  if (reg.length) {
    ctx.save();
    if (det.length) {
      const clip = new Path2D(), [ax, ay] = toWorld(-50, -50), [bx, by] = toWorld(W + 50, H + 50);
      clip.rect(ax, ay, bx - ax, by - ay);
      for (const t of det) clip.rect(t.wb.x0, t.wb.y0, t.wb.x1 - t.wb.x0, t.wb.y1 - t.wb.y0);
      ctx.clip(clip, "evenodd");
    }
    drawLayer(reg, ts, px, zw, true);
    ctx.restore();
  }
  if (det.length) drawLayer(det, ts, px, zw, false);
  drawClosures(px, zw);
  const place = S.view === "place" && S.place?.gid ? SEGS.get(S.place.gid) : null;
  if (place) {
    const p = segPath(place);
    ctx.strokeStyle = T.text; ctx.globalAlpha = 0.25; ctx.lineWidth = (BW[place.cls] + 5) * zw * px; ctx.stroke(p);
    ctx.globalAlpha = 1; ctx.lineWidth = (BW[place.cls] + 1.6) * zw * px; ctx.stroke(p);
  }
  if (S.hover) {
    const sg = SEGS.get(S.hover);
    if (sg) { ctx.strokeStyle = T.text; ctx.globalAlpha = 0.6; ctx.lineWidth = (BW[sg.cls] + 1.4) * zw * px; ctx.stroke(segPath(sg)); ctx.globalAlpha = 1; }
  }
  if (S.view === "dir" && S.plan) drawRoutes(px, zw);

  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  if (prefs.layers.rain) drawRain(dt);
  if (!baseOK()) {  // the basemap carries labels; ours are the offline fallback
    drawStreetLabels(det.length ? det : reg);
    drawLocalityLabels(det.length && mpp() < 14 ? det : reg);
  }
  if (S.view === "dir" && S.plan) drawRouteMarkers(ts);
  if (S.view === "place" && S.place) dropPin(...proj(S.place.lat, S.place.lon));
  if (prefs.layers.sites && S.vuln) for (const p of S.vuln.sites || []) ring(...proj(p.lat, p.lon), p.name, ts);
  drawClosureMarks();
  drawReports();
  drawMe(ts);
}
function drawLayer(tiles, ts, px, zw, region) {
  const now = performance.now();
  // terrain + water bodies
  for (const tl of tiles) {
    const a = clamp((now - tl.born) / 500, 0, 1);
    ctx.globalAlpha = a;
    if (prefs.layers.terrain && tl.lod === "detail" && tl.terrain instanceof Image) {
      // light: multiply (white = no change). dark: inverted and screened, so low ground glows faintly
      ctx.globalCompositeOperation = T.dark ? "screen" : "multiply"; ctx.globalAlpha = a * (T.dark ? 0.16 : 0.6);
      if (T.dark) ctx.filter = "invert(1)";
      const r = tl.fc?.rb || tl.wb;
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(tl.terrain, r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
      ctx.filter = "none"; ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = a;
    }
    if (!baseOK()) { ctx.fillStyle = T.water; ctx.fill(tl.water); }
  }
  ctx.globalAlpha = 1;
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
  for (const tl of tiles) {
    if (!tl.fc) continue;
    buildTileRaster(tl);
    const r = tl.fc.rb, a = clamp((now - (tl.fcBorn || 0)) / 700, 0, 1), wb = tl.wb;
    ctx.save();
    ctx.beginPath(); ctx.rect(wb.x0, wb.y0, wb.x1 - wb.x0, wb.y1 - wb.y0); ctx.clip();  // each tile paints only its own square: no seams
    if (prefs.layers.rain) { ctx.globalAlpha = a; ctx.drawImage(tl.rainCv, r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0); }
    if (prefs.layers.flood && !region) {  // main-road tiles: the coarse 150 m raster is noise; show their wet roads only
      ctx.globalCompositeOperation = T.dark ? "lighter" : "source-over";
      ctx.globalAlpha = 0.95 * a;
      ctx.drawImage(tl.rasCv, r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
      ctx.globalCompositeOperation = "source-over";
    }
    ctx.restore();
  }
  // streets (only as a fallback: normally the basemap draws them, and we draw just the water on them)
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  const minor = region ? 1 : clamp((V.k - 0.04) / 0.12, 0, 1);  // fade in residential streets as you zoom in
  const wz = region ? Math.max(zw, 0.9) : zw;
  if (!baseOK()) {
  for (const tl of tiles) {
    const a = clamp((now - tl.born) / 500, 0, 1);
    for (let c = 2; c >= 0; c--) {
      const al = (c === 2 ? minor : 1) * a;
      if (al <= 0.02) continue;
      ctx.globalAlpha = al;
      if (T.casing !== "transparent" && V.k > 0.06) { ctx.strokeStyle = T.casing; ctx.lineWidth = (BW[c] * wz + 1.3) * px; ctx.stroke(tl.base[c]); }
    }
    for (let c = 2; c >= 0; c--) {
      const al = (c === 2 ? minor : 1) * a;
      if (al <= 0.02) continue;
      ctx.globalAlpha = al; ctx.strokeStyle = T.s[c]; ctx.lineWidth = BW[c] * wz * px; ctx.stroke(tl.base[c]);
    }
  }
  }
  ctx.globalAlpha = 1;
  if (!prefs.layers.flood) return;
  for (const tl of tiles) {
    if (!tl.fc) continue;
    const a = clamp((now - (tl.fcBorn || 0)) / 700, 0, 1);
    for (const [lvl, b] of buildWet(tl)) {
      const c = waterRGB(b.d), al = clamp(0.3 + lvl / 6, 0.3, 1) * a;
      if (lvl >= 3) { ctx.strokeStyle = `rgba(${c},${al * 0.22})`; ctx.lineWidth = (b.w + 6) * wz * px; ctx.stroke(b.p); }
      ctx.strokeStyle = `rgba(${c},${al})`; ctx.lineWidth = (b.w + 1.1) * wz * px; ctx.stroke(b.p);
    }
  }
}
function drawClosures(px, zw) {
  if (!prefs.layers.closures || V.k < 0.08) return;
  const closed = new Path2D(), cons = new Path2D(), subwayShut = new Path2D(), subway = new Path2D();
  let any = false;
  for (const gid of S.closedSegs) { const sg = SEGS.get(gid); if (sg) { segPath(sg, sg.construction ? cons : closed); any = true; } }
  for (const tl of visibleTiles()) {
    if (tl.lod !== "detail") continue;
    for (const sg of tl.segs) {
      if (!sg.underpass) continue;
      any = true;
      if (depthAt(sg, S.t) >= 30) segPath(sg, subwayShut); else segPath(sg, subway);
    }
  }
  if (!any) return;
  ctx.setLineDash([2 * px, 3 * px]); ctx.strokeStyle = T.alt; ctx.lineWidth = 3 * zw * px; ctx.stroke(cons);
  ctx.setLineDash([3 * px, 2.5 * px]); ctx.strokeStyle = rgb(T.w[2]); ctx.lineWidth = 3.4 * zw * px; ctx.stroke(closed); ctx.stroke(subwayShut);
  ctx.setLineDash([]); ctx.strokeStyle = T.dark ? "rgba(255,255,255,.5)" : "rgba(10,10,10,.5)"; ctx.lineWidth = (BW[1] + 2.4) * zw * px;
  ctx.globalAlpha = 0.5; ctx.stroke(subway); ctx.globalAlpha = 1;
}
function drawClosureMarks() {
  if (!prefs.layers.closures || V.k < 0.4) return;
  const mark = (sg, col) => {
    const [sx, sy] = toScreen(sg.mx, sg.my);
    if (sx < -10 || sy < -10 || sx > W + 10 || sy > H + 10) return;
    ctx.fillStyle = col; ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(sx, sy, 7, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.fillRect(sx - 4, sy - 1.3, 8, 2.6);
  };
  for (const gid of S.closedSegs) { const sg = SEGS.get(gid); if (sg) mark(sg, sg.construction ? T.alt : rgb(T.w[2])); }
  for (const tl of visibleTiles()) if (tl.lod === "detail") for (const sg of tl.segs) if (sg.underpass && depthAt(sg, S.t) >= 30) mark(sg, rgb(T.w[2]));
}

/* routes */
function routePath(coords) {
  const p = new Path2D();
  coords.forEach(([la, lo], i) => { const [x, y] = proj(la, lo); i ? p.lineTo(x, y) : p.moveTo(x, y); });
  return p;
}
function chosen() { const P = S.plan; return P && P.dry && S.sel !== "usual" ? "dry" : "usual"; }
let routeCache = { key: null };
function drawRoutes(px, zw) {
  const P = S.plan, key = `${P.run_id}|${P.mode}|${P.depart_min}|${P.trip?.to}|${P.trip?.from}|${SEGS.size}`;
  if (routeCache.key !== key) {
    const wet = (prof) => {
      const out = { amber: new Path2D(), red: new Path2D() };
      for (const [gid, d] of prof) { const sg = SEGS.get(gid); if (!sg) continue; if (d >= P.limit_cm) segPath(sg, out.red); else if (d >= P.limit_cm * 0.6) segPath(sg, out.amber); }
      return out;
    };
    routeCache = { key, usual: routePath(P.usual.coords), uw: wet(P.usual.profile), dry: P.dry ? routePath(P.dry.coords) : null, dw: P.dry ? wet(P.dry.profile) : null };
  }
  const R = routeCache, sel = chosen(), w = Math.max(zw, 0.8);
  const paint = (path, wt, on) => {
    if (on) {
      ctx.setLineDash([]);
      ctx.strokeStyle = T.routeCasing; ctx.lineWidth = 8 * w * px; ctx.stroke(path);
      ctx.strokeStyle = T.accent; ctx.lineWidth = 5 * w * px; ctx.stroke(path);
    } else {
      ctx.setLineDash([5 * px, 5 * px]);
      ctx.strokeStyle = T.alt; ctx.lineWidth = 2.8 * w * px; ctx.stroke(path);
      ctx.setLineDash([]);
    }
    ctx.strokeStyle = rgb(T.w[1]); ctx.lineWidth = (on ? 5 : 3.2) * w * px; ctx.stroke(wt.amber);
    ctx.strokeStyle = rgb(T.w[2]); ctx.lineWidth = (on ? 5.6 : 3.6) * w * px; ctx.stroke(wt.red);
  };
  if (sel === "dry") { paint(R.usual, R.uw, false); paint(R.dry, R.dw, true); }
  else { if (R.dry) paint(R.dry, R.dw, false); paint(R.usual, R.uw, true); }
  ctx.setLineDash([1 * px, 14 * px]); ctx.lineDashOffset = dash * px * 6;
  ctx.strokeStyle = T.dark ? "rgba(0,0,0,.7)" : "rgba(255,255,255,.9)"; ctx.lineWidth = 2.2 * w * px;
  ctx.stroke(sel === "dry" ? R.dry : R.usual);
  ctx.setLineDash([]); ctx.lineDashOffset = 0;
}
function drawRouteMarkers(ts) {
  const P = S.plan, u = P.usual.coords, a = u[0], b = u[u.length - 1], tr = P.trip || {};
  if (tr.from_label !== "Your location") startDot(...proj(a[0], a[1]));
  dropPin(...proj(b[0], b[1]));
  const route = chosen() === "dry" ? P.dry : P.usual, w = route?.worst;
  if (w && w.depth >= P.limit_cm * 0.6) { const sg = SEGS.get(w.seg); if (sg) ring(sg.mx, sg.my, `${w.depth} cm`, ts, w.depth >= P.limit_cm ? T.w[2] : T.w[1]); }
}
function startDot(x, y) {
  const [sx, sy] = toScreen(x, y);
  ctx.fillStyle = T.dark ? "#000" : "#fff"; ctx.strokeStyle = T.text; ctx.lineWidth = 3.5;
  ctx.beginPath(); ctx.arc(sx, sy, 6.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
}
function dropPin(x, y) {
  const [sx, sy] = toScreen(x, y);
  ctx.save(); ctx.translate(sx, sy);
  ctx.shadowColor = "rgba(0,0,0,.35)"; ctx.shadowBlur = 6; ctx.shadowOffsetY = 2;
  ctx.fillStyle = T.text;
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.bezierCurveTo(-3, -9, -11, -14, -11, -23); ctx.arc(0, -23, 11, Math.PI, 0); ctx.bezierCurveTo(11, -14, 3, -9, 0, 0); ctx.fill();
  ctx.shadowColor = "transparent"; ctx.fillStyle = T.dark ? "#000" : "#fff"; ctx.beginPath(); ctx.arc(0, -23, 4.2, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}
function ring(x, y, label, ts, col = T.w[2]) {
  const [sx, sy] = toScreen(x, y);
  if (sx < -80 || sy < -80 || sx > W + 80 || sy > H + 80) return;
  const ph = (ts / 1500) % 1;
  ctx.strokeStyle = `rgba(${col},${1 - ph})`; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(sx, sy, 6 + ph * 18, 0, Math.PI * 2); ctx.stroke();
  ctx.fillStyle = rgb(col); ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(sx, sy, 5.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  if (label) {
    ctx.font = `800 12.5px ${FONT}`; ctx.textAlign = "left";
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = rgb(col);
    roundRect(sx + 10, sy - 11, tw + 14, 22, 6); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.fillText(label, sx + 17, sy + 4.5);
  }
}
function roundRect(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }

function drawRain(dt) {
  ctx.lineCap = "round"; ctx.lineWidth = 1;
  for (const d of drops) {
    d.y += d.s * dt * 0.0011; d.x -= d.s * dt * 0.00032;
    if (d.y > 1) { d.y -= 1; d.x = Math.random(); }
    if (d.x < 0) d.x += 1;
    const sx = d.x * W, sy = d.y * H, [wx, wy] = toWorld(sx, sy), I = rainAt(S.t, wx, wy);
    if (I < 8) continue;
    ctx.strokeStyle = `rgba(${T.rain},${clamp((I - 8) / 70, 0, 1) * (T.dark ? 0.4 : 0.45)})`;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(sx + d.l * 0.3, sy - d.l); ctx.stroke();
  }
}
function drawStreetLabels(tiles) {
  const k = V.k;
  if (k < 0.12) return;
  const maxCls = k < 0.3 ? 0 : k < 0.6 ? 1 : 2, placed = [];
  let n = 0;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const all = [];
  for (const tl of tiles) for (const L of tl.labels) if (L.cls <= maxCls) all.push(L);
  all.sort((p, q) => p.cls - q.cls || q.sg.len - p.sg.len);
  const seen = new Set();
  for (const L of all) {
    if (n > 70) break;
    const sg = L.sg, pts = sg.pts, m = Math.floor(pts.length / 4) * 2;
    const [sx, sy] = toScreen(sg.mx, sg.my);
    if (sx < 20 || sy < 20 || sx > W - 20 || sy > H - 20) continue;
    const near = `${L.name}|${Math.round(sx / 220)}|${Math.round(sy / 220)}`;
    if (seen.has(near)) continue;
    const ia = Math.max(0, m - 2), ib = Math.min(pts.length - 2, m + 2);
    const [ax, ay] = toScreen(pts[ia], pts[ia + 1]), [bx, by] = toScreen(pts[ib], pts[ib + 1]);
    ctx.font = `${L.cls === 0 ? 700 : 600} ${L.cls === 0 ? 12.5 : 11.5}px ${FONT}`;
    const tw = ctx.measureText(L.name).width;
    if (sg.len * k < tw * 1.1) continue;
    const box = [sx - tw / 2 - 6, sy - 9, sx + tw / 2 + 6, sy + 9];
    if (placed.some((p) => !(box[2] < p[0] || box[0] > p[2] || box[3] < p[1] || box[1] > p[3]))) continue;
    placed.push(box); n++; seen.add(near);
    let ang = Math.atan2(by - ay, bx - ax);
    if (ang > Math.PI / 2) ang -= Math.PI; if (ang < -Math.PI / 2) ang += Math.PI;
    ctx.save(); ctx.translate(sx, sy); ctx.rotate(ang);
    ctx.lineWidth = 3.5; ctx.strokeStyle = T.halo; ctx.lineJoin = "round"; ctx.strokeText(L.name, 0, 0);
    ctx.fillStyle = T.label; ctx.fillText(L.name, 0, 0);
    ctx.restore();
  }
  ctx.textBaseline = "alphabetic";
}
function drawLocalityLabels(tiles) {
  ctx.font = `800 10.5px ${FONT}`; ctx.textAlign = "center";
  if ("letterSpacing" in ctx) ctx.letterSpacing = "1.6px";
  const placed = [];
  for (const tl of tiles) for (const L of tl.locs || []) {
    const [sx, sy] = toScreen(...L.xy);
    if (sx < 0 || sy < 0 || sx > W || sy > H) continue;
    const t = L.name.toUpperCase(), w = ctx.measureText(t).width, box = [sx - w / 2 - 8, sy - 10, sx + w / 2 + 8, sy + 6];
    if (placed.some((p) => !(box[2] < p[0] || box[0] > p[2] || box[3] < p[1] || box[1] > p[3]))) continue;
    placed.push(box);
    ctx.globalAlpha = V.k > 1 ? 0.5 : 0.9;
    ctx.lineWidth = 3; ctx.strokeStyle = T.halo; ctx.strokeText(t, sx, sy);
    ctx.fillStyle = T.locality; ctx.fillText(t, sx, sy);
  }
  ctx.globalAlpha = 1;
  if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
}
function drawMe(ts) {
  if (!S.me) return;
  const [x, y] = proj(S.me.lat, S.me.lon), [sx, sy] = toScreen(x, y);
  if (sx < -60 || sy < -60 || sx > W + 60 || sy > H + 60) return;
  const r = Math.max(12, Math.min(S.me.acc || 30, 500) * V.k), [mr, mg, mb] = hexRGB(T.me);
  ctx.fillStyle = `rgba(${mr},${mg},${mb},.12)`; ctx.strokeStyle = `rgba(${mr},${mg},${mb},.3)`; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  const ph = (ts / 2000) % 1;
  ctx.strokeStyle = `rgba(${mr},${mg},${mb},${0.55 * (1 - ph)})`; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(sx, sy, 9 + ph * 16, 0, Math.PI * 2); ctx.stroke();
  ctx.fillStyle = T.me; ctx.strokeStyle = "#fff"; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.arc(sx, sy, 7.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
}

/* ------------------------------------------------------------ map input */
const ptrs = new Map(); let drag = null, moved = false, pinch = null, lastTap = 0;
cv.addEventListener("pointerdown", (e) => {
  cv.setPointerCapture(e.pointerId); ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  moved = false; anim.to = null; unlockAudio(); closeFloating();
  if (ptrs.size === 1) drag = { x: e.clientX, y: e.clientY, cx: V.cx, cy: V.cy };
  if (ptrs.size === 2) { const [p, q] = [...ptrs.values()]; pinch = { d: Math.hypot(p[0] - q[0], p[1] - q[1]), k: V.k }; }
});
cv.addEventListener("pointermove", (e) => {
  if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  if (pinch && ptrs.size === 2) {
    const [p, q] = [...ptrs.values()], d = Math.hypot(p[0] - q[0], p[1] - q[1]);
    zoomAt((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, pinch.k * d / pinch.d / V.k); moved = true; return;
  }
  if (drag && ptrs.size === 1) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) { moved = true; cv.classList.add("dragging"); }
    if (moved) { V.cx = drag.cx - dx / V.k; V.cy = drag.cy - dy / V.k; }
    return;
  }
  if (e.pointerType === "mouse") hoverAt(e.clientX, e.clientY);
});
const endPtr = (e) => {
  ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null;
  if (ptrs.size === 0) {
    cv.classList.remove("dragging");
    if (drag && !moved && e.type === "pointerup") {
      const now = performance.now();
      if (now - lastTap < 300) { zoomAt(e.clientX, e.clientY, 2, true); lastTap = 0; }
      else { lastTap = now; const x = e.clientX, y = e.clientY; setTimeout(() => { if (lastTap === now) tapAt(x, y); }, 280); }
    }
    if (moved) { sendFocus(); if (S.view === "home") scheduleNearby(); }
    drag = null;
  }
};
cv.addEventListener("pointerup", endPtr); cv.addEventListener("pointercancel", endPtr);
cv.addEventListener("pointerleave", () => { S.hover = null; $("#tip").hidden = true; });
let wheelT = null;
cv.addEventListener("wheel", (e) => { e.preventDefault(); anim.to = null; zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0016)); clearTimeout(wheelT); wheelT = setTimeout(() => { sendFocus(); scheduleNearby(); }, 300); }, { passive: false });
function zoomAt(sx, sy, f, animate) {
  const [wx, wy] = toWorld(sx, sy), k = clamp(V.k * f, K_MIN, K_MAX);
  const to = { cx: wx - (sx - W / 2) / k, cy: wy - (sy - H / 2) / k, k };
  if (animate) Object.assign(anim, { from: { ...V }, to, t0: performance.now(), dur: 300 }); else Object.assign(V, to);
}
$("#zin").onclick = () => zoomAt(W / 2, H / 2, 1.8, true);
$("#zout").onclick = () => zoomAt(W / 2, H / 2, 1 / 1.8, true);
function hit(sx, sy, rpx = 12) {
  const [wx, wy] = toWorld(sx, sy), r = rpx / V.k;
  let best = null, bd = r;
  for (const tl of visibleTiles()) {
    if (tl.lod === "region" && showDetail() && tileAt(wx, wy, "detail")) continue;
    if (tl.lod === "detail" && !showDetail()) continue;
    const C = tl.CELL;
    for (let cx = Math.floor((wx - r) / C); cx <= Math.floor((wx + r) / C); cx++) for (let cy = Math.floor((wy - r) / C); cy <= Math.floor((wy + r) / C); cy++) {
      for (const i of tl.hash.get(cx + "," + cy) || []) {
        const p = tl.segs[i].pts;
        for (let q = 0; q < p.length - 2; q += 2) { const d = distSeg(wx, wy, p[q], p[q + 1], p[q + 2], p[q + 3]); if (d < bd) { bd = d; best = tl.segs[i].gid; } }
      }
    }
  }
  return best;
}
function distSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy || 1, t = clamp(((px - ax) * dx + (py - ay) * dy) / L, 0, 1);
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
}
function hoverAt(sx, sy) {
  const gid = hit(sx, sy, 9); S.hover = gid;
  const tip = $("#tip");
  if (!gid) { tip.hidden = true; return; }
  const d = Math.round(depthAt(gid, S.t));
  tip.innerHTML = `${esc(SEGS.get(gid).name)}<b class="num">${d >= 6 ? d + " cm" : "dry"}</b>`;
  tip.hidden = false;
  tip.style.left = Math.min(sx + 14, W - tip.offsetWidth - 8) + "px"; tip.style.top = (sy + 16) + "px";
}
function tapAt(sx, sy) {
  const gid = hit(sx, sy, 16);
  if (gid) openPlace({ gid });
  else if (S.view === "place") back();
}

/* ------------------------------------------------------------ timeline */
const track = $("#track");
function renderTimeline() {
  if (!S.sum) return;
  $("#tl-sub").textContent = S.sum.label.replace("Demo · ", "");
  $("#tl-sub").classList.toggle("fresh", S.fresh);
  const dm = $("#depart-mark");
  dm.hidden = S.view !== "dir";
  dm.style.left = (S.depart / 360 * 100) + "%";
  lastFrameIdx = -1;
  renderHead();
}
let lastFrameIdx = -1;
function renderHead() {
  if (!S.sum) return;
  $("#head").style.left = (S.t / 360 * 100) + "%";
  $("#tl-time").textContent = S.t < 5 ? `Now ${clockAt(0)}` : clockAt(S.t);
  track.setAttribute("aria-valuenow", Math.round(S.t)); track.setAttribute("aria-valuetext", clockAt(S.t));
  const f = clamp(Math.round(S.t / 10), 0, S.sum.rain_mean.length - 1);
  $("#tl-rain").textContent = `${S.sum.rain_mean[f]} mm/h`;
  if (f !== lastFrameIdx) { lastFrameIdx = f; renderBars(); }
}
function renderBars() {
  const hc = $("#hyeto"), r = track.getBoundingClientRect();
  if (!r.width) return;
  if (hc.width !== Math.round(r.width * DPR)) { hc.width = Math.round(r.width * DPR); hc.height = Math.round(r.height * DPR); }
  const c = hc.getContext("2d"); c.setTransform(DPR, 0, 0, DPR, 0, 0); c.clearRect(0, 0, r.width, r.height);
  const n = S.sum.rain_mean.length, bw = r.width / n, [cr, cg, cb] = T.w[1];
  S.sum.rain_mean.forEach((v, i) => {
    const h = clamp(v / 90, 0.08, 1) * r.height, past = i * 10 <= S.t;
    c.fillStyle = `rgba(${cr},${cg},${cb},${past ? 0.95 : 0.3})`;
    c.fillRect(i * bw + 0.5, r.height - h, Math.max(1, bw - 1.5), h);
  });
}
let tlTimer = null;
function onTimeChange() {
  renderHead();
  clearTimeout(tlTimer);
  tlTimer = setTimeout(() => {
    if (S.view === "home") { if (S.user === "priya") scheduleNearby(); else refreshOps(); }
    if (S.view === "place") render();
  }, S.playing ? 700 : 200);
}
function scrubTo(x) { const r = track.getBoundingClientRect(); S.t = clamp((x - r.left) / r.width * 360, 0, 360); onTimeChange(); }
let scrubbing = false;
track.addEventListener("pointerdown", (e) => { scrubbing = true; track.setPointerCapture(e.pointerId); setPlaying(false); scrubTo(e.clientX); unlockAudio(); });
track.addEventListener("pointermove", (e) => scrubbing && scrubTo(e.clientX));
track.addEventListener("pointerup", () => (scrubbing = false));
track.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { S.t = clamp(S.t + 10, 0, 360); onTimeChange(); e.preventDefault(); }
  if (e.key === "ArrowLeft") { S.t = clamp(S.t - 10, 0, 360); onTimeChange(); e.preventDefault(); }
});
function setPlaying(on) {
  S.playing = on;
  if (on && S.t >= 359) S.t = 0;
  $("#play-ico").setAttribute("d", on ? "M4.5 3h2.6v10H4.5zM9 3h2.6v10H9z" : "M4.5 2.8v10.4l8.5-5.2z");
  $("#play").setAttribute("aria-label", on ? "Pause" : "Play the next 6 hours");
}
$("#play").onclick = () => { setPlaying(!S.playing); unlockAudio(); };

/* --------------------------------------------------------------- sheet */
const sheet = $("#sheet");
const SH = { snap: "half" };
function snapHeights() {
  const peekEl = $(".peek", sheet);
  const peek = Math.round(Math.min(H * 0.42, (peekEl ? peekEl.offsetHeight : 76) + 24 + 8));
  const top = parseFloat(getComputedStyle(root).getPropertyValue("--sat")) || 0;
  return { peek, half: Math.round(H * 0.44), full: Math.round(H - 76 - top) };
}
function applySheet(px) {
  if (isDesk()) {
    root.style.setProperty("--sheet-visible", "0px"); sheet.classList.remove("peek");
    $("#timeline").style.visibility = ""; $("#ctrls").style.visibility = "";
    return;
  }
  const h = px ?? snapHeights()[SH.snap];
  root.style.setProperty("--sheet-visible", `${h}px`);
  sheet.classList.toggle("peek", SH.snap === "peek" && px == null);
  const full = SH.snap === "full" && px == null;
  $("#timeline").style.visibility = full ? "hidden" : "";
  $("#ctrls").style.visibility = full ? "hidden" : "";
}
function setSnap(s) { SH.snap = s; applySheet(); }
let sd = null;
sheet.addEventListener("pointerdown", (e) => {
  if (isDesk()) return;
  const onHandle = e.target.closest(".grab, .vhead") || (e.target.closest(".peek") && !e.target.closest("button,a,select,input"));
  if (!onHandle && SH.snap !== "peek") return;
  sd = { y: e.clientY, h: snapHeights()[SH.snap], t: performance.now(), id: e.pointerId, moved: false };
});
sheet.addEventListener("pointermove", (e) => {
  if (!sd || e.pointerId !== sd.id) return;
  const dy = e.clientY - sd.y;
  if (!sd.moved && Math.abs(dy) < 7) return;
  if (!sd.moved) { sd.moved = true; sheet.setPointerCapture(e.pointerId); sheet.classList.add("dragging"); }
  const hs = snapHeights();
  applySheet(clamp(sd.h - dy, hs.peek * 0.7, hs.full));
});
const sheetUp = (e) => {
  if (!sd || e.pointerId !== sd.id) return;
  sheet.classList.remove("dragging");
  if (sd.moved) {
    const hs = snapHeights(), cur = parseFloat(getComputedStyle(root).getPropertyValue("--sheet-visible"));
    const v = (e.clientY - sd.y) / Math.max(1, performance.now() - sd.t);
    let best = Object.entries(hs).sort((a, b) => Math.abs(a[1] - cur) - Math.abs(b[1] - cur))[0][0];
    if (v > 0.6) best = SH.snap === "full" ? "half" : "peek";
    if (v < -0.6) best = SH.snap === "peek" ? "half" : "full";
    setSnap(best);
  } else if (e.target.closest(".grab")) setSnap(SH.snap === "peek" ? "half" : SH.snap === "half" ? "full" : "half");
  sd = null;
};
sheet.addEventListener("pointerup", sheetUp);
sheet.addEventListener("pointercancel", sheetUp);

/* ------------------------------------------------------------ navigation */
const stack = [];
function go(view, snap) {
  if (S.view !== view) stack.push(S.view);
  S.view = view;
  render();
  if (snap) setSnap(snap);
}
function back() {
  sugg.hidden = true;
  if (S.view === "dir") { S.plan = null; S.advice = null; S.planWait = null; routeCache.key = null; }
  S.view = stack.pop() || "home";
  if (S.view === "dir" && !S.plan) S.view = "home";
  if (S.view === "home") { qEl.value = ""; $("#q-clear").hidden = true; S.searchFor = "dest"; S.depart = S.persona?.depart_min ?? 0; scheduleNearby(); }
  render();
  setSnap("half");
}
$("#s-back").onclick = back;
let viewsTimer = null;
function maybeRefreshViews() {
  clearTimeout(viewsTimer);
  viewsTimer = setTimeout(() => { if (S.view === "home" && !sd) render(); }, 600);
}
function render() {
  if (!S.cfg) return;
  const body = $("#sheet-body"), v = S.view, scroll = body.scrollTop;
  $("#s-back").hidden = v === "home";
  $("#s-brand").hidden = v !== "home";
  const html = v === "home" ? (S.user === "priya" ? homeView() : opsView()) : v === "place" ? placeView() : v === "dir" ? dirView()
    : v === "commute" ? commuteView() : v === "report" ? reportView() : "";
  if (html !== render.last) {  // never replace the DOM under someone's finger for nothing
    render.last = html;
    body.innerHTML = html;
    body.scrollTop = render.lastView === v ? scroll : 0;
    render.lastView = v;
    wire(body);
  }
  applySheet();
  renderTimeline();
}

const ICON = {
  home: `<svg viewBox="0 0 24 24"><path d="M4 11 12 4l8 7v8a1 1 0 0 1-1 1h-4v-5H9v5H5a1 1 0 0 1-1-1z"/></svg>`,
  trip: `<svg viewBox="0 0 24 24"><path d="M4 18h16M6 18l2-7h8l2 7M9 11l1-4h4l1 4"/></svg>`,
  pin: `<svg viewBox="0 0 24 24"><path d="M12 21s-6-5.5-6-11a6 6 0 0 1 12 0c0 5.5-6 11-6 11z"/><circle cx="12" cy="10" r="2.2"/></svg>`,
  street: `<svg viewBox="0 0 24 24"><path d="M8 3 6 21M16 3l2 18M12 5v2M12 11v2M12 17v2"/></svg>`,
  locality: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/></svg>`,
  hospital: `<svg viewBox="0 0 24 24"><path d="M12 6v12M6 12h12"/></svg>`,
  school: `<svg viewBox="0 0 24 24"><path d="m3 9 9-5 9 5-9 5zM7 11v5c3 2 7 2 10 0v-5"/></svg>`,
  clock: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>`,
  me: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/></svg>`,
  go: `<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`,
  dir: `<svg viewBox="0 0 24 24"><path d="M12 3 21 12 12 21 3 12z"/><path d="M10 14v-3h4M12.5 9l2 2-2 2"/></svg>`,
  swap: `<svg viewBox="0 0 24 24"><path d="M7 4v16M7 4 4 7M7 4l3 3M17 20V4M17 20l-3-3M17 20l3-3"/></svg>`,
  storm: `<svg viewBox="0 0 24 24"><path d="M7 15a4 4 0 1 1 1-7.9A5 5 0 0 1 18 9a3 3 0 0 1 0 6zM9 18l-1 2M13 18l-1 2M17 18l-1 2"/></svg>`,
};
function depthMeter(d) { return `<span class="meter"><i style="width:${clamp(d / 80 * 100, 3, 100)}%;background:${rgb(waterRGB(Math.max(1, d)))}"></i></span>`; }
const SHIM = `<span class="shimmer"></span><span class="shimmer" style="width:62%"></span>`;
const prepBox = (msg) => `<div class="prep"><span class="spin"></span><span>${esc(msg)}</span></div>`;

/* home */
function coverageCard() {
  const p = S.progress;
  if (!p) return "";
  const d = p.detail, r = p.region;
  const dReady = d.ready + d.empty, rReady = r.ready + r.empty;
  if (dReady >= d.tiles && rReady >= r.tiles && d.forecast >= d.ready) return "";
  const cell = (b, sub, a, n) => `<div><b>${b}</b><span>${sub}</span><span class="bar"><i style="width:${n ? Math.round(a / n * 100) : 100}%"></i></span></div>`;
  return `<div class="sec"><div class="sec-h">Loading in the background<small>you can use the map now</small></div>
    <div class="coverage">${cell("Every street", `${dReady}/${d.tiles} tiles · ${d.km} km around ${S.me ? "you" : "here"}`, dReady, d.tiles)}${cell("Main roads", `${rReady}/${r.tiles} tiles · ${r.km} km around ${S.me ? "you" : "here"}`, rReady, r.tiles)}</div></div>`;
}
function homeView() {
  const P = S.commute, per = S.persona, demo = S.sum?.mode === "demo";
  let peek = `<div class="peek sec">${SHIM}</div>`;
  if (demo && !per && S.demo?.state === "preparing") {
    peek = `<div class="peek sec"><div class="commute"><span class="ico">${ICON.storm}</span><span class="t">Setting up the demo</span>
      <span class="d"><span class="spin"></span></span><span class="s">Running both nowcasts on the streets around you and picking a commute</span></div></div>`;
  } else if (S.personaLoaded && !per) {
    peek = `<div class="peek sec"><button class="commute" data-act="edit-commute"><span class="ico">${ICON.home}</span><span class="t">Set your commute</span>
      <span class="d"><span class="pill watch">Not set</span></span><span class="s">Home, work and when you leave. We warn you before your route floods.</span></button></div>`;
  } else if (per && per.in_window === false) {
    peek = `<div class="peek sec"><button class="commute" data-act="commute"><span class="ico">${ICON.home}</span><span class="t">Home at ${esc(per.depart_label || per.leave)}</span>
      <span class="d"><span class="pill clear">Later</span></span><span class="s">${esc(per.from_label)} → ${esc(per.to_label)} · forecast covers the next 6 hours</span></button></div>`;
  } else if (per && S.commuteWait) {
    peek = `<div class="peek sec"><button class="commute" data-act="commute"><span class="ico">${ICON.home}</span><span class="t">Home at ${esc(clockAt(per.depart_min))}</span>
      <span class="d"><span class="spin"></span></span><span class="s">${esc(S.commuteWait)}</span></button></div>`;
  } else if (P && per) {
    const w = P.usual.worst, st = P.status;
    peek = `<div class="peek sec"><button class="commute" data-act="commute">
      <span class="ico">${ICON.home}</span>
      <span class="t">Home at ${esc(clockAt(per.depart_min))}</span>
      <span class="d"><b class="num">${w ? w.depth : 0}<small> cm</small></b><span class="pill ${st}">${st === "alert" ? "Flooded" : st === "watch" ? "Watch" : "Clear"}</span></span>
      <span class="s">${esc(per.from_label)} → ${esc(per.to_label)} · ${esc(modeLabel(S.mode))}</span>
    </button></div>`;
  }
  const near = S.nearby, anchorMe = !!S.me;
  const nearList = !near ? SHIM
    : near.streets.length ? `<div class="list">${near.streets.map((r) => `<button class="li" data-gid="${esc(r.seg_id)}"><span class="n">${esc(r.name)}</span><span class="d num" style="color:${rgb(waterRGB(r.depth_cm))}">${r.depth_cm} cm</span><span class="s">${esc(r.locality)}</span>${depthMeter(r.depth_cm)}</button>`).join("")}</div>`
    : `<div class="empty">No streets above 15 cm ${anchorMe ? "near you" : "here"} at ${esc(clockAt(S.t))}.</div>`;
  const fc = S.sum;
  if (!fc) return peek;
  let peakI = 0; fc.rain_mean.forEach((v, i) => { if (v > fc.rain_mean[peakI]) peakI = i; });
  const { worstF, worstN } = worstMoment();
  const short = (t) => clockAt(t).replace(" PM", "").replace(" AM", "");
  const live = fc.mode === "live";
  return `${peek}
    <div class="sec"><div class="sec-h">Flooding ${anchorMe ? "near you" : "here"}<small>at ${esc(clockAt(S.t))}</small></div>${nearList}</div>
    <div class="sec"><div class="sec-h">Next 6 hours<small>${live ? '<span class="live-dot"></span>live' : esc(fc.mode === "demo" ? "demo storm" : "replay")}</small></div>
      ${fc.total_mm < 1 ? `<div class="empty">No rain expected here in the next 6 hours.${live ? " The forecast refreshes every 15 minutes. Try the demo storm in How it works." : ""}</div>` : ""}
      ${contextLines()}
      <div class="kv2"><div><b class="num">${fc.total_mm}</b><span>mm of rain</span></div><div><b class="num">${esc(short(fc.times[peakI]))}</b><span>heaviest rain</span></div><div><b class="num">${worstN}</b><span>streets over ${limit()} cm on screen</span></div></div>
      ${worstN ? `<button class="btn ghost" data-act="worst" data-f="${worstF}">Show the worst moment, ${esc(clockAt(worstF * 10))}</button>` : ""}</div>
    ${coverageCard()}
    <div class="sec"><div class="btns">${!live ? "" : `<button class="btn ghost" data-act="demo-here">${ICON.storm}Demo storm here</button>`}
        ${per && live ? `<button class="btn ghost" data-act="edit-commute">Edit commute</button>` : ""}
        <button class="btn ghost" data-act="how">How it works</button></div>
      <div class="hint">${S.cfg.synthetic ? "Synthetic streets (offline mode)." : "Streets © OpenStreetMap contributors · terrain: AWS Terrain Tiles."}</div></div>`;
}
function worstMoment() {
  let worstF = 0, worstN = 0;
  const tiles = visibleTiles().filter((t) => t.lod === "detail" && t.fc);
  if (!tiles.length) return { worstF, worstN };
  const n = tiles[0].fc.n, L = limit();
  for (let f = 0; f < n; f++) {
    let c = 0;
    for (const tl of tiles) { const N = tl.fc.nseg, seg = tl.fc.seg; for (let i = 0; i < N; i++) if (seg[f * N + i] >= L) c++; }
    if (c > worstN) { worstN = c; worstF = f; }
  }
  return { worstF, worstN };
}
function contextLines() {
  const c = S.sum.context || {}, out = [];
  const rp = S.sum.replay;
  if (rp && rp.name) out.push(`Replaying <b>${esc(rp.name)}</b> (${esc(rp.date || "")}): ${rp.total_6h} mm in its wettest 6 hours, from recorded rain.`);
  if (S.sum.mode === "demo") out.push(`An <b>elevated storm</b> on your real streets: only the rain is scripted.`);
  if (c.return_years) out.push(`Heaviest hour <b>${c.peak_hourly_mm} mm/h</b>: about a <b>1-in-${c.return_years >= 10 ? Math.round(c.return_years) : c.return_years}-year</b> downpour here.`);
  else if (c.peak_hourly_mm > 5) out.push(`Heaviest hour <b>${c.peak_hourly_mm} mm/h</b>.`);
  if (c.ground) out.push(`Ground is <b>${esc(c.ground.ground)}</b>: ${c.ground.rain_7d} mm in the last 7 days.`);
  if (c.tide?.next_high_min != null) out.push(`High tide at <b>${esc(clockAt(c.tide.next_high_min))}</b>: drains in low-lying streets empty more slowly.`);
  return out.length ? `<div class="ctx">${out.map((x) => `<span>${x}</span>`).join("")}</div>` : "";
}

/* commute editor */
function commuteView() {
  const d = S.draft;
  const wp = (k, mk, lbl) => `<span class="wp-mk ${mk}"></span><button class="wp" data-act="pick-${k}"><span class="lbl">${lbl}</span>${esc(d[k]?.label || "Choose")}</button>`;
  return `<div class="sec"><div class="place-title">Your commute</div>
      <div class="sub">We check it every time the forecast updates and warn you before you leave.</div>
      <div class="wps">${wp("work", "", "From")}<span></span>${wp("home", "end", "To")}<span></span></div>
      <label class="field">Leave at <input type="time" id="leave-time" value="${esc(d.leave)}"></label>
      <div class="seg" role="radiogroup" aria-label="Travel mode">${MODES.map(([m, l]) => `<button data-dmode="${m}" class="${m === d.mode ? "on" : ""}">${l}</button>`).join("")}</div>
      <div class="btns"><button class="btn primary" data-act="save-commute" ${d.home && d.work ? "" : "disabled"}>Save commute</button>
        ${S.me ? `<button class="btn ghost" data-act="me-home">Use my location as home</button>` : ""}</div>
      <div class="hint">Saved on this server only. Cedar lets nobody but you read it.</div></div>`;
}
function openCommuteEditor(patch) {
  const p = S.persona && S.persona.name === "You" ? S.persona : null;
  S.draft = S.draft || {
    work: p ? { lat: p.from[0], lon: p.from[1], label: p.from_label } : null,
    home: p ? { lat: p.to[0], lon: p.to[1], label: p.to_label } : (S.me ? { lat: S.me.lat, lon: S.me.lon, label: "Home (here)" } : null),
    leave: p?.leave || "18:00", mode: p?.mode || S.mode,
  };
  Object.assign(S.draft, patch || {});
  go("commute", "half");
}
async function saveCommute() {
  const d = S.draft;
  const r = await api("/api/commute", { user: "priya", from: [d.work.lat, d.work.lon], to: [d.home.lat, d.home.lon],
    from_label: d.work.label, to_label: d.home.label, leave: d.leave, mode: d.mode });
  if (!r.ok) { notice(`<div class="b">Couldn't save: ${esc(r.j?.decision?.note || r.j?.detail || "error")}</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`); return; }
  S.persona = r.j.persona; S.mode = d.mode; prefs.mode = d.mode; S.draft = null; savePrefs();
  stack.length = 0; S.view = "home"; render(); setSnap("half");
  refreshCommute();
}

/* report water */
function reportView() {
  const at = S.reportAt;
  const opts = [[0, "Dry road", "No water"], [10, "Ankle", "about 10 cm"], [25, "Shin", "about 25 cm"], [45, "Knee", "about 45 cm"], [90, "Waist", "about 90 cm"], [-1, "Road closed", "barricade or police"]];
  return `<div class="sec"><div class="place-title">How deep is the water?</div>
      <div class="sub">At ${esc(at.label)}. Reports are anonymous; they correct the forecast nearby for the next 2 hours and show on everyone's map.</div>
      <div class="depths">${opts.map(([d, n, sub]) => `<button data-depth="${d}"><b>${n}</b><span>${sub}</span><i style="background:${d < 0 ? `repeating-linear-gradient(90deg, ${rgb(T.w[2])} 0 6px, transparent 6px 10px)` : d ? rgb(waterRGB(d)) : "var(--text)"}"></i></button>`).join("")}</div></div>`;
}
function openReport() {
  let at;
  if (S.view === "place" && S.place) at = { lat: S.place.lat, lon: S.place.lon, label: S.place.name };
  else if (S.me) at = { lat: S.me.lat, lon: S.me.lon, label: "your location" };
  else {
    const [x, y] = toWorld(isDesk() ? (W + 424) / 2 : W / 2, isDesk() ? H / 2 : H / 3), gid = hit(...toScreen(x, y), 60), sg = gid && SEGS.get(gid);
    const [la, lo] = sg ? unproj(sg.mx, sg.my) : unproj(x, y);
    at = { lat: la, lon: lo, label: sg ? sg.name : "the middle of the map" };
  }
  S.reportAt = at;
  go("report", "half");
}
async function sendReport(depth) {
  const at = S.reportAt;
  const r = await api("/api/report", { user: S.user, lat: at.lat, lon: at.lon, depth_cm: depth });
  if (!r.ok) { notice(`<div class="b">${r.status === 0 ? "You're offline. Try again when you're connected." : esc(r.j?.detail || "Couldn't send the report.")}</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`); return; }
  back();
  if (depth < 0) { await loadClosures(); if (S.view === "dir") refreshPlan(); }
  snack(`Thanks. Reported: ${esc(r.j.report.text)}.`);
}
function snack(html, ms = 6000) {
  const sn = $("#snack");
  sn.innerHTML = `<span>${html}</span><button>OK</button>`; sn.hidden = false; delete sn.dataset.reload;
  $("button", sn).onclick = () => (sn.hidden = true);
  clearTimeout(snack.t); snack.t = setTimeout(() => (sn.hidden = true), ms);
}
$("#report").onclick = () => { unlockAudio(); openReport(); };
async function loadClosures() {
  const r = await api("/api/closures");
  if (r.ok) { S.closures = r.j.closures; S.closedSegs = new Set(r.j.closures.filter((c) => c.closes).flatMap((c) => c.segs)); S.tomtom = r.j.tomtom; S.replays = r.j.replays; }
}
async function loadReports() {
  const r = await api("/api/reports?hours=6");
  if (r.ok) { S.reports = r.j.reports; }
}
function drawReports() {
  if (!S.reports.length || V.k < 0.1) return;
  const now = Date.now() / 1000;
  for (const r of S.reports) {
    if (r.kind === "closed") continue;
    const [sx, sy] = toScreen(...proj(r.lat, r.lon));
    if (sx < -20 || sy < -20 || sx > W + 20 || sy > H + 20) continue;
    const age = Math.max(0, (now - r.ts) / 60), fade = clamp(1 - age / 360, 0.35, 1);
    const col = r.depth_cm_reported ? waterRGB(r.depth_cm_reported) : hexRGB(T.text);
    ctx.globalAlpha = fade;
    ctx.fillStyle = T.dark ? "#000" : "#fff"; ctx.fillRect(sx - 8, sy - 8, 16, 16);
    ctx.fillStyle = rgb(col); ctx.fillRect(sx - 5.5, sy - 5.5, 11, 11);
    if (V.k > 0.6) {
      const label = `${r.depth_cm_reported ? r.depth_cm_reported + " cm" : "dry"} · ${age < 60 ? Math.round(age) + " min" : Math.round(age / 60) + " h"} ago`;
      ctx.font = `700 11.5px ${FONT}`; ctx.textAlign = "left";
      ctx.lineWidth = 3; ctx.strokeStyle = T.halo; ctx.strokeText(label, sx + 12, sy + 4);
      ctx.fillStyle = T.label; ctx.fillText(label, sx + 12, sy + 4);
    }
    ctx.globalAlpha = 1;
  }
}

/* place */
function placeView() {
  const p = S.place;
  if (!p) return "";
  if (!p.timeline) return `<div class="sec"><div class="place-title">${esc(p.name)}</div>${SHIM}</div>`;
  const tl = p.timeline.length ? p.timeline : [0];
  const d = SEGS.get(p.gid) ? Math.round(depthAt(p.gid, S.t)) : tl[clamp(Math.round(S.t / 10), 0, tl.length - 1)];
  const st = statusOf(d), peak = Math.max(...tl), pk = tl.indexOf(peak);
  const W2 = 320, H2 = 80, mx = Math.max(60, peak, limit() * 1.2);
  const pts = tl.map((v, f) => `${(f / Math.max(1, tl.length - 1) * W2).toFixed(1)},${(H2 - v / mx * (H2 - 4)).toFixed(1)}`).join(" ");
  const ly = H2 - limit() / mx * (H2 - 4), nx = S.t / 360 * W2;
  return `<div class="sec">
      <div class="place-title">${esc(p.name)}</div>
      <div class="sub">${esc(p.locality)} · ${esc((p.highway || "").replace("_", " "))} · ${p.length_m} m${p.lod === "region" ? " · main-road forecast" : ""}</div>
      ${placeTags(p, d)}
      <div class="big"><b class="num" style="color:${d >= 6 ? rgb(waterRGB(d)) : "var(--text)"}">${d >= 6 ? d : 0}</b><span>cm at ${esc(clockAt(S.t))}</span><span class="pill ${st}">${st === "alert" ? `Too deep to ${S.mode === "walk" ? "walk" : "ride"}` : st === "watch" ? "Near the limit" : "Passable"}</span></div>
      <div class="curve"><svg viewBox="0 0 ${W2} ${H2}" preserveAspectRatio="none" aria-label="Depth over the next 6 hours, peaking at ${peak} cm">
        <line x1="0" x2="${W2}" y1="${ly}" y2="${ly}" stroke="${rgb(T.w[2])}" stroke-dasharray="4 4" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
        <polygon points="0,${H2} ${pts} ${W2},${H2}" fill="${rgb(T.w[1])}" opacity=".14"/>
        <polyline points="${pts}" fill="none" stroke="${rgb(T.w[1])}" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
        <line x1="${nx}" x2="${nx}" y1="0" y2="${H2}" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
        <div class="ax"><span>Now</span><span>Peak ${peak} cm at ${esc(clockAt(pk * 10))} · dashed: ${limit()} cm limit</span><span>+6 h</span></div></div>
      <div class="btns"><button class="btn primary" data-act="dir-here">${ICON.dir}Directions</button><button class="btn ghost" data-act="report-here">Report water</button><button class="btn ghost" data-act="peak" data-f="${pk}">Show peak</button></div>
      <div class="btns"><button class="btn ghost" data-act="save-home">Set as home</button><button class="btn ghost" data-act="save-work">Set as work</button></div>
    </div>
    <div class="sec"><div class="sec-h">Past flood reports nearby<small>OpenSearch</small></div>
      ${p.reports.length ? p.reports.map((r) => `<div class="rep"><span class="d">${esc(r.date || "")}${r.source === "synthetic-seed" ? '<span class="seed">seed data</span>' : r.source === "documented" ? '<span class="seed doc">documented</span>' : r.source === "resident" ? '<span class="seed">resident</span>' : ""}${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">source</a>` : ""}</span>${esc(r.text)}</div>`).join("") : `<div class="empty">No reports within 400 m.</div>`}
    </div>`;
}
async function openPlace(p) {
  closeFloating();
  if (!p.gid) return;
  const sg = SEGS.get(p.gid);
  if (sg) { const [la, lo] = unproj(sg.mx, sg.my); S.place = { gid: p.gid, name: sg.name, lat: la, lon: lo }; flyTo(sg.mx, sg.my, Math.max(V.k, 0.8)); }
  else { S.place = { gid: p.gid, name: p.name || "Street", lat: p.lat, lon: p.lon }; if (p.lat) flyTo(...proj(p.lat, p.lon), Math.max(V.k, 0.8)); }
  qEl.value = S.place.name; $("#q-clear").hidden = false;
  go("place", "half");
  const r = await api(`/api/segment/${p.gid}`);
  if (r.ok && S.place?.gid === p.gid) { Object.assign(S.place, r.j); render(); }
  else if (!r.ok && S.place?.gid === p.gid) { S.place.timeline = []; S.place.reports = []; render(); }
}
function placeTags(p, d) {
  const t = [];
  if (p.underpass) t.push(d >= 30 ? `<span class="tag bad">Subway · likely closed (city shuts subways around 30 cm)</span>` : `<span class="tag warn">Subway · floods first in heavy rain</span>`);
  if (p.bridge) t.push(`<span class="tag">Bridge or flyover · above the water</span>`);
  if (p.construction) t.push(`<span class="tag">Under construction · closed</span>`);
  for (const c of p.closures || []) if (c.source !== "OpenStreetMap") t.push(`<span class="tag ${c.closes ? "bad" : "warn"}">${esc(c.kind)} · ${esc(c.source)}${c.desc && c.desc !== c.kind ? ": " + esc(c.desc) : ""}</span>`);
  return t.length ? `<div class="tags">${t.join("")}</div>` : "";
}

/* directions */
function viaOf(route) {
  const tally = new Map();
  for (const [gid] of route.profile) { const sg = SEGS.get(gid); if (!sg || sg.name.startsWith("Unnamed")) continue; tally.set(sg.name, (tally.get(sg.name) || 0) + sg.len); }
  const top = [...tally].sort((a, b) => b[1] - a[1])[0];
  return top ? `via ${top[0]}` : "";
}
function strip(route, lim) {
  const lens = route.profile.map(([gid]) => SEGS.get(gid)?.len || 60), tot = lens.reduce((a, b) => a + b, 0) || 1;
  return `<span class="strip">${route.profile.map(([, d], i) => `<i style="width:${(lens[i] / tot * 100).toFixed(2)}%;background:${d >= lim * 0.6 ? rgb(waterRGB(d)) : "var(--text)"};opacity:${d >= lim * 0.6 ? 1 : 0.35}"></i>`).join("")}</span>`;
}
function dirView() {
  const tr = S.trip;
  if (!tr) return "";
  const P = S.plan, adv = S.advice, fromMe = tr.from_label === "Your location";
  let banner = S.planWait ? prepBox(S.planWait) : `<div class="banner clear"><span class="bi">·</span>${SHIM}</div>`, opts = "", start = "";
  if (P) {
    const st = P.status, w = P.usual.worst;
    const head = adv?.headline || (st === "alert" ? `${w.name}: ${w.depth} cm` : st === "watch" ? `Water building on ${w.name}` : "Your route is dry");
    const body = S.adviceLoading ? SHIM : esc(adv?.advice || "");
    banner = `<div class="banner ${st}"><span class="bi">${st === "clear" ? "✓" : "!"}</span><span class="h">${esc(head)}</span><span class="b">${body}</span>
      ${adv ? `<span class="by">${adv.engine.startsWith("strands") ? "Written by the Strands agent" : "Template advice (agent offline)"} · <button data-act="why">why?</button></span>` : ""}</div>`;
    const sel = chosen();
    const row = (key, r, name) => {
      if (!r) return "";
      const pills = r.flooded.length ? r.flooded.slice(0, 2).map((f) => `<span class="pill alert">${esc(f.name)} ${f.depth} cm</span>`).join("")
        : `<span class="pill ${r.max_depth >= P.limit_cm * 0.6 ? "watch" : "clear"}">${r.max_depth < 6 ? "Dry" : `Max ${r.max_depth} cm`}</span>`;
      const via = viaOf(r);
      return `<button class="opt ${sel === key ? "on" : ""}" data-sel="${key}"><span class="bar"></span><span class="m num">${r.minutes} min</span><span class="k num">${r.km} km</span>
        <span class="v">${esc(name)}${via ? " · " + esc(via) : ""}</span><span class="w">${pills}</span>${strip(r, P.limit_cm)}</button>`;
    };
    opts = `<div class="list">${row("dry", P.dry, "Dry route · recommended")}${row("usual", P.usual, P.dry ? "Usual route" : "Fastest route")}</div>`;
    if (P.avoided?.length || P.subways_closed?.length) opts += `<div class="tags" style="margin-top:8px">${(P.avoided || []).map((a) => `<span class="tag warn">Avoids ${esc(a.name)} · ${esc(a.why)}</span>`).join("")}${(P.subways_closed || []).map((n) => `<span class="tag bad">${esc(n)} subway likely closed</span>`).join("")}</div>`;
    const link = sel === "dry" ? P.gmaps : (P.gmaps_usual || P.gmaps);
    start = `<div class="gobar"><a class="btn primary block" href="${esc(link)}" target="_blank" rel="noopener">${ICON.go}Start in Google Maps · ${(sel === "dry" ? P.dry : P.usual).minutes} min</a></div>`;
    opts += `<div class="note" style="margin-top:6px">${sel === "dry" ? "Google Maps opens with stops that keep you on the dry route." : "Opens this route in Google Maps."}${P.lod === "main roads" ? ` A ${P.corridor_km} km trip: main roads are checked along the way, every street near both ends.` : ""}</div>`;
  }
  return `<div class="sec">
      <div class="wps">
        <span class="wp-mk ${fromMe ? "me" : ""}"></span><button class="wp" data-act="pick-from"><span class="lbl">From</span>${esc(tr.from_label)}</button>
        <button class="s-ic swap" data-act="swap" aria-label="Swap start and destination">${ICON.swap}</button>
        <span class="wp-mk end"></span><button class="wp" data-act="pick-to"><span class="lbl">To</span>${esc(tr.to_label)}</button>
      </div>
      <div class="opts-row">
        <div class="seg" role="radiogroup" aria-label="Travel mode">${MODES.map(([m, l]) => `<button data-mode="${m}" class="${m === S.mode ? "on" : ""}" role="radio" aria-checked="${m === S.mode}">${l}</button>`).join("")}</div>
        <select id="leave" aria-label="Leave at">${LEAVE.map((t) => `<option value="${t}" ${t === S.depart ? "selected" : ""}>${t === 0 ? "Leave now" : "Leave " + clockAt(t)}</option>`).join("")}</select>
      </div>
      ${banner}
    </div>
    <div class="sec">${opts || (S.planWait ? `<div class="hint">Streets along the way are loading in the background. This updates by itself.</div>` : SHIM)}</div>${start}`;
}
function openCommute() {
  if (!S.persona) return openCommuteEditor();
  const p = S.persona;
  S.trip = { kind: "commute", from: p.from, to: p.to, from_label: p.from_label, to_label: p.to_label };
  S.depart = Math.min(p.depart_min ?? 0, 340); S.t = S.depart; S.sel = "best";
  qEl.value = p.to_label; $("#q-clear").hidden = false;
  go("dir", "half");
  refreshPlan(true);
}
function openDirectionsTo(to) {
  const from = S.me ? { lat: S.me.lat, lon: S.me.lon, label: "Your location" }
    : S.view === "dir" && S.trip ? { lat: S.trip.from[0], lon: S.trip.from[1], label: S.trip.from_label }
    : S.persona ? { lat: S.persona.from[0], lon: S.persona.from[1], label: S.persona.from_label }
    : (() => { const [la, lo] = unproj(...toWorld(isDesk() ? (W + 424) / 2 : W / 2, H / 2)); return { lat: la, lon: lo, label: "Map centre" }; })();
  S.trip = { kind: "custom", from: [from.lat, from.lon], to: [to.lat, to.lon], from_label: from.label, to_label: to.name };
  if (S.view !== "dir") { S.depart = 0; S.t = 0; }
  S.sel = "best";
  qEl.value = to.name; $("#q-clear").hidden = false;
  go("dir", "half");
  refreshPlan(true);
}
function tripBody() {
  const t = S.trip;
  if (!t || t.kind === "commute") return {};
  return { from: t.from, to: t.to, from_label: t.from_label, to_label: t.to_label };
}
let planToken = 0, planRetry = null;
async function refreshPlan(fit) {
  const tok = ++planToken;
  clearTimeout(planRetry);
  if (fit !== false && !S.planWait) { S.plan = null; S.advice = null; routeCache.key = null; render(); }  // keep the old plan on screen while refreshing
  if (fit && S.trip) fitPoints([S.trip.from, S.trip.to].map(([a, b]) => proj(a, b)));
  const r = await api("/api/route", { user: "priya", mode: S.mode, depart_min: S.depart, ...tripBody() });
  if (tok !== planToken) return;
  if (r.status === 409) { S.view = "home"; openCommuteEditor(); return; }
  if (r.status === 202) { S.planWait = r.j.message; render(); planRetry = setTimeout(() => refreshPlan(false), 2500); return; }
  S.planWait = null;
  if (!r.ok) { $("#sheet-body").innerHTML = `<div class="sec"><div class="empty">${r.status === 0 ? "You're offline, and this route isn't saved on this device." : esc(r.j?.message || "Couldn't plan that route.")}</div></div>`; return; }
  S.plan = r.j; routeCache.key = null; S.adviceLoading = !S.advice; render();
  if (fit !== false) fitPoints([...S.plan.usual.coords, ...(S.plan.dry?.coords || [])].map(([a, b]) => proj(a, b)));
  const a = await api("/api/advise", { user: "priya", mode: S.mode, depart_min: S.depart, ...tripBody() });
  if (tok !== planToken) return;
  S.adviceLoading = false; S.advice = a.ok && a.status === 200 ? a.j : null; render();
}
let commuteRetry = null;
async function refreshCommute() {
  clearTimeout(commuteRetry);
  if (!S.persona) { S.commute = null; S.commuteWait = null; if (S.view === "home") render(); return; }
  const r = await api("/api/route", { user: "priya", mode: S.mode, depart_min: S.persona.depart_min ?? 0 });
  if (r.status === 202) { S.commuteWait = r.j.message; commuteRetry = setTimeout(refreshCommute, 2500); }
  else { S.commuteWait = null; if (r.ok) S.commute = r.j; }
  if (S.view === "home") render();
}
async function loadPersona() {
  const c = await api(`/api/commute?user=priya`);
  if (c.ok) { S.persona = c.j.persona; S.demo = c.j.demo || S.demo; }
  S.personaLoaded = true;
  if (S.persona && !prefs.mode && S.persona.mode) S.mode = S.persona.mode;
  S.depart = S.persona?.depart_min ?? 0;
  await refreshCommute();
}

/* ward desk / control room */
function opsView() {
  const O = S.ops, u = S.cfg.users[S.user];
  if (!O) return `<div class="peek sec">${SHIM}</div>`;
  if (O.forbidden) return `<div class="peek sec"><div class="empty">No access to ward operations.</div></div>`;
  const total = O.localities.reduce((s, l) => s + l.impassable, 0), one = O.localities.length === 1 ? O.localities[0] : null;
  const rows = one ? one.flooded.map((f) => ({ gid: f.seg_id, n: f.name, d: f.depth_cm, s: f.locality }))
    : O.localities.map((l) => ({ gid: l.worst?.seg_id, n: l.locality, d: l.max_depth, s: `${l.impassable} streets over 30 cm${l.worst ? " · worst " + l.worst.name : ""}` }));
  const vul = S.vuln?.sites || [];
  return `<div class="peek sec"><div class="commute">
      <span class="ico">${ICON.locality}</span><span class="t">${S.user === "control" ? "Control room · all localities" : `Ward desk · ${esc(one?.locality || u.locality || u.label)}`}</span>
      <span class="d"><b class="num">${total}</b><span>over 30 cm</span></span><span class="s">at ${esc(O.clock)} · ${esc(O.decision.policies[0] || "allowed")}</span></div></div>
    <div class="sec"><div class="sec-h">${one ? "Streets to clear first" : "Localities by priority"}<small>OpenSearch aggregation</small></div>
      <div class="list">${rows.slice(0, 8).map((x) => `<button class="li" ${x.gid ? `data-gid="${esc(x.gid)}"` : ""}><span class="n">${esc(x.n)}</span><span class="d num" style="color:${rgb(waterRGB(Math.max(1, x.d)))}">${x.d} cm</span><span class="s">${esc(x.s)}</span>${depthMeter(x.d)}</button>`).join("") || `<div class="empty">Nothing above 30 cm at this time.</div>`}</div>
      ${O.denied?.length ? `<div class="hint">${O.denied.length} other localities are hidden by Cedar policy.</div>` : ""}</div>
    ${S.user === "control" ? `<div class="sec"><div class="sec-h">Hospitals and schools with flooded access<small>within 200 m</small></div>
      <div class="list">${vul.map((p) => `<div class="li"><span class="n">${esc(p.name)}</span><span class="d num" style="color:${rgb(waterRGB(p.depth))}">${p.depth} cm</span><span class="s">${esc(p.kind)} · ${esc(p.street)}</span></div>`).join("") || `<div class="empty">None at this time.</div>`}</div></div>` : ""}`;
}
async function refreshOps() {
  const t = Math.round(S.t / 10) * 10;
  if (S.user !== "priya") { const r = await api(`/api/ops?user=${S.user}&t=${t}`); S.ops = r.ok ? r.j : { forbidden: true }; }
  if (S.user === "control") { const v = await api(`/api/vulnerable?user=control&t=${t}`); S.vuln = v.ok ? v.j : null; }
  const w = await api("/api/world"); if (w.ok) S.cfg.users = w.j.users;
  if (S.view === "home") render();
}

/* nearby */
let nearTimer = null;
function scheduleNearby() { clearTimeout(nearTimer); nearTimer = setTimeout(refreshNearby, 250); }
async function refreshNearby() {
  if (S.user !== "priya" || !S.cfg) return;
  let lat, lon;
  if (S.me) ({ lat, lon } = S.me);
  else [lat, lon] = unproj(...toWorld(isDesk() ? (W + 424) / 2 : W / 2, isDesk() ? H / 2 : H / 3));
  const r = await api(`/api/nearby?lat=${lat}&lon=${lon}&t=${Math.round(S.t / 10) * 10}`);
  if (r.ok) { S.nearby = r.j; if (S.view === "home") render(); }
}

/* --------------------------------------------------------- wire actions */
function wire(el) {
  $$("[data-act]", el).forEach((b) => b.addEventListener("click", () => act(b.dataset.act, b)));
  $$("[data-gid]", el).forEach((b) => b.addEventListener("click", () => openPlace({ gid: b.dataset.gid })));
  $$("[data-mode]", el).forEach((b) => b.addEventListener("click", () => { S.mode = b.dataset.mode; prefs.mode = S.mode; savePrefs(); for (const tl of TILES.values()) { tl.rasKey = null; tl.wetKey = null; } refreshPlan(); refreshCommute(); }));
  $$("[data-sel]", el).forEach((b) => b.addEventListener("click", () => { S.sel = b.dataset.sel; routeCache.key = null; render(); }));
  $$("[data-depth]", el).forEach((b) => b.addEventListener("click", () => sendReport(+b.dataset.depth)));
  $$("[data-dmode]", el).forEach((b) => b.addEventListener("click", () => { S.draft.mode = b.dataset.dmode; render(); }));
  const lt = $("#leave-time", el);
  if (lt) lt.addEventListener("change", () => { S.draft.leave = lt.value || "18:00"; });
  const lv = $("#leave", el);
  if (lv) lv.addEventListener("change", () => { S.depart = +lv.value; S.t = S.depart; setPlaying(false); refreshPlan(); });
}
function act(a, b) {
  if (a === "commute") openCommute();
  if (a === "edit-commute") openCommuteEditor();
  if (a === "save-commute") saveCommute();
  if (a === "me-home") { S.draft.home = { lat: S.me.lat, lon: S.me.lon, label: "Home (here)" }; render(); }
  if (a === "pick-home" || a === "pick-work") { S.searchFor = a.slice(5); qEl.value = ""; qEl.placeholder = `Choose ${S.searchFor}`; qEl.focus(); }
  if (a === "save-home" || a === "save-work") openCommuteEditor({ [a.slice(5)]: { lat: S.place.lat, lon: S.place.lon, label: S.place.name } });
  if (a === "report-here") openReport();
  if (a === "why") { S.tab = "why"; toggleDrawer(true); }
  if (a === "how") { S.tab = "system"; toggleDrawer(true); }
  if (a === "worst" || a === "peak") { S.t = +b.dataset.f * 10; setPlaying(false); onTimeChange(); if (S.view === "place") render(); }
  if (a === "dir-here") openDirectionsTo({ name: S.place.name, lat: S.place.lat, lon: S.place.lon });
  if (a === "pick-to" || a === "pick-from") {
    S.searchFor = a === "pick-from" ? "from" : "dest";
    qEl.value = ""; qEl.placeholder = S.searchFor === "from" ? "Choose a start" : "Choose a destination"; qEl.focus();
  }
  if (a === "swap" && S.trip) {
    const t = S.trip;
    S.trip = { kind: "custom", from: t.to, to: t.from, from_label: t.to_label, to_label: t.from_label };
    qEl.value = S.trip.to_label;
    refreshPlan(true);
  }
  if (a === "demo-here") setRain("demo");
  if (a === "tick" || a === "reset") systemAct(a, b);
}
async function setRain(source) {
  const [lat, lon] = S.me ? [S.me.lat, S.me.lon] : unproj(V.cx, V.cy);
  snack(source === "demo" ? "Starting the demo storm on the streets around you…" : "Switching rain source…", 4000);
  const r = await api("/api/rain", { source, lat, lon });
  if (!r.ok) notice(`<div class="h">Couldn't switch</div><div class="b">${esc(r.j?.error || "Try again in a moment.")}</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`);
}

/* --------------------------------------------------------------- search */
const qEl = $("#q"), sugg = $("#sugg");
let suggItems = [], farItems = [], suggOn = -1, qTimer = null, gTimer = null;
function searchNear() { return S.me ? [S.me.lat, S.me.lon] : unproj(V.cx, V.cy); }
function showSuggestions() {
  const q = qEl.value.trim();
  clearTimeout(qTimer); clearTimeout(gTimer);
  if (q.length < 2) {
    const quick = [];
    if (S.me && S.searchFor !== "dest") quick.push({ name: "Your location", kind: "me", lat: S.me.lat, lon: S.me.lon });
    if (S.persona && S.searchFor === "dest") {
      quick.push({ name: "Home", sub: S.persona.to_label, kind: "home", lat: S.persona.to[0], lon: S.persona.to[1] });
      quick.push({ name: "Work", sub: S.persona.from_label, kind: "work", lat: S.persona.from[0], lon: S.persona.from[1] });
    }
    suggItems = [...quick, ...(prefs.recent || []).slice(0, 4).map((r) => ({ ...r, recent: true }))]; farItems = []; suggOn = -1;
    renderSugg(false);
    return;
  }
  const [lat, lon] = searchNear();
  qTimer = setTimeout(async () => { const r = await api(`/api/search?q=${encodeURIComponent(q)}&lat=${lat}&lon=${lon}`); suggItems = r.j?.results || []; suggOn = -1; renderSugg(true); }, 130);
  if (q.length >= 3) gTimer = setTimeout(async () => {
    const r = await api(`/api/geocode?q=${encodeURIComponent(q)}&lat=${lat}&lon=${lon}`);
    const have = new Set(suggItems.map((p) => p.name.toLowerCase()));
    farItems = (r.j?.results || []).filter((p) => !have.has(p.name.toLowerCase())); renderSugg(true);
  }, 450);
  else farItems = [];
}
function renderSugg(searched) {
  const all = [...suggItems, ...farItems];
  if (!all.length && !searched) { sugg.hidden = true; return; }
  const [la, lo] = searchNear();
  const item = (p, i) => {
    const d = p.seg && SEGS.get(p.seg) ? Math.round(depthAt(p.seg, S.t)) : null;
    const ic = p.recent ? ICON.clock : ICON[p.kind] || ICON.pin;
    const km = p.lat != null && p.kind !== "me" ? haversine(la, lo, p.lat, p.lon) / 1000 : null;
    const sub = [p.sub || (p.kind === "me" ? "GPS" : p.kind), km != null && km >= 1 ? `${km < 10 ? km.toFixed(1) : Math.round(km)} km` : ""].filter(Boolean).join(" · ");
    return `<button class="sg ${i === suggOn ? "on" : ""}" data-i="${i}" role="option"><span class="ic">${ic}</span><span class="n">${esc(p.name)}</span>
      <span class="d num" style="color:${d != null && d >= 6 ? rgb(waterRGB(d)) : "var(--faint)"}">${d != null ? (d >= 6 ? d + " cm" : "dry") : ""}</span>
      <span class="s">${esc(sub)}</span></button>`;
  };
  let html = "";
  if (suggItems.length) html += (searched ? `<div class="hd">Streets and places nearby</div>` : "") + suggItems.map(item).join("");
  if (farItems.length) html += `<div class="hd">Anywhere in India</div>` + farItems.map((p, i) => item(p, suggItems.length + i)).join("");
  if (!html) html = `<div class="hd">No matches yet</div>`;
  sugg.innerHTML = html; sugg.hidden = false;
  $$("[data-i]", sugg).forEach((b) => b.addEventListener("mousedown", (e) => { e.preventDefault(); pick(all[+b.dataset.i]); }));
}
qEl.addEventListener("focus", () => { showSuggestions(); if (!isDesk()) setSnap("peek"); });
qEl.addEventListener("input", () => { $("#q-clear").hidden = !qEl.value; showSuggestions(); });
qEl.addEventListener("keydown", (e) => {
  const all = [...suggItems, ...farItems];
  if (e.key === "ArrowDown") { suggOn = Math.min(suggOn + 1, all.length - 1); renderSugg(true); e.preventDefault(); }
  if (e.key === "ArrowUp") { suggOn = Math.max(suggOn - 1, 0); renderSugg(true); e.preventDefault(); }
  if (e.key === "Enter" && all.length) { pick(all[Math.max(suggOn, 0)]); e.preventDefault(); }
  if (e.key === "Escape") { sugg.hidden = true; qEl.blur(); }
});
qEl.addEventListener("blur", () => setTimeout(() => {
  sugg.hidden = true; qEl.placeholder = "Search streets and places";
  if (!qEl.value && S.searchFor !== "dest") S.searchFor = "dest";
  if (!isDesk() && SH.snap === "peek") setSnap("half");
}, 160));
$("#q-clear").onclick = () => { qEl.value = ""; $("#q-clear").hidden = true; if (S.view !== "home") back(); else { qEl.focus(); showSuggestions(); } };
function pick(p) {
  sugg.hidden = true; qEl.blur();
  if (!p) return;
  if (!p.recent && !["me", "home", "work"].includes(p.kind)) {
    prefs.recent = [{ name: p.name, sub: p.sub, kind: p.kind, lat: p.lat, lon: p.lon, seg: p.seg }, ...(prefs.recent || []).filter((r) => r.name !== p.name)].slice(0, 6);
    savePrefs();
  }
  if ((S.searchFor === "home" || S.searchFor === "work") && S.draft) {
    S.draft[S.searchFor] = { lat: p.lat, lon: p.lon, label: p.kind === "me" ? "Your location" : p.name };
    S.searchFor = "dest"; qEl.value = ""; render(); return;
  }
  if (S.searchFor === "from" && S.trip) {
    S.searchFor = "dest";
    S.trip = { ...S.trip, kind: "custom", from: [p.lat, p.lon], from_label: p.kind === "me" ? "Your location" : p.name };
    qEl.value = S.trip.to_label;
    return refreshPlan(true);
  }
  if (p.seg && p.kind === "street") return openPlace({ gid: p.seg, name: p.name, lat: p.lat, lon: p.lon });
  openDirectionsTo(p);
}
function notice(html, handlers = {}) {
  const n = $("#notice");
  n.innerHTML = html; n.hidden = false;
  $$("[data-n]", n).forEach((b) => b.addEventListener("click", () => { n.hidden = true; handlers[b.dataset.n]?.(); }));
}

/* ------------------------------------------------------------- location */
let geoWatch = null, firstFix = true;
function initLocation(userAsked) {
  if (!SECURE) return;
  if (!("geolocation" in navigator)) { if (userAsked) notice(`<div class="b">Location isn't available in this browser.</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`); return; }
  const ok = (pos) => {
    const { latitude: lat, longitude: lon, accuracy: acc } = pos.coords;
    S.me = { lat, lon, acc };
    $("#locate").classList.add("on");
    if (haversine(lat, lon, G.lat0, G.lon0) > 60000) setOrigin(lat, lon);
    if (firstFix || userAsked) {
      firstFix = false;
      centreOn(...proj(lat, lon), Math.max(V.k, 0.35));
      sendFocus(true); scheduleNearby(); loadSummary(); maybeReanchorDemo();
      if (S.view === "home") render();
    }
  };
  const bad = (err) => {
    if (!userAsked) return;
    notice(`<div class="b">${err.code === 1 ? "Location is blocked for this page. Allow it from the icon in the address bar, then tap the locate button again." : "Couldn't get your location. Try again in a moment."}</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`);
  };
  navigator.geolocation.getCurrentPosition(ok, bad, { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
  if (geoWatch == null) geoWatch = navigator.geolocation.watchPosition((p) => {
    S.me = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy };
  }, () => {}, { enableHighAccuracy: true, maximumAge: 30000 });
}
function maybeReanchorDemo() {
  // The demo storm is centred where the map was when it started. If you're somewhere else, move it to you.
  const a = S.cfg?.run?.anchor;
  if (S.sum?.mode !== "demo" || !a || !S.me || maybeReanchorDemo.done) return;
  if (haversine(a[0], a[1], S.me.lat, S.me.lon) > 8000) { maybeReanchorDemo.done = true; setRain("demo"); }
}
$("#locate").onclick = () => { unlockAudio(); if (!SECURE) return needsHttps("Your location"); if (S.me) centreOn(...proj(S.me.lat, S.me.lon), Math.max(V.k, 0.6)); else initLocation(true); };

/* ----------------------------------------------------------- user + menu */
const USER_COLOR = { priya: "#0a0a0a", ravi: "#0e7a5a", control: "#ff2d1a" };
function setUser(u, quiet) {
  S.user = u;
  const av = $("#avatar");
  av.textContent = S.cfg.users[u].label[0];
  av.className = `avatar ${u}`;
  S.ops = null; S.vuln = null;
  prefs.layers.sites = u === "control";
  syncChips();
  if (quiet) return;
  S.view = "home"; stack.length = 0; S.plan = null;
  qEl.value = ""; $("#q-clear").hidden = true;
  if (u === "priya") { refreshCommute(); scheduleNearby(); } else refreshOps();
  render(); setSnap("half");
}
let deferredInstall = null;
addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredInstall = e; });
function renderMenu() {
  const m = $("#menu"), notif = "Notification" in window ? Notification.permission : "unsupported";
  m.innerHTML = Object.entries(S.cfg.users).map(([id, u]) => `<button class="who" data-user="${id}" role="menuitem"><span class="av" style="background:${USER_COLOR[id]};color:${id === "priya" && T.dark ? "#000" : "#fff"};${id === "priya" && T.dark ? "background:#f5f5f2" : ""}">${esc(u.label[0])}</span><span>${esc(u.label)}</span><span class="ck">${id === S.user ? "✓" : ""}</span><small>${esc(u.desc || u.role)}</small></button>`).join("")
    + `<hr><div class="row"><span>Theme</span><span class="seg">${["auto", "light", "dark"].map((t) => `<button data-theme="${t}" class="${prefs.theme === t ? "on" : ""}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</span></div>
    <button class="row" data-m="notify"><span>Flood alerts on this device</span><span>${notif === "granted" ? "On" : notif === "denied" ? "Blocked in browser" : notif === "unsupported" ? "Not supported" : "Turn on"}</span></button>
    ${deferredInstall ? `<button class="row" data-m="install"><span>Install app</span><span>Opens offline</span></button>` : ""}
    <button class="row" data-m="phone"><span>Open on your phone</span><span>QR code</span></button>
    <button class="row" data-m="how"><span>How it works</span><span>D</span></button>`;
  $$("[data-user]", m).forEach((b) => b.addEventListener("click", () => { m.hidden = true; setUser(b.dataset.user); }));
  $$("[data-theme]", m).forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); prefs.theme = b.dataset.theme; savePrefs(); applyTheme(); renderMenu(); render(); }));
  $$("[data-m]", m).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const k = b.dataset.m;
    if (k === "how") { m.hidden = true; S.tab = "system"; toggleDrawer(true); }
    if (k === "phone") { m.hidden = true; showPhone(); }
    if (k === "install" && deferredInstall) { deferredInstall.prompt(); await deferredInstall.userChoice; deferredInstall = null; renderMenu(); }
    if (k === "notify") { await enableNotifications(); renderMenu(); }
  }));
}
$("#avatar").onclick = (e) => { e.stopPropagation(); const m = $("#menu"); m.hidden = !m.hidden; $("#avatar").setAttribute("aria-expanded", String(!m.hidden)); if (!m.hidden) renderMenu(); };
document.addEventListener("click", (e) => { if (!e.target.closest("#menu, #avatar")) $("#menu").hidden = true; });
function closeFloating() { $("#menu").hidden = true; sugg.hidden = true; }

/* chips */
function syncChips() {
  $$("[data-layer]").forEach((c) => {
    const on = !!prefs.layers[c.dataset.layer];
    c.classList.toggle("on", on); c.setAttribute("aria-pressed", String(on));
    if (c.dataset.layer === "sites") c.classList.toggle("locked", S.user !== "control");
  });
}
$$("[data-layer]").forEach((c) => c.addEventListener("click", () => {
  const L = c.dataset.layer;
  if (L === "sites" && S.user !== "control") {
    notice(`<div class="h">Restricted layer</div><div class="b">Which hospitals and schools are about to lose road access is shown to emergency control only, by the Cedar policy <b>emergency-vulnerable-sites</b>. Switch to Control room from the account menu.</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`);
    return;
  }
  prefs.layers[L] = !prefs.layers[L]; savePrefs(); syncChips();
  if (L === "sites" && prefs.layers.sites && !S.vuln) refreshOps();
}));

/* --------------------------------------------------------------- drawer */
function toggleDrawer(on) { S.drawer = on ?? !S.drawer; $("#drawer").hidden = !S.drawer; if (S.drawer) { renderDrawer(); pollStatus(); } }
$("#drawer-close").onclick = () => toggleDrawer(false);
$$("#tabs button").forEach((b) => b.addEventListener("click", () => { S.tab = b.dataset.tab; renderDrawer(); }));
const pre = (o) => `<pre>${esc(JSON.stringify(o, null, 2))}</pre>`;
function renderDrawer() {
  if (!S.drawer) return;
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  const body = $("#tab-body"), sc = body.scrollTop;
  body.innerHTML = S.tab === "why" ? whyTab() : S.tab === "ops" ? opsTab() : systemTab();
  body.scrollTop = sc;
  $$("[data-rain]", body).forEach((b) => b.addEventListener("click", async () => {
    $$("[data-rain]", body).forEach((x) => (x.disabled = true)); b.querySelector("i") ? (b.querySelector("i").textContent = "Starting…") : (b.textContent = "Switching…");
    await setRain(b.dataset.rain);
  }));
  $$("[data-act]", body).forEach((b) => b.addEventListener("click", () => act(b.dataset.act, b)));
}
function whyTab() {
  const a = S.advice;
  if (!a) return `<div class="empty">Open directions for a trip to see how the agent wrote its advice.</div>`;
  return `<div class="blk"><h5>Agent <small>${esc(a.engine)}</small></h5>
      <dl class="kv"><dt>Framework</dt><dd>Strands Agents SDK</dd><dt>Model</dt><dd>${esc(a.model.provider)} · ${esc(a.model.model)}${a.model.ready ? "" : ` (${esc(a.model.note || "not ready")})`}</dd><dt>Took</dt><dd class="num">${a.ms} ms</dd></dl>
      ${a.engine === "template" ? `<div class="hint">No model was reachable, so a template wrote the advice from the same tool results.</div>` : ""}</div>
    <div class="blk"><h5>Tool calls</h5>${a.trace.map((t) => `<div class="step"><span class="tn">${esc(t.tool)}(${esc(Object.entries(t.args).map(([k, v]) => `${k}=${v}`).join(", "))})<small>${t.ms} ms</small></span>${pre(t.result)}</div>`).join("")}
      <div class="step"><span class="tn">notification</span><div><b>${esc(a.headline)}</b><br>${esc(a.advice)}</div></div></div>`;
}
function opsTab() {
  if (S.user === "priya") return `<div class="blk"><h5>Ward operations <span class="cedar deny">denied</span></h5>
    <div class="empty">Pump and crew priorities are for ward officers and emergency control. Cedar denies a resident's request because no policy permits it.</div>
    ${pre({ principal: 'User::"priya"', action: 'Action::"viewOps"', resource: "Locality::*", decision: "Deny (no permit matched)" })}
    <div class="hint">Switch to Ravi or Control room from the account menu.</div></div>`;
  const O = S.ops;
  if (!O || O.forbidden) return `<div class="empty">Loading…</div>`;
  return `<div class="blk"><h5>Localities at ${esc(O.clock)} <small>OpenSearch aggregation</small></h5>
    <div class="list">${O.localities.map((l) => `<div class="li"><span class="n">${esc(l.locality)}</span><span class="d num">${l.impassable} · ${l.max_depth} cm</span><span class="s">${l.worst ? "Worst: " + esc(l.worst.name) : "No flooding"}</span></div>`).join("")}</div>
    <span class="cedar">${esc(O.decision.policies.join(", "))}</span>
    ${O.denied?.length ? `<div class="hint">Hidden by Cedar: ${O.denied.map(esc).join(", ")}</div>` : ""}</div>`;
}
function systemTab() {
  const s = S.status;
  if (!s) return `<div class="empty">Loading status…</div>`;
  const r = s.run, w = s.world, kind = r.kind, P = w.progress;
  const modeBtn = (src, title, sub) => `<button data-rain="${src}" class="${(src === "demo" ? kind === "demo" : src === r.source) ? "on" : ""}"><b>${title}</b><span>${sub}</span><i>${(src === "demo" ? kind === "demo" : src === r.source) ? "On" : ""}</i></button>`;
  const tilesGrid = (p) => `<div class="tiles-grid">${Array.from({ length: p.tiles }, (_, i) => `<i class="${i < p.forecast ? "fc" : i < p.ready ? "ready" : i < p.ready + p.empty ? "empty" : i < p.ready + p.empty + p.failed ? "failed" : ""}"></i>`).join("")}</div>`;
  return `<div class="blk"><h5>Rain <small>${esc(kind)}</small></h5>
      <div class="mode-card">${modeBtn("live", "Live", "Open-Meteo 15-minute forecast, refreshed every 15 min")}${modeBtn("demo", "Demo storm here", "An elevated storm on your real streets, centred on you. Only the rain is scripted.")}</div>
      <div class="hint">Replay a real Chennai storm (recorded hourly rain, ERA5) on today's streets:</div>
      <div class="replays">${Object.entries(r.replays || {}).map(([k, n]) => `<button data-rain="replay:${k}" class="${r.source === "replay:" + k ? "on" : ""}">${esc(n)}</button>`).join("")}</div>
      <dl class="kv"><dt>Now</dt><dd>${esc(r.label)}</dd><dt>Rain grid</dt><dd>${r.rain.points} Open-Meteo points on a ${r.rain.lattice_deg}° lattice${r.rain.error ? ` · <span style="color:var(--w2)">${esc(r.rain.error)}</span>` : ""}</dd>
      <dt>Reports</dt><dd>${r.resident_reports} from residents · ${r.documented} documented past closures matched</dd>
      <dt>Closures</dt><dd>${r.tomtom.enabled ? (r.tomtom.error ? `TomTom error: ${esc(r.tomtom.error)}` : `TomTom live: ${r.tomtom.closed_segments} segments closed`) : "TomTom off (set TOMTOM_API_KEY)"}; OSM construction and barriers; resident reports</dd></dl>
      <div class="btns"><button class="btn primary" data-act="tick">${kind === "live" ? "Refresh forecast now" : "Run nowcast update"}</button>${kind === "demo" ? `<button class="btn ghost" data-act="reset">Reset to ${esc(clockAt(0))}</button>` : ""}</div>
      <div class="hint">For the demo video, run <b>sam local invoke PollerFunction</b> instead (Lambda → SNS → SQS). This button delivers in-process.</div></div>
    <div class="blk"><h5>Map data <small>${w.synthetic ? "synthetic" : "OpenStreetMap + AWS terrain"}</small></h5>
      <dl class="kv"><dt>Every street</dt><dd>${P.detail.ready + P.detail.empty}/${P.detail.tiles} tiles within ${P.detail.km} km · ${P.detail.forecast} forecast</dd>
      <dt>Main roads</dt><dd>${P.region.ready + P.region.empty}/${P.region.tiles} tiles within ${P.region.km} km · ${P.region.forecast} forecast</dd>
      <dt>Loaded</dt><dd>${w.tiles_loaded} tiles · ${w.segments.toLocaleString()} street segments</dd>
      <dt>Jobs</dt><dd>${w.jobs.running} running · ${w.jobs.queued} queued · ${w.jobs.done} done${w.jobs.failed ? ` · ${w.jobs.failed} retrying` : ""}</dd></dl>
      ${tilesGrid(P.detail)}${tilesGrid(P.region)}
      <div class="log">${w.upstreams.map((u) => `<div class="lr ${u.state === "open" ? "bad" : ""}"><span class="t">${esc(u.state)}</span><span class="w">${esc(u.name)}</span><span class="m">${u.calls} calls · ${u.mb} MB${u.errors ? ` · ${u.errors} err` : ""}</span></div>`).join("")}</div></div>
    <div class="blk"><h5>OpenSearch <small>${s.store.engine === "opensearch" ? "connected" : "offline · in-memory"}</small></h5>
      ${s.store.warning ? `<div class="hint">${esc(s.store.warning)}</div>` : ""}
      <div class="log">${s.queries.map((q) => `<details><summary class="lr"><span class="t">${esc(q.at)}</span><span class="w">${esc(q.op)}</span><span class="m">${q.ms} ms · ${q.hits}</span></summary>${pre(q.query)}</details>`).join("")}</div></div>
    <div class="blk"><h5>Cedar <small>${s.policy.policies.length} policies</small></h5>
      <div class="log">${s.decisions.map((d) => `<details><summary class="lr ${d.allowed ? "" : "deny"}"><span class="t">${esc(d.at)}</span><span class="w">${esc(d.principal.split("::")[1].replace(/"/g, ""))} → ${esc(d.action.split("::")[1].replace(/"/g, ""))}</span><span class="m">${d.allowed ? "allow" : "deny"}</span></summary>${pre(d)}</details>`).join("")}</div></div>
    <div class="blk"><h5>Alerts <small>${s.alerts.localstack ? "LocalStack SNS → SQS" : "LocalStack offline"}</small></h5>
      <div class="log">${s.pipeline.map((p) => `<div class="lr"><span class="t">${esc(p.at)}</span><span class="w">${esc(p.step)}</span><span class="m">${esc(p.detail).slice(0, 36)}</span></div>`).join("") || `<div class="empty">No alert activity yet.</div>`}</div></div>
    <div class="blk"><h5>Agent <small>Strands</small></h5><dl class="kv"><dt>Provider</dt><dd>${esc(s.agent.provider)} · ${esc(s.agent.model)}</dd><dt>Status</dt><dd>${s.agent.ready ? "ready" : esc(s.agent.note)}</dd></dl></div>
    <div class="blk"><h5>This device</h5><dl class="kv"><dt>Offline cache</dt><dd>${"serviceWorker" in navigator && SECURE ? (navigator.serviceWorker.controller ? "active" : "starting") : "needs HTTPS"}</dd><dt>Tiles in memory</dt><dd>${[...TILES.values()].filter((t) => t.status === "ready").length}</dd><dt>Alerts</dt><dd>${"Notification" in window ? Notification.permission : "not supported"}</dd></dl></div>`;
}
async function systemAct(a, b) {
  b.disabled = true; b.textContent = a === "tick" ? "Updating…" : "Resetting…";
  if (a === "tick") {
    const ls = S.status?.alerts?.localstack;
    const r = await api("/api/internal/tick", { source: "ui", deliver: ls ? "return" : "direct" });
    if (r.ok && ls) for (const al of r.j.alerts) showToast({ ...al, via: "in-process (button)" });
  }
  if (a === "reset") { await api("/api/internal/reset", {}); S.fresh = false; }
  pollStatus();
}
let statusTimer = null;
async function pollStatus() {
  clearTimeout(statusTimer);
  const r = await api("/api/status");
  if (r.ok) { S.status = r.j; if (S.drawer && S.tab === "system") renderDrawer(); }
  statusTimer = setTimeout(pollStatus, S.drawer ? 3000 : 20000);
}
setInterval(async () => {  // background loading progress for the home sheet
  if (document.hidden || S.view !== "home") return;
  const [lat, lon] = S.me ? [S.me.lat, S.me.lon] : unproj(V.cx, V.cy);
  const r = await api(`/api/progress?lat=${lat}&lon=${lon}`);
  if (r.ok) { const was = JSON.stringify(S.progress); S.progress = r.j.progress; if (was !== JSON.stringify(S.progress)) maybeRefreshViews(); }
}, 4000);

/* --------------------------------------------------------------- events */
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = async (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "tiles") for (const k of ev.tiles) { const tl = TILES.get(k); if (tl && tl.status === "pending") { tl.status = "new"; tl.next = 0; } }
    if (ev.type === "forecast") {
      for (const k of ev.tiles) { const tl = TILES.get(k); if (tl && tl.status === "ready") { tl.fcWant = true; tl.fcNext = 0; } }
      if (S.sum && ev.run_id !== S.sum.run_id) await loadSummary();
      clearTimeout(connectEvents.cm); connectEvents.cm = setTimeout(() => { refreshCommute(); if (S.view === "dir" && !S.planWait) refreshPlan(false); if (S.view === "home") { if (S.user === "priya") scheduleNearby(); else refreshOps(); } }, 1200);
    }
    if (ev.type === "run") {
      S.fresh = (ev.label || "").includes("updated");
      const modeChanged = ev.mode && ev.mode !== S.sum?.mode;
      const w = await api("/api/world"); if (w.ok) S.cfg = w.j;
      await loadSummary();
      for (const tl of TILES.values()) if (tl.status === "ready") { tl.fcWant = true; tl.fcNext = 0; }
      if (modeChanged) { S.persona = null; S.commute = null; S.plan = null; stack.length = 0; S.view = "home"; S.t = 0; await loadPersona(); setUser("priya", true); }
      else refreshCommute();
      render(); pollStatus();
    }
    if (ev.type === "demo") { S.demo = { state: ev.state }; await loadPersona(); if (S.view === "home") render(); if (ev.state === "ready" && S.sum?.mode === "demo") { S.t = S.persona?.depart_min ?? 0; onTimeChange(); } }
    if (ev.type === "report") S.reports.push({ ...ev.report, ts: Date.now() / 1000 });
    if (ev.type === "closures") { await loadClosures(); if (S.view === "dir") refreshPlan(false); }
    if (ev.type === "alert") showToast(ev);
  };
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.cfg) { loadSummary(); refreshCommute(); for (const tl of TILES.values()) if (tl.status === "ready") tl.fcWant = true; } });
let toastTimer = null;
function showToast(ev) {
  const t = $("#toast");
  t.innerHTML = `<div class="ico"><span class="drop"></span></div><div class="app"><span>PAANI KAHAN</span><span>now</span></div>
    <div class="h">${esc(ev.headline)}</div><div class="b">${esc(ev.advice)}</div><div class="via">via ${esc(ev.via || "")}</div>`;
  t.hidden = false; t.style.animation = "none"; void t.offsetWidth; t.style.animation = "";
  chime();
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 12000);
  t.onclick = () => { t.hidden = true; if (S.user !== "priya") setUser("priya"); openCommute(); };
  if (document.hidden && "Notification" in window && Notification.permission === "granted") {
    navigator.serviceWorker?.ready.then((reg) => reg.showNotification(ev.headline, { body: ev.advice, icon: "/static/icons/icon-192.png", tag: "pk-commute", renotify: true }));
  }
}
let actx = null;
function unlockAudio() { try { if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)(); else if (actx.state === "suspended") actx.resume(); } catch { /* no audio */ } }
function chime() {
  if (!actx || actx.state !== "running") return;
  [880, 1320].forEach((f, i) => {
    const o = actx.createOscillator(), g = actx.createGain(), t0 = actx.currentTime + i * 0.12;
    o.type = "sine"; o.frequency.value = f; g.gain.setValueAtTime(0, t0); g.gain.linearRampToValueAtTime(0.08, t0 + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.5);
    o.connect(g).connect(actx.destination); o.start(t0); o.stop(t0 + 0.55);
  });
}

/* ------------------------------------------------------- open on phone */
async function showPhone() {
  const r = await api("/api/phone");
  const links = r.j?.links || [];
  const body = links.length
    ? links.map((L) => `<div class="h">${esc(L.label)}</div><div class="qr">${L.qr}</div><div class="b" style="word-break:break-all"><b>${esc(L.url)}</b></div>`).join("")
    : `<div class="h">No phone link yet</div><div class="b">For the HTTPS link (location, offline and install all work), run this on the laptop, then open this again:</div>
       <pre>finch compose --profile phone up -d</pre>
       <div class="b">For same Wi-Fi only, run <b>scripts\\phone-lan.ps1</b> as administrator (see README).</div>`;
  notice(`${body}<div class="btns"><button class="btn ghost" data-n="ok">Close</button></div>`);
}
const SECURE = window.isSecureContext;
function needsHttps(what) {
  notice(`<div class="h">${esc(what)} needs HTTPS</div><div class="b">You opened the app over a plain Wi-Fi address, and browsers only allow location, offline use and notifications on secure (https) pages. On the laptop, open the account menu → <b>Open on your phone</b> and use the phone link instead.</div><div class="btns"><button class="btn ghost" data-n="ok">OK</button></div>`);
}

/* --------------------------------------------- offline cache + background */
async function registerSW() {
  if (!("serviceWorker" in navigator) || !SECURE) return;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    savePrefs();
    reg.addEventListener("updatefound", () => {
      const nw = reg.installing;
      nw?.addEventListener("statechange", () => {
        if (nw.state === "installed" && navigator.serviceWorker.controller) {
          const sn = $("#snack");
          sn.innerHTML = `<span>A new version is ready.</span><button>Reload</button>`; sn.hidden = false;
          $("button", sn).onclick = () => { sn.dataset.reload = "1"; nw.postMessage("skip-waiting"); };
        }
      });
    });
    navigator.serviceWorker.addEventListener("controllerchange", () => { if ($("#snack").dataset.reload) location.reload(); });
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data?.type === "open-commute") openCommute(); });
    if (prefs.notify) registerPeriodic(reg);
  } catch { /* not available on this origin */ }
}
async function registerPeriodic(reg) {
  try {
    if (!("periodicSync" in reg)) return;
    const st = await navigator.permissions.query({ name: "periodic-background-sync" });
    if (st.state === "granted") await reg.periodicSync.register("pk-forecast", { minInterval: 15 * 60 * 1000 });
  } catch { /* unsupported */ }
}
async function enableNotifications() {
  if (!SECURE) return needsHttps("Flood alerts");
  if (!("Notification" in window)) return;
  const p = await Notification.requestPermission();
  prefs.notify = p === "granted"; savePrefs();
  if (prefs.notify) {
    const reg = await navigator.serviceWorker?.ready;
    if (reg) { registerPeriodic(reg); reg.showNotification("Flood alerts are on", { body: "We'll tell you when your route is about to flood.", icon: "/static/icons/icon-192.png", tag: "pk-hello" }); }
  }
}

/* ------------------------------------------------------------- keyboard */
addEventListener("keydown", (e) => {
  const typing = e.target.closest("input,textarea,select");
  if (e.key === "Escape") {
    if (!$("#menu").hidden) $("#menu").hidden = true;
    else if (S.drawer) toggleDrawer(false);
    else if (!$("#notice").hidden) $("#notice").hidden = true;
    else if (S.view !== "home" && !typing) back();
    return;
  }
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); qEl.focus(); }
  if (e.code === "Space" && !e.target.closest("button,a,[role=slider]")) { e.preventDefault(); setPlaying(!S.playing); }
  if (e.key === "d" || e.key === "D") toggleDrawer();
  if (e.key === "+" || e.key === "=") zoomAt(W / 2, H / 2, 1.6, true);
  if (e.key === "-") zoomAt(W / 2, H / 2, 1 / 1.6, true);
});

/* ---------------------------------------------------------------- boot */
// No boot screen: the map shows immediately and fills in as tiles arrive.
async function boot() {
  applyTheme();
  syncChips();
  const start = prefs.last || [12.979, 80.223];
  setOrigin(start[0], start[1]);
  centreOn(0, 0, 0.25, 0);   // the basemap shows right away, whatever the server is doing
  let w = await api("/api/world");
  while (!w.ok) { await new Promise((r) => setTimeout(r, 1500)); w = await api("/api/world"); }
  S.cfg = w.j;
  if (w.j.basemap !== undefined) BASE.cfg = w.j.basemap;
  if (BASE.cfg) $("#attrib").textContent = BASE.cfg.attribution;
  if (!prefs.last) { setOrigin(...S.cfg.origin); centreOn(0, 0, 0.25, 0); }
  centreOn(0, 0, 0.4, 1200);
  setUser("priya", true);
  render();
  sendFocus(true);
  await loadSummary();
  S.t = 0;
  await loadPersona();
  if (S.sum?.mode === "demo" && S.persona) S.t = S.persona.depart_min ?? 0;
  render();
  loadReports(); loadClosures();
  connectEvents();
  pollStatus();
  initLocation(false);
  scheduleNearby();
  registerSW();
  if (location.search.includes("commute")) openCommute();
}
resize();
requestAnimationFrame(frame);
boot();
