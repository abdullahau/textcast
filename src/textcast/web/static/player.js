/* The read-along player.
 *
 * Deliberately thin. media-chrome (MIT, vendored) owns the transport: play,
 * pause, the seek bar, the time display, the rate, the keys and the ARIA.
 *
 * What is left is the part no library provides: highlight the block being
 * read, seek when one is tapped, roll on to the next section, and keep the
 * lock screen and the saved position up to date.
 *
 * The highlight follows the clock, not the WebVTT cues. See followClock.
 */
(function () {
  "use strict";

  var cfg = window.TEXTCAST;
  var sections = JSON.parse(document.getElementById("payload").textContent).sections || [];
  if (!sections.length) return;

  var audio = document.getElementById("audio");
  var doc = document.getElementById("doc");
  var sheet = document.getElementById("sheet");
  var header = document.querySelector("header.bar");
  var $ = function (id) { return document.getElementById(id); };

  var offsets = [];
  var totalMs = 0;
  sections.forEach(function (s) { offsets.push(totalMs); totalMs += s.ms; });

  /* Five seconds each way. Long enough to catch a missed clause, short enough
     that two presses is still less than a sentence. Kept in step with the
     seekoffset on the buttons in reader.html. */
  var SKIP_SECONDS = 5;

  /* How far the highlight is held back, in milliseconds.
     `audio.currentTime` says where the decoder is, not where the speaker is.
     Everything after it is delay: the output buffer, and over Bluetooth the
     codec and the radio as well. About 25 ms on a laptop, which nobody sees,
     and 150-300 ms over Bluetooth, which is a clause — the whole of why the
     read-along looked right on a desktop and ran ahead of the voice on a
     phone.

     The browser will say. `AudioContext.outputLatency` is the *output
     device's* latency, not any graph's, so a context with nothing connected
     to it reports the number and the audio element keeps its own path to the
     speaker. See measureLatency: it reads 0 until the device's stream is
     open, so it is asked after playback starts and again when the device
     changes.

     `trimMs` is what is left after that — a browser that does not implement
     the property, or a pair of earbuds that lies about itself. It is zero
     for almost everybody and the slider says so. */
  var detectedMs = 0;
  var trimMs = parseInt(store("sync-offset", "0"), 10) || 0;

  function offsetMs() { return detectedMs + trimMs; }

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

  var pauseAtVisual = store("pause-visual", "0") === "1";
  var pausedAt = null;

  function highlight(blockId) {
    /* Called every frame while playing, so the common case — nothing has
       changed — costs a string compare and no DOM lookup at all. */
    if (activeEl && activeEl.id === blockId) return;
    if (pausedAt && pausedAt !== blockId) pausedAt = null;
    var el = blockId ? document.getElementById(blockId) : null;
    if (el === activeEl) return;
    if (activeEl) activeEl.classList.remove("on");
    activeEl = el;
    if (!el) return;
    el.classList.add("on");
    keepInView(true);
    stopToLook(el);
  }

  /* Stop at a chart or a table, so it can be looked at rather than talked
     over. Only while playing: seeking *to* a figure is already a decision to
     look at it, and pausing there would fight the person who asked. The id is
     remembered so pressing play again carries on past it instead of stopping
     on the same block for ever. */
  function stopToLook(el) {
    if (!pauseAtVisual || !el.dataset.visual) return;
    if (audio.paused || pausedAt === el.id) return;
    pausedAt = el.id;
    audio.pause();
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
    var id = blockAt((audio.currentTime || 0) * 1000 - offsetMs());
    if (id) highlight(id);
  }

  /* Ask the browser what the output device costs.
   *
   * The context is opened, polled until it admits a number, and closed again.
   * Held open it would keep a second output stream alive for the whole
   * article — on iOS that means owning the audio session, which is not
   * something a read-along should take from the element that is playing.
   *
   * Reading 0 is the ordinary state before the stream is up, not an answer,
   * so a zero is waited out rather than believed. Chrome 102, Firefox 70 and
   * Safari 18.4 implement it; anything older falls through to the trim. */
  var measuring = false;
  var asked = false;

  function measureLatency() {
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) { asked = true; showOffset(); return; }
    if (measuring) return;
    measuring = true;

    var ctx;
    try {
      ctx = new Ctor();
    } catch (e) {
      measuring = false;
      asked = true;
      showOffset();
      return;
    }
    if (ctx.state === "suspended") ctx.resume().catch(function () {});

    var tries = 0;
    var done = function () {
      measuring = false;
      try { ctx.close(); } catch (e) { /* already closed */ }
    };

    var look = function () {
      var seconds = ctx.outputLatency;
      var ok = typeof seconds === "number" && isFinite(seconds) && seconds > 0
               // A second and a half is not a headphone, it is a bug.
               && seconds < 1.5;
      if (ok) {
        var ms = Math.round(seconds * 1000);
        asked = true;
        if (ms !== detectedMs) { detectedMs = ms; syncHighlight(); }
        showOffset();
        done();
        return;
      }
      if (++tries > 12) {                     // ~1.2 s, then give up quietly
        asked = true;
        showOffset();
        done();
        return;
      }
      setTimeout(look, 100);
    };
    setTimeout(look, 100);
  }

  // --------------------------------------------------------------- follow

  /* Keeping the block on screen, which is not the same as scrolling to it.
     `scrollIntoView({block: "center"})` centres in the layout viewport, and
     on a phone that is not what you can see: the header covers the top, the
     player covers the bottom, and the URL bar slides in and out under both.
     It also scrolled on every block whatever the page was doing, so a
     thumb-scroll to look ahead was undone by the next paragraph, and one
     smooth scroll across a long article ran for seconds after a seek.

     So: measure the band that is actually visible, do nothing at all while
     the block is inside it, and let a hand on the page win for a few
     seconds. */
  var HOLD_MS = 4000;   // how long a hand-scroll owns the page
  var NUDGE_MS = 800;   // never issue a second scroll inside this
  var CHECK_MS = 250;   // how often the frame loop bothers to look
  var EDGE = 12;        // breathing room against either bar
  var heldUntil = 0;
  var nudgedAt = 0;
  var checkedAt = 0;

  function band() {
    /* visualViewport, not innerHeight: on a phone they differ by the height
       of the URL bar, and by the on-screen keyboard when one is open. */
    var height = (window.visualViewport && window.visualViewport.height) || innerHeight;
    var player = $("player");
    return {
      top: (header ? Math.max(0, header.getBoundingClientRect().bottom) : 0) + EDGE,
      bottom: height - (player && !player.hidden ? player.getBoundingClientRect().height : 0) - EDGE
    };
  }

  function keepInView(smooth, force) {
    if (!follow || !activeEl) return;
    var now = Date.now();
    if (!force && (now < heldUntil || now - nudgedAt < NUDGE_MS)) return;

    var view = band();
    var height = view.bottom - view.top;
    if (height <= 0) return;
    var box = activeEl.getBoundingClientRect();
    // Readable where it is. Leaving the page alone is the point.
    if (box.top >= view.top && box.bottom <= view.bottom) return;

    /* A third of the way down the band, not centred: what has not been read
       yet is what you want to see. A block taller than the band starts at
       the top instead, because its first line is the one being read. */
    var wanted = box.height >= height ? view.top : view.top + (height - box.height) / 3;
    var delta = box.top - wanted;
    if (Math.abs(delta) < 4) return;

    nudgedAt = now;
    /* Smooth over a paragraph, instant over an article. A smooth scroll of
       several thousand pixels runs for seconds on a phone, and the highlight
       is wrong for every one of them. */
    scrollBy({ top: delta, behavior: smooth && Math.abs(delta) < 2000 ? "smooth" : "auto" });
  }

  function maybeKeepInView() {
    var now = Date.now();
    if (now - checkedAt < CHECK_MS) return;
    checkedAt = now;
    keepInView(true);
  }

  /* A hand on the page wins for HOLD_MS. `touchmove` and `wheel`, not
     `touchstart`: a tap is not a scroll, and tapping a block to play from it
     should still bring the highlight back. */
  function handOnPage() { heldUntil = Date.now() + HOLD_MS; }
  addEventListener("wheel", handOnPage, { passive: true });
  addEventListener("touchmove", handOnPage, { passive: true });

  /* Read the clock every frame rather than waiting to be told. `cuechange`
     fires when the browser gets round to it — 10 ms in Chromium, far looser
     elsewhere — and a seek made by media-chrome's own scrub bar changes no
     cue set at all, so the highlight sat still until the next boundary. */
  var frame = null;

  function followClock() {
    syncHighlight();
    /* Not only when the block changes. A block can run for a minute, so a
       scroll away from it used to leave the reader lost until the next one. */
    maybeKeepInView();
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

  /* Pause, seek, then play once the seek has landed. Setting currentTime on
     a playing element lets the buffered output finish first, so you hear a
     fragment of where you just were. The resume checks where it ended up:
     `seeked` is asynchronous, and an armed listener fires on whatever seek
     happens next, including one the user made. */
  function seekWithin(ms, autoplay) {
    var CLOSE_ENOUGH_MS = 250;

    var apply = function () {
      var target = Math.max(0, ms);
      var resume = autoplay || !audio.paused;
      if (!audio.paused) audio.pause();

      var landed = function () { return Math.abs(audio.currentTime * 1000 - target) < CLOSE_ENOUGH_MS; };

      /* `seeked` means the playhead moved, not that anything is decoded
         there: playing on it runs the clock in silence, and on iOS that is
         the first word or two gone. readyState 3 is HAVE_FUTURE_DATA. The
         timeout is a backstop — never refuse to play over a missing event. */
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
  /* Set by "Stop and forget my place", cleared when playback starts again.
     That button pauses and seeks, and both write the row, so without this the
     position comes straight back. */
  var forgotten = false;

  /* Carried, not recomputed. Only the end of the last section sets it and
     only play clears it: an ordinary save used to send `false`, so opening a
     finished article and leaving threw the completed badge away. */
  var finishedNow = !!(cfg.start && cfg.start.finished);

  function savePosition(force, finished) {
    if (current < 0 || forgotten) return;
    if (!force && Date.now() - lastSaved < 5000) return;
    lastSaved = Date.now();
    if (finished) finishedNow = true;

    var body = JSON.stringify({ section: current, ms: Math.round(elapsed()), finished: finishedNow });
    var url = "/api/articles/" + cfg.articleId + "/position";
    if (navigator.sendBeacon) navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    else {
      fetch(url, { method: "POST", body: body, headers: { "Content-Type": "application/json" }, keepalive: true })
        .catch(function () { /* offline; the next save tries again */ });
    }
  }

  /* Stop, go back to the top, and delete the saved position. The three go
     together: a row left behind would resume the article and still list it
     as unfinished. */
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
      /* PNG, not the SVG. Android draws this on the lock screen and in the
         notification shade, and neither renders an SVG — the notification
         came up with a blank square where the icon goes. */
      artwork: [
        { src: "/static/icon-192.png", sizes: "192x192", type: "image/png" },
        { src: "/static/icon-512.png", sizes: "512x512", type: "image/png" }
      ]
    });
  }

  function wireMediaSession() {
    if (!("mediaSession" in navigator)) return;
    var handlers = {
      play: function () { audio.play().catch(function () { /* needs a gesture */ }); },
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

  /* Ask the worker something and wait for the answer.
     `postMessage` on its own is a shout into a room: the download either
     happened or it did not and the page had no way to know, so a failed
     `addAll` left the box ticked over a cache holding nothing. */
  function tell(message) {
    return new Promise(function (resolve) {
      var worker = navigator.serviceWorker && navigator.serviceWorker.controller;
      if (!worker || !window.MessageChannel) { resolve(null); return; }
      var channel = new MessageChannel();
      var settled = false;
      var finish = function (value) { if (!settled) { settled = true; resolve(value); } };
      channel.port1.onmessage = function (event) { finish(event.data); };
      // A worker being replaced never answers. Do not wait on it for ever.
      setTimeout(function () { finish(null); }, 20000);
      worker.postMessage(message, [channel.port2]);
    });
  }

  function readable(bytes) {
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
    return (bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0) + " MB";
  }

  function showKept() {
    tell({ type: "usage" }).then(function (answer) {
      if (!answer || !answer.bytes) { offlineNote.textContent = ""; return; }
      var mine = answer.bytes[cfg.slug] || 0;
      var total = Object.keys(answer.bytes).reduce(
        function (sum, slug) { return sum + answer.bytes[slug]; }, 0);
      var others = Object.keys(answer.bytes).length - (mine ? 1 : 0);
      offlineNote.textContent = mine
        ? "This article takes " + readable(mine) + " on this device"
          + (others > 0 ? ", " + readable(total) + " across " + (others + 1) + " articles." : ".")
        : (total ? readable(total) + " kept on this device." : "");
    });
  }

  function setOffline(on) {
    store("offline:" + cfg.slug, "", on ? "1" : "0");
    var base = "/media/" + encodeURIComponent(cfg.slug) + "/";
    var files = sections.reduce(
      function (all, s) { return all.concat(base + s.file, base + s.track); }, []);

    offlineNote.textContent = on ? "Downloading…" : "Removing…";
    tell({
      type: on ? "cache-article" : "drop-article",
      slug: cfg.slug,
      path: location.pathname,
      files: files
    }).then(function (answer) {
      if (on && answer && answer.ok === false) {
        // Say so, and untick it. A ticked box over an empty cache is a
        // promise the commute finds out about.
        store("offline:" + cfg.slug, "", "0");
        offlineBox.checked = false;
        offlineNote.textContent = "The download did not finish — try again on a better connection.";
        return;
      }
      showKept();
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
    /* Here and not at load: the property reads 0 until the output stream is
       open, and pressing play is what opens it. It is also the gesture iOS
       wants before it will let an AudioContext run at all. */
    measureLatency();
  });

  /* Headphones plugged in halfway through an article change the answer by
     200 ms, and a reload is not a reasonable thing to ask for. */
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", function () {
      if (!audio.paused) measureLatency();
    });
  }
  audio.addEventListener("pause", function () { stopFollowing(); savePosition(true); });
  audio.addEventListener("ended", stopFollowing);
  /* Every seek, whoever made it. */
  audio.addEventListener("seeked", syncHighlight);
  audio.addEventListener("timeupdate", function () {
    savePosition(false);
    // A hidden tab runs no frames and the audio keeps playing. timeupdate
    // still fires, coarsely, and coarse beats frozen.
    if (document.hidden) syncHighlight();
  });

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
    // Pressed on purpose, so it overrides a hand-scroll rather than waiting
    // the four seconds out.
    if (follow) keepInView(true, true);
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

  /* Only the gutter handle seeks. Tapping the text fought with selecting a
     sentence, and every block already carries a play button. */
  doc.addEventListener("click", function (event) {
    var handle = event.target.closest("[data-seek]");
    if (!handle) return;
    event.preventDefault();
    /* Or the handle keeps focus, and the next Space press fires it again
       instead of pausing. The document handler below covers the rest. */
    handle.blur();
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

  var visualBox = $("opt-pause-visual");
  visualBox.checked = pauseAtVisual;
  visualBox.addEventListener("change", function () {
    pauseAtVisual = this.checked;
    /* Three arguments, and the value is the third. Passing `true` there wrote
       the string "true", which the read above compares against "1" — so the
       setting was stored and never came back, and the box was empty on every
       reload. The two other stores here had it right. */
    store("pause-visual", "", pauseAtVisual ? "1" : "0");
  });


  /* The controls lock.
     Listening with the phone in a hand doing something else lands taps on
     whatever is under a thumb, and the scrub bar spans the screen — so the
     audio jumped to a random place. Locked, everything in the bar but the
     padlock ignores pointers, and so do the play handles beside each block.

     Tap to lock; hold to unlock. A tap would undo it, and a stray tap is the
     thing being guarded against. Keys are left alone: this is a lock against
     a thumb, not against a keyboard. */
  var lockBtn = $("lock");
  var locked = false;
  var holdTimer = null;
  var unlockedByHold = false;
  var HOLD_TO_UNLOCK_MS = 550;

  function setLocked(on) {
    locked = on;
    $("player").classList.toggle("locked", on);
    document.body.classList.toggle("locked", on);
    lockBtn.setAttribute("aria-pressed", on ? "true" : "false");
    lockBtn.setAttribute("aria-label", on ? "Hold to unlock the controls" : "Lock the controls");
    lockBtn.title = on ? "Hold to unlock" : "Lock the controls";
    store("locked:" + cfg.slug, "", on ? "1" : "0");
    if (!on && hint && !hint.hidden && hintText.textContent !== "Unlocked") hideHint(0);
  }

  /* What a hold looks like while it is happening.
     Holding for an unmarked length of time is a guess, and a guess that has
     to be repeated is worse than a second tap. The bar fills over exactly the
     time the timer below waits, because the script writes both. */
  var hint = $("lock-hint");
  var hintText = $("lock-hint-text");
  var fill = $("lock-fill");
  var hintTimer = null;

  function showHint(text, filling) {
    clearTimeout(hintTimer);
    hintText.textContent = text;
    hint.hidden = false;
    fill.classList.remove("filling");
    fill.style.transition = "none";
    // Forced, so the width really goes back to 0 before the fill starts.
    void fill.offsetWidth;
    if (filling) {
      fill.style.transition = "width " + HOLD_TO_UNLOCK_MS + "ms linear";
      fill.classList.add("filling");
    }
  }

  function hideHint(after) {
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () {
      hint.hidden = true;
      fill.classList.remove("filling");
    }, after || 0);
  }

  lockBtn.addEventListener("pointerdown", function (event) {
    if (!locked) return;
    /* Capture the pointer, so a thumb that drifts a few pixels off a 2 rem
       button does not silently cancel the hold. That, and not the length of
       the hold, is why it took several goes. */
    try { lockBtn.setPointerCapture(event.pointerId); } catch (e) { /* mouse */ }
    showHint("Keep holding to unlock", true);
    holdTimer = setTimeout(function () {
      holdTimer = null;
      unlockedByHold = true;
      setLocked(false);
      showHint("Unlocked", false);
      hideHint(900);
      // Says the hold was long enough, without a sound in the reader's ear.
      if (navigator.vibrate) navigator.vibrate(15);
    }, HOLD_TO_UNLOCK_MS);
  });
  // No `pointerleave`: the capture above means the release comes back here
  // wherever the thumb ended up, and leaving is no longer a cancellation.
  ["pointerup", "pointercancel"].forEach(function (name) {
    lockBtn.addEventListener(name, function (event) {
      try { lockBtn.releasePointerCapture(event.pointerId); } catch (e) { /* mouse */ }
      if (!holdTimer) return;
      clearTimeout(holdTimer);
      holdTimer = null;
      // Let go early: say what went wrong rather than simply doing nothing.
      showHint("Hold the padlock a moment longer", false);
      hideHint(1600);
    });
  });

  /* A tap on a dead control falls through to the bar itself, because the
     controls are `pointer-events: none` and the bar is not. Saying why
     nothing happened is the difference between a lock and a broken player. */
  $("player").addEventListener("pointerdown", function (event) {
    if (!locked || lockBtn.contains(event.target)) return;
    showHint("Locked — hold the padlock to unlock", false);
    hideHint(1600);
  });
  lockBtn.addEventListener("click", function () {
    // A hold ends in a click too, and that click must not lock it again.
    if (unlockedByHold) { unlockedByHold = false; return; }
    if (!locked) setLocked(true);
  });
  setLocked(store("locked:" + cfg.slug, "0") === "1");

  // ------------------------------------------------------ highlight timing

  var syncBox = $("opt-sync");
  var syncOut = $("sync-value");
  var syncSaid = $("sync-detected");

  function showOffset() {
    syncOut.textContent = (trimMs > 0 ? "+" : "") + trimMs + " ms";
    /* Three states, not two. Before the first play nothing has been asked,
       and saying "this browser will not say" then is simply wrong — it reads
       as a missing feature rather than as a measurement not yet taken. */
    syncSaid.textContent = detectedMs
      ? "This device reports " + detectedMs + " ms of output delay, and the "
        + "highlight is already held back by that much."
      : (asked
         ? "This browser will not say what the output delay is, so set it by ear."
         : "Measured from the output device the moment you press play.");
  }

  syncBox.value = String(trimMs);
  showOffset();
  syncBox.addEventListener("input", function () {
    trimMs = parseInt(this.value, 10) || 0;
    showOffset();
    store("sync-offset", "", String(trimMs));
    syncHighlight();
  });

  var offlineBox = $("opt-offline");
  var offlineNote = $("offline-size");
  offlineBox.checked = store("offline:" + cfg.slug, "0") === "1";
  offlineBox.addEventListener("change", function () { setOffline(this.checked); });
  // What it costs, once, when the sheet is first opened rather than on load:
  // it wakes the worker and walks the cache.
  $("menu").addEventListener("click", function once() {
    $("menu").removeEventListener("click", once);
    showKept();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeSheet();
  });

  /* Space plays and pauses, wherever the page's focus happens to be.
     media-chrome binds its own keys, but only while something inside the
     media-controller has focus, so in practice Space did one of two wrong
     things: pressed again whichever block play button was last clicked, or
     scrolled the page. Both are worse than the obvious behaviour.

     Capture and preventDefault, so a focused button never sees the key at
     all — a <button> fires its click on keyup, and cancelling the keydown is
     what stops it. Text fields and the sheet keep Space: typing a space and
     ticking a checkbox are what the key is for there. */
  function typing(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  document.addEventListener("keydown", function (event) {
    if (event.code !== "Space" && event.key !== " " && event.key !== "Spacebar") return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (typing(event.target) || sheet.contains(event.target)) return;
    event.preventDefault();
    if (audio.paused) audio.play().catch(function () { /* needs a gesture */ });
    else audio.pause();
  }, true);
  addEventListener("pagehide", function () { savePosition(true); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { savePosition(true); return; }
    /* Coming back from a locked screen. Put the highlight where the audio
       actually is before the reader has time to read the wrong paragraph,
       and override any hand-scroll from before the screen went off. */
    syncHighlight();
    keepInView(false, true);
    if (!audio.paused && frame === null) followClock();
    // The earbuds went in while the screen was off, more often than not.
    if (!audio.paused) measureLatency();
  });

  // Resume where this article was left. Saved positions are absolute across
  // the article, not per section.
  if (cfg.start && cfg.start.ms > 0) goToAbsolute(cfg.start.ms, false);
  else loadSection(0, 0, false);
})();
