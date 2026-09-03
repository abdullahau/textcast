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
  /* media-chrome owns the transport: the play button, the scrub bar and the
     skip buttons. It was left to be picked up opportunistically on the first
     reader page, so installing the app and marking an article offline before
     ever opening one gave you an offline article with no controls. It is the
     point of the offline article, so it is precached with the rest. */
  "/static/vendor/media-chrome-4.19.2.js",
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

/* Turn a stored whole file into the 206 the media element asked for.
 *
 * A Blob, not an ArrayBuffer. `arrayBuffer()` reads the whole section into
 * memory to hand back a slice of it, and iOS asks for a range on every play,
 * pause and seek — so scrubbing a 22-minute section meant copying 4.6 MB per
 * drag. `blob.slice` is lazy: the browser keeps the body where it is and the
 * Response reads only the part that was asked for.
 */
async function sliceRange(response, range) {
  /* Parsed before the body is read, not after. Reading a Response disturbs
     it, so the two "give up and hand back what we were given" paths below
     were returning a Response nothing could read any more. */
  const match = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!match || (match[1] === "" && match[2] === "")) return response;

  const body = await response.blob();
  const total = body.size;
  let start = match[1] === "" ? null : parseInt(match[1], 10);
  let end = match[2] === "" ? null : parseInt(match[2], 10);
  if (start === null) {
    // "bytes=-500" is the *last* 500 bytes, not the first.
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

  /* Static assets are immutable *per version*, and the version is the `?v=`
     the page puts on them. So the match has to be exact.

     It used to pass `ignoreSearch: true`, which threw that suffix away — the
     whole point of the suffix — and answered a request for ?v=0.4.0 with
     whatever copy of app.css was already stored. Worse, the bare
     `caches.match` searches *every* cache, not this build's, so a previous
     build's stylesheet could answer too. A page then painted with CSS that
     predated its own markup: a figure with no rule to constrain it stretched
     to the picture's natural width, and the Sign out mark, which is drawn by
     `stroke: currentColor` from a rule that had not shipped yet, drew
     nothing. A hard refresh "fixed" both because a hard refresh goes round
     the worker entirely.

     Exact first, then the network — which is what a version that has never
     been seen before should do, once, and it is cached from then on.
     `ignoreSearch` survives only as the offline fallback: with no network,
     any stored copy of the file beats none. */
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request, { cacheName: SHELL }).then((exact) => exact || fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(SHELL).then((c) => c.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request, { ignoreSearch: true, cacheName: SHELL })))
    );
    return;
  }

  // Pages: network first so the library is current, cache as the offline fallback.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && request.mode === "navigate") {
          /* Cloned here, synchronously, and not inside the `match` below:
             the page starts reading `response` as soon as this returns, and
             a Response cannot be cloned once its body is being consumed.
             Only a page already held offline is refreshed — this is the copy
             the user asked to keep, not every page they happen to open. */
          const copy = response.clone();
          caches.open(OFFLINE).then((c) => c.match(request).then((existing) => existing && c.put(request, copy)));
        }
        return response;
      })
      .catch(async () => {
        /* `respondWith` rejects if it is handed undefined, and the browser
           then shows its own network error. `caches.match` resolves to
           undefined for anything never stored, so an offline visit to a page
           the user had not marked failed that way rather than saying so. */
        return (await caches.match(request))
          || (await caches.match("/"))
          || new Response(
            "<!doctype html><meta charset=utf-8>"
            + "<meta name=viewport content='width=device-width,initial-scale=1'>"
            + "<title>Offline</title>"
            + "<p style='font:1rem system-ui;margin:3rem auto;max-width:30rem;padding:0 1rem'>"
            + "You are offline, and this page is not one of the ones kept for offline reading. "
            + "Open an article you marked to keep, or try again once you have a signal.",
            { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
          );
      })
  );
});
