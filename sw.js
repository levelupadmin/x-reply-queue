// Service Worker for X Reply Queue
// Caches the main HTML so the app loads instantly even offline (showing last-cached batch).
const VERSION = "v5-2026-07-02-v2ui";
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

  // Network-first for HTML + live data (metrics/learnings): always get the freshest
  // queue and growth data, fall back to cache offline.
  const isLiveData = url.pathname.endsWith("metrics.json") || url.pathname.endsWith("learnings.jsonl");
  if (event.request.mode === "navigate" || url.pathname.endsWith(".html") || url.pathname.endsWith("/") || isLiveData) {
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

// ---- Push notifications ----
self.addEventListener("push", (event) => {
  let data = { title: "X reply queue", body: "New replies ready", url: SCOPE };
  try { if (event.data) data = { ...data, ...event.data.json() }; } catch (e) {}
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: SCOPE + "icon-192.png",
    badge: SCOPE + "icon-192.png",
    data: { url: data.url || SCOPE },
    tag: "x-reply-batch"
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || SCOPE;
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) { if (w.url.includes(SCOPE) && "focus" in w) return w.focus(); }
    return clients.openWindow(target);
  }));
});
