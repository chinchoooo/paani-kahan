/* Paani Kahan service worker.
   - App shell (page, script, styles, icons, fonts): cache first, refreshed in the background.
   - Map tiles and forecasts (GET /api/tile/..., /api/summary, ...): network first with a short
     timeout, falling back to the last copy, so the app opens offline with the last forecast.
     Responses served from cache carry an `x-pk-cache` header so the page can say so.
   - Live streams and writes (/api/events, POST): never cached.
   - Background: on Periodic Background Sync (installed app, Chromium browsers) it refreshes
     the forecast and checks the saved commute; if it has flooded, it shows a notification. */
const V = "__V__";
const SHELL = `pk-shell-${V}`;
const DATA = "pk-data-v3";
const TERRAIN = "pk-terrain-v1";
const SHELL_URLS = ["/", "/static/app.js?v=" + V, "/static/app.css?v=" + V, "/manifest.webmanifest",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png"];
const DATA_GET = ["/api/tile/", "/api/world", "/api/summary", "/api/commute", "/api/progress", "/api/status", "/api/search", "/api/nearby", "/api/segment", "/api/closures", "/api/reports"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).catch(() => {}));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if ((k.startsWith("pk-shell-") && k !== SHELL) || k === "pk-data-v1") await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("message", (e) => {
  if (e.data === "skip-waiting") self.skipWaiting();
  if (e.data && e.data.type === "prefs") caches.open(DATA).then((c) => c.put("/__prefs", new Response(JSON.stringify(e.data.prefs))));
});

function timeout(ms) { return new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms)); }

async function networkFirst(req, ms) {
  const cache = await caches.open(DATA);
  try {
    const res = await Promise.race([fetch(req), timeout(ms)]);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch {
    const hit = await cache.match(req);
    if (!hit) return new Response(JSON.stringify({ offline: true }), { status: 503, headers: { "content-type": "application/json" } });
    const h = new Headers(hit.headers);
    h.set("x-pk-cache", hit.headers.get("date") || "1");
    return new Response(await hit.blob(), { status: hit.status, headers: h });
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(SHELL);
  const hit = await cache.match(req, { ignoreSearch: req.mode === "navigate" });
  const refresh = fetch(req).then((res) => { if (res.ok || res.type === "opaque") cache.put(req, res.clone()); return res; }).catch(() => null);
  return hit || (await refresh) || new Response("Offline", { status: 503 });
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname === "/api/events" || url.pathname.startsWith("/api/internal")) return;
  if (url.origin === location.origin && url.pathname.endsWith("/terrain.png")) {  // immutable per tile build
    e.respondWith(caches.open(TERRAIN).then(async (c) => (await c.match(req)) || fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; })));
    return;
  }
  if (url.origin === location.origin && DATA_GET.some((p) => url.pathname.startsWith(p))) {
    e.respondWith(networkFirst(req, url.pathname.startsWith("/api/tile/") ? 6000 : 3500));
    return;
  }
  if (req.mode === "navigate") { e.respondWith(networkFirst(req, 2500).then((r) => r.status === 503 ? cacheFirst(new Request("/")) : r)); return; }
  if (url.origin === location.origin || url.host.endsWith("fonts.googleapis.com") || url.host.endsWith("fonts.gstatic.com")) {
    e.respondWith(cacheFirst(req));
  }
});

// ---- background refresh (installed app on Chromium browsers) -------------------------
self.addEventListener("periodicsync", (e) => {
  if (e.tag === "pk-forecast") e.waitUntil(backgroundCheck());
});

async function backgroundCheck() {
  const cache = await caches.open(DATA);
  try {
    const fc = await fetch("/api/summary");
    if (fc.ok) await cache.put("/api/summary", fc.clone());
    const cm = await fetch("/api/commute?user=priya");
    const persona = cm.ok ? (await cm.json()).persona : null;
    if (!persona || persona.in_window === false) return;  // no commute saved, or it leaves after the 6-hour window
    const r = await fetch("/api/route", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ user: "priya", mode: persona.mode || "two_wheeler", depart_min: persona.depart_min }) });
    if (!r.ok) return;
    const plan = await r.json();
    const last = await cache.match("/__notified");
    const lastRun = last ? await last.text() : "";
    if (plan.status === "alert" && lastRun !== plan.run_id) {
      const w = plan.usual.worst;
      await self.registration.showNotification("Flooding on your route", {
        body: `${w.name}: ${w.depth} cm at ${w.clock || "your departure"}. ${plan.dry ? `Dry route ready (${plan.dry.minutes} min).` : "Consider waiting."}`,
        icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png", tag: "pk-commute", renotify: true,
      });
      await cache.put("/__notified", new Response(plan.run_id));
    }
  } catch { /* offline: try next time */ }
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (all.length) { all[0].focus(); all[0].postMessage({ type: "open-commute" }); }
    else await self.clients.openWindow("/?commute");
  })());
});
