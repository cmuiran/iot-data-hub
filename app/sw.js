// Service worker: the app shell works offline (network first, the cache when offline); never the API.
const CACHE = "app-shell-v2";
const SHELL = ["./", "index.html", "css/app.css", "js/main.js", "js/core.js", "js/views.js", "js/auto.js",
  "i18n/fa.json", "i18n/en.json", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin ||
      url.pathname.startsWith("/api/") || url.pathname.startsWith("/ingest/")) return;
  // network first so updates arrive; the cached shell when offline
  event.respondWith(fetch(event.request)
    .then((r) => {
      const copy = r.clone();
      if (r.ok) caches.open(CACHE).then((c) => c.put(event.request, copy));
      return r;
    })
    .catch(() => caches.match(event.request)));
});
