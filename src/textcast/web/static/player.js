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
    var el = blockId ? document.getElementById(blockId) : null;
    if (el === activeEl) return;
    if (activeEl) activeEl.classList.remove("on");
    activeEl = el;
    if (!el) return;
    el.classList.add("on");
    if (follow) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  /* The browser tells us which block is playing; we only have to react. */
  function onCueChange() {
    var active = this.activeCues;
    if (active && active.length) highlight(active[0].id);
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
      });
      audio.appendChild(el);
      audio.load();
    }

    if (atMs != null) seekWithin(atMs);
    if (autoplay) audio.play().catch(function () { /* needs a gesture */ });

    $("now-title").textContent = section.title || "";
    markChapters();
    updateMediaSession();
  }

  function seekWithin(ms) {
    var apply = function () { audio.currentTime = Math.max(0, ms) / 1000; };
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
  function savePosition(force, finished) {
    if (current < 0) return;
    if (!force && Date.now() - lastSaved < 5000) return;
    lastSaved = Date.now();

    var body = JSON.stringify({ section: current, ms: Math.round(elapsed()), finished: !!finished });
    var url = "/api/articles/" + cfg.articleId + "/position";
    if (navigator.sendBeacon) navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    else fetch(url, { method: "POST", body: body, headers: { "Content-Type": "application/json" }, keepalive: true });
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
      seekbackward: function () { audio.currentTime = Math.max(0, audio.currentTime - 15); },
      seekforward: function () { audio.currentTime += 30; },
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
  audio.addEventListener("pause", function () { savePosition(true); });
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

  $("menu").addEventListener("click", function () {
    sheet.hidden = !sheet.hidden;
    this.setAttribute("aria-expanded", sheet.hidden ? "false" : "true");
  });

  /* Tap any paragraph to hear it — the scrubbing a rendered video cannot do. */
  doc.addEventListener("click", function (event) {
    var el = event.target.closest(".b");
    if (!el) return;
    var idx = Number(el.dataset.s);
    var blocks = sections[idx] && sections[idx].blocks;
    if (!blocks) return;
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i][0] === el.dataset.b) {
        loadSection(idx, blocks[i][1], true);
        highlight(el.dataset.b);
        return;
      }
    }
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
