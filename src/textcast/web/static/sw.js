/* Service worker.
 *
 * Two jobs: keep the shell fast, and hold whole articles offline so a commute
 * with no signal still works. That is what AirDropping a WAV used to buy.
 */
/* Bump BUILD on any release that changes a static asset. The cache names
   carry it, so `install` re-runs and `activate` drops the old caches —
   otherwise a stale stylesheet survives every deploy. */
const BUILD = "0.2.0";
const SHELL = `textcast-shell-${BUILD}`;
const OFFLINE = `textcast-offline-${BUILD}`;

/* No query strings here: the pages decide their own cache-busting suffix, and
   a hardcoded one here would pin an old version forever. */
const SHELL_FILES = ["/static/app.css", "/static/player.js", "/static/tags.js", "/static/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== OFFLINE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  const { type, slug, path, files } = event.data || {};
  if (type === "cache-article") {
    event.waitUntil(
      caches.open(OFFLINE).then((cache) => cache.addAll([path, ...files]).catch(() => {}))
    );
  } else if (type === "drop-article") {
    event.waitUntil(
      caches.open(OFFLINE).then(async (cache) => {
        for (const request of await cache.keys()) {
          if (request.url.includes(encodeURIComponent(slug)) || request.url.endsWith(path)) {
            await cache.delete(request);
          }
        }
      })
    );
  }
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== location.origin) return;

  // Never cache the API: job state and positions must be live.
  if (url.pathname.startsWith("/api/")) return;

  // Audio and static assets are immutable — cache first.
  if (url.pathname.startsWith("/media/") || url.pathname.startsWith("/static/")) {
    // Ignore ?v= when matching, so a page asking for ?v=4 still hits a stored
    // ?v=3 entry rather than going to the network on every load.
    const options = url.pathname.startsWith("/static/") ? { ignoreSearch: true } : undefined;
    event.respondWith(
      caches.match(request, options).then((hit) => hit || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(url.pathname.startsWith("/media/") ? OFFLINE : SHELL).then((c) => c.put(request, copy));
        return response;
      }))
    );
    return;
  }

  // Pages: network first so the library is current, cache as the offline fallback.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && request.mode === "navigate") {
          const copy = response.clone();
          caches.open(OFFLINE).then((c) => c.match(request).then((existing) => existing && c.put(request, copy)));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});
