// Service Worker for X Reply Queue
// Caches the main HTML so the app loads instantly even offline (showing last-cached batch).
const VERSION = "v3-2026-05-17";
const CACHE = "x-reply-queue-" + VERSION;
const SCOPE = "/x-reply-queue/";
const CORE = [
  SCOPE,
  SCOPE + "index.html",
  SCOPE + "manifest.json",
  SCOPE + "icon-192.png",
  SCOPE + "icon-512.png",
  SCOPE + "apple-touch-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(CORE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith("x-reply-queue-") && k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (!url.pathname.startsWith(SCOPE)) return;

  // Network-first for HTML (always get the freshest queue, fall back to cache offline)
  if (event.request.mode === "navigate" || url.pathname.endsWith(".html") || url.pathname.endsWith("/")) {
    event.respondWith(
      fetch(event.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
        return res;
      }).catch(() => caches.match(event.request).then((c) => c || caches.match(SCOPE)))
    );
    return;
  }

  // Cache-first for everything else (icons, manifest)
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(event.request, copy));
      return res;
    }))
  );
});
