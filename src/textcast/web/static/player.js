/* The read-along player.
 *
 * Deliberately thin. Two things it does NOT do itself:
 *
 *   - Transport UI. media-chrome (MIT, vendored) owns play/pause, the seek
 *     bar, time display, playback rate, keyboard handling and ARIA.
 *   - Text-to-audio sync. Each section ships a WebVTT metadata track whose
 *     cue ids are block ids, so the browser fires `cuechange` as each block
 *     starts. No timing map to search, no drift to debug.
 *
 * What is left is the part no library provides: highlight the current block,
 * seek when one is tapped, roll on to the next section, and keep the lock
 * screen and the saved position up to date.
 */
(function () {
  "use strict";

  var cfg = window.TEXTCAST;
  var sections = JSON.parse(document.getElementById("payload").textContent).sections || [];
  if (!sections.length) return;

  var audio = document.getElementById("audio");
  var doc = document.getElementById("doc");
  var sheet = document.getElementById("sheet");
  var $ = function (id) { return document.getElementById(id); };

  var offsets = [];
  var totalMs = 0;
  sections.forEach(function (s) { offsets.push(totalMs); totalMs += s.ms; });

  /* Five seconds each way. Long enough to catch a missed clause, short enough
     that two presses is still less than a sentence. Kept in step with the
     seekoffset on the buttons in reader.html. */
  var SKIP_SECONDS = 5;

  var current = -1;
  var activeEl = null;
  var track = null;
  var follow = store("follow", "1") === "1";
  var sleepAtSectionEnd = false;

  function store(key, fallback, value) {
    try {
      if (arguments.length === 3) { localStorage.setItem("tc:" + key, value); return value; }
      return localStorage.getItem("tc:" + key) || fallback;
    } catch (e) { return fallback; }
  }

  function fmt(ms) {
    var t = Math.max(0, Math.round(ms / 1000));
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(s).padStart(2, "0");
  }

  function elapsed() { return offsets[current] + (audio.currentTime || 0) * 1000; }

  // ------------------------------------------------------- highlighting

  function highlight(blockId) {
    /* Called every frame while playing, so the common case — nothing has
       changed — costs a string compare and no DOM lookup at all. */
    if (activeEl && activeEl.id === blockId) return;
    var el = blockId ? document.getElementById(blockId) : null;
    if (el === activeEl) return;
    if (activeEl) activeEl.classList.remove("on");
    activeEl = el;
    if (!el) return;
    el.classList.add("on");
    if (follow) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  /* Which block covers this moment, from the timing map the page already
     carries. A binary search over contiguous cues: the last one that has
     started is the one being read. */
  function blockAt(ms) {
    var blocks = sections[current] && sections[current].blocks;
    if (!blocks || !blocks.length) return null;
    var lo = 0, hi = blocks.length - 1, found = null;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (blocks[mid][1] <= ms) { found = blocks[mid]; lo = mid + 1; }
      else { hi = mid - 1; }
    }
    return found ? found[0] : null;
  }

  function syncHighlight() {
    var id = blockAt((audio.currentTime || 0) * 1000);
    if (id) highlight(id);
  }

  /* Read the clock every frame while it is playing, rather than waiting to be
     told. `cuechange` only fires when the active cue *set* changes, and how
     promptly is the browser's business — Chromium is within about 10 ms,
     others are far looser, which shows up as a word or two of the next block
     being read before the highlight moves. Worse, a seek made by anything
     other than this file — the skip buttons and the scrub bar are
     media-chrome's, and they set currentTime themselves — changed no cue set
     at all, so the highlight stayed where it was until the next boundary. */
  var frame = null;

  function followClock() {
    syncHighlight();
    frame = requestAnimationFrame(followClock);
  }

  function stopFollowing() {
    if (frame !== null) { cancelAnimationFrame(frame); frame = null; }
    syncHighlight();
  }

  /* Kept as a backstop: a background tab stops running frames, and the audio
     keeps playing. */
  function onCueChange() {
    if (frame === null) syncHighlight();
  }

  // ------------------------------------------------------------ sections

  function loadSection(idx, atMs, autoplay) {
    if (idx < 0 || idx >= sections.length) return;
    var section = sections[idx];

    if (idx !== current) {
      current = idx;
      var base = "/media/" + encodeURIComponent(cfg.slug) + "/";

      if (track) track.removeEventListener("cuechange", onCueChange);
      var old = audio.querySelector("track");
      if (old) old.remove();

      audio.src = base + section.file;

      var el = document.createElement("track");
      el.kind = "metadata";
      el.default = true;
      el.src = base + section.track;
      el.addEventListener("load", function () {
        track = el.track;
        track.mode = "hidden";
        track.addEventListener("cuechange", onCueChange);
        syncHighlight();
      });
      audio.appendChild(el);
      audio.load();
    }

    if (atMs != null) seekWithin(atMs, autoplay);
    else if (autoplay) audio.play().catch(function () { /* needs a gesture */ });

    $("now-title").textContent = section.title || "";
    markChapters();
    updateMediaSession();
  }

  /* Pause, seek, then play again once the seek has landed.
     Setting currentTime on a playing element lets the already-buffered output
     finish first, so you hear a fragment of where you just were. Starting
     playback before a deferred seek has applied does the same from the top of
     the section.

     The resume checks where it ended up, because `seeked` is asynchronous: if
     something else moves the playhead first, that event is not ours and
     resuming on it would start playback somewhere nobody asked for. */
  function seekWithin(ms, autoplay) {
    var CLOSE_ENOUGH_MS = 250;

    var apply = function () {
      var target = Math.max(0, ms);
      var resume = autoplay || !audio.paused;
      if (!audio.paused) audio.pause();

      var landed = function () { return Math.abs(audio.currentTime * 1000 - target) < CLOSE_ENOUGH_MS; };

      /* `seeked` says the playhead moved, not that there is anything decoded
         to play from there. Start anyway and the clock runs while no sound
         comes out — on iOS that is the first word or two of the block, gone.
         HAVE_FUTURE_DATA is the readyState that means it can actually begin.
         The timeout is a backstop: never refuse to play because an event did
         not arrive. */
      var play = function () {
        var start = function () {
          audio.removeEventListener("canplay", start);
          clearTimeout(waiting);
          audio.play().catch(function () { /* needs a gesture */ });
        };
        if (audio.readyState >= 3) { start(); return; }
        var waiting = setTimeout(start, 1500);
        audio.addEventListener("canplay", start);
      };

      var onSeeked = function () {
        audio.removeEventListener("seeked", onSeeked);
        syncHighlight();
        if (resume && landed()) play();
      };

      if (landed()) {
        audio.currentTime = target / 1000;
        syncHighlight();
        if (resume) play();
        return;
      }
      audio.addEventListener("seeked", onSeeked);
      audio.currentTime = target / 1000;
    };

    if (audio.readyState >= 1) apply();
    else audio.addEventListener("loadedmetadata", apply, { once: true });
  }

  function goToAbsolute(ms, autoplay) {
    var idx = sections.length - 1;
    for (var i = 0; i < sections.length; i++) {
      if (ms < offsets[i] + sections[i].ms) { idx = i; break; }
    }
    loadSection(idx, ms - offsets[idx], autoplay);
  }

  function onEnded() {
    if (sleepAtSectionEnd) return;
    if (current + 1 < sections.length) loadSection(current + 1, 0, true);
    else savePosition(true, true);
  }

  // ------------------------------------------------------------ chapters

  function markChapters() {
    sheet.querySelectorAll(".chapter").forEach(function (btn, i) {
      btn.classList.toggle("on", i === current);
    });
  }

  function buildChapters() {
    var host = $("chapters");
    sections.forEach(function (s, i) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chapter";

      var label = document.createElement("span");
      label.className = "chapter-title";
      label.textContent = s.title || "Section " + (i + 1);

      var time = document.createElement("span");
      time.textContent = fmt(s.ms);

      btn.append(label, time);
      btn.addEventListener("click", function () {
        loadSection(i, 0, true);
        closeSheet();
      });
      host.appendChild(btn);
    });
  }

  function closeSheet() {
    sheet.hidden = true;
    $("menu").setAttribute("aria-expanded", "false");
  }

  // ------------------------------------------------------- saved position

  var lastSaved = 0;
  /* Set by "Stop and forget my place" and cleared the moment playback starts
     again. Pausing fires a save, and so does the timeupdate that follows, so
     without this the row is written straight back and the article never
     leaves "Continue listening". */
  var forgotten = false;

  /* Carried, not recomputed on every save. Only the end of the last section
     sets it and only pressing play clears it: an ordinary save used to send
     `false`, so opening a finished article and leaving without playing threw
     the completed badge away. */
  var finishedNow = !!(cfg.start && cfg.start.finished);

  function savePosition(force, finished) {
    if (current < 0 || forgotten) return;
    if (!force && Date.now() - lastSaved < 5000) return;
    lastSaved = Date.now();
    if (finished) finishedNow = true;

    var body = JSON.stringify({ section: current, ms: Math.round(elapsed()), finished: finishedNow });
    var url = "/api/articles/" + cfg.articleId + "/position";
    if (navigator.sendBeacon) navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    else fetch(url, { method: "POST", body: body, headers: { "Content-Type": "application/json" }, keepalive: true });
  }

  /* Stop, go back to the top of the article, and delete the saved position.
     The three go together: a position left behind would resume this article
     the next time it is opened, and would still list it as unfinished. */
  function stopAndForget() {
    forgotten = true;
    finishedNow = false;
    audio.pause();
    loadSection(0, 0, false);
    closeSheet();
    fetch("/api/articles/" + cfg.articleId + "/position/clear", { method: "POST" })
      .catch(function () { /* offline; press it again when there is a network */ });
  }

  // -------------------------------------------------------- media session
  // media-chrome does not touch the Media Session API, so the lock screen,
  // headphone buttons and chapter skips are wired here.

  function updateMediaSession() {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: cfg.title,
      artist: sections[current] ? sections[current].title : "",
      album: cfg.series || "textcast",
      artwork: [{ src: "/static/icon.svg", sizes: "any", type: "image/svg+xml" }]
    });
  }

  function wireMediaSession() {
    if (!("mediaSession" in navigator)) return;
    var handlers = {
      play: function () { audio.play(); },
      pause: function () { audio.pause(); },
      /* Same step as the buttons, so the lock screen and the page agree. */
      seekbackward: function (details) {
        audio.currentTime = Math.max(0, audio.currentTime - (details && details.seekOffset || SKIP_SECONDS));
      },
      seekforward: function (details) {
        audio.currentTime = Math.min(audio.duration || Infinity,
                                     audio.currentTime + (details && details.seekOffset || SKIP_SECONDS));
      },
      previoustrack: function () {
        if (current > 0) loadSection(current - 1, 0, !audio.paused);
        else audio.currentTime = 0;
      },
      nexttrack: function () {
        if (current + 1 < sections.length) loadSection(current + 1, 0, !audio.paused);
      }
    };
    Object.keys(handlers).forEach(function (name) {
      try { navigator.mediaSession.setActionHandler(name, handlers[name]); } catch (e) { /* unsupported */ }
    });
  }

  // -------------------------------------------------------------- offline

  function setOffline(on) {
    store("offline:" + cfg.slug, "", on ? "1" : "0");
    var worker = navigator.serviceWorker && navigator.serviceWorker.controller;
    if (!worker) return;
    var base = "/media/" + encodeURIComponent(cfg.slug) + "/";
    worker.postMessage({
      type: on ? "cache-article" : "drop-article",
      slug: cfg.slug,
      path: location.pathname,
      files: sections.reduce(function (all, s) { return all.concat(base + s.file, base + s.track); }, [])
    });
  }

  // --------------------------------------------------------------- wiring

  $("player").hidden = false;
  document.body.classList.add("has-player");

  buildChapters();
  wireMediaSession();
  audio.addEventListener("ended", onEnded);
  audio.addEventListener("play", function () {
    forgotten = false;
    finishedNow = false;   // listening again, so it is no longer finished
    if (frame === null) followClock();
  });
  audio.addEventListener("pause", function () { stopFollowing(); savePosition(true); });
  audio.addEventListener("ended", stopFollowing);
  /* Every seek, whoever made it. */
  audio.addEventListener("seeked", syncHighlight);
  audio.addEventListener("timeupdate", function () { savePosition(false); });

  $("prev").addEventListener("click", function () {
    if (audio.currentTime > 3 || current === 0) audio.currentTime = 0;
    else loadSection(current - 1, 0, !audio.paused);
  });
  $("next").addEventListener("click", function () {
    if (current + 1 < sections.length) loadSection(current + 1, 0, !audio.paused);
  });

  var followBtn = $("follow");
  followBtn.setAttribute("aria-pressed", follow ? "true" : "false");
  followBtn.addEventListener("click", function () {
    follow = !follow;
    followBtn.setAttribute("aria-pressed", follow ? "true" : "false");
    store("follow", "", follow ? "1" : "0");
    if (follow && activeEl) activeEl.scrollIntoView({ block: "center", behavior: "smooth" });
  });

  $("menu").addEventListener("click", function (event) {
    event.stopPropagation();
    sheet.hidden = !sheet.hidden;
    this.setAttribute("aria-expanded", sheet.hidden ? "false" : "true");
  });
  $("sheet-close").addEventListener("click", closeSheet);
  /* In the sheet, not in the control bar: it throws away where you were, and
     a mis-tap next to the play button would be too easy and cost too much. */
  $("stop").addEventListener("click", stopAndForget);
  /* Escape is not a key a phone has. Without one of these the sheet could
     only be left by reloading the page. */
  document.addEventListener("click", function (event) {
    if (!sheet.hidden && !sheet.contains(event.target)) closeSheet();
  });

  /* Seeking must never cost you a text selection.
     The gutter handle is the reliable way in. Clicking the text itself is
     opt-in, and even then a click that ends a selection is left alone. */

  /* A section with no audio is left out of the payload, so its position in
     this array is not the index the page marked the block with. */
  function positionOf(sectionIdx) {
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].idx === sectionIdx) return i;
    }
    return -1;
  }

  function seekToBlock(blockId, sectionIdx) {
    var at = positionOf(sectionIdx);
    var blocks = at >= 0 && sections[at].blocks;
    if (!blocks) return;
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i][0] === blockId) {
        loadSection(at, blocks[i][1], true);
        highlight(blockId);
        return;
      }
    }
  }

  /* Only the gutter handle seeks. Tapping the text itself was an option once
     and it fought with selecting a sentence; every block carries a play
     button, so there is nothing the tap did that the handle does not. */
  doc.addEventListener("click", function (event) {
    var handle = event.target.closest("[data-seek]");
    if (!handle) return;
    event.preventDefault();
    var owner = handle.closest(".b");
    seekToBlock(handle.dataset.seek, Number(owner.dataset.s));
  });

  $("opt-footnotes").addEventListener("change", function () {
    document.body.classList.toggle("hide-footnotes", !this.checked);
  });
  $("opt-summaries").addEventListener("change", function () {
    document.body.classList.toggle("hide-summaries", !this.checked);
  });
  $("opt-sleep").addEventListener("change", function () { sleepAtSectionEnd = this.checked; });

  var offlineBox = $("opt-offline");
  offlineBox.checked = store("offline:" + cfg.slug, "0") === "1";
  offlineBox.addEventListener("change", function () { setOffline(this.checked); });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeSheet();
  });
  addEventListener("pagehide", function () { savePosition(true); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) savePosition(true);
  });

  // Resume where this article was left. Saved positions are absolute across
  // the article, not per section.
  if (cfg.start && cfg.start.ms > 0) goToAbsolute(cfg.start.ms, false);
  else loadSection(0, 0, false);
})();
