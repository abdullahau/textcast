/* Service worker.
 *
 * Two jobs: keep the shell fast, and hold whole articles offline so a commute
 * with no signal still works. That is what AirDropping a WAV used to buy.
 */
/* The cache names carry BUILD, so a new value makes `install` re-run and
   `activate` drop the old caches. The server rewrites this line with the
   package version as it serves the file: bumping it by hand was forgotten
   once, and a stale stylesheet survived the deploy. The literal below is only
   what you get if you open this file directly from /static/. */
const BUILD = "dev";
const SHELL = `textcast-shell-${BUILD}`;
const OFFLINE = `textcast-offline-${BUILD}`;

/* No query strings here: the pages decide their own cache-busting suffix, and
   a hardcoded one here would pin an old version forever. */
const SHELL_FILES = [
  "/static/app.css",
  "/static/player.js",
  "/static/progress.js",
  "/static/tags.js",
  "/static/icon.svg",
];

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

/* Audio, and the reason seeking was wrong on a phone.
 *
 * An <audio> element does not fetch a file; it asks for byte ranges, and on
 * iOS it does so for every play, pause and seek. `caches.match` ignores the
 * Range header — matching is by URL — so a request for bytes 500000-600000
 * was answered with the whole file and status 200. Measured: 2,437,998 bytes
 * returned for a 100,001-byte ask. The element then read the first byte it
 * got as the byte it asked for, so the clock and the audio parted company and
 * every seek landed a few blocks late.
 *
 * `cache.put` refuses a 206 as well, so the write silently rejected and
 * nothing ranged was ever stored.
 *
 * So: whole files go in the cache, and a ranged ask is served by slicing one.
 * Offline playback keeps working, and it can seek.
 */
async function mediaResponse(request) {
  const range = request.headers.get("range");
  const cache = await caches.open(OFFLINE);
  const hit = await cache.match(request);

  if (hit) return range ? sliceRange(hit, range) : hit;
  if (range) return fetch(request);  // a 206 cannot be stored, so do not try

  const response = await fetch(request);
  if (response.status === 200) cache.put(request, response.clone()).catch(() => {});
  return response;
}

/* Turn a stored whole file into the 206 the media element asked for. */
async function sliceRange(response, range) {
  const body = await response.arrayBuffer();
  const total = body.byteLength;
  const match = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!match) return response;

  let start = match[1] === "" ? null : parseInt(match[1], 10);
  let end = match[2] === "" ? null : parseInt(match[2], 10);
  if (start === null) {
    // "bytes=-500" is the *last* 500 bytes, not the first.
    if (end === null) return response;
    start = Math.max(0, total - end);
    end = total - 1;
  } else if (end === null || end >= total) {
    end = total - 1;
  }
  if (start > end || start >= total) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${total}` } });
  }

  return new Response(body.slice(start, end + 1), {
    status: 206,
    statusText: "Partial Content",
    headers: {
      "Content-Type": response.headers.get("Content-Type") || "application/octet-stream",
      "Content-Length": String(end - start + 1),
      "Content-Range": `bytes ${start}-${end}/${total}`,
      "Accept-Ranges": "bytes"
    }
  });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== location.origin) return;

  // Never cache the API: job state and positions must be live.
  if (url.pathname.startsWith("/api/")) return;

  // Audio is cache-first, but a media element asks for byte ranges, and the
  // Cache API does not know about them. See mediaResponse.
  if (url.pathname.startsWith("/media/")) {
    event.respondWith(mediaResponse(request));
    return;
  }

  // Static assets are immutable — cache first.
  if (url.pathname.startsWith("/static/")) {
    // Ignore ?v= when matching, so a page asking for ?v=4 still hits a stored
    // ?v=3 entry rather than going to the network on every load.
    event.respondWith(
      caches.match(request, { ignoreSearch: true }).then((hit) => hit || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(SHELL).then((c) => c.put(request, copy));
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
