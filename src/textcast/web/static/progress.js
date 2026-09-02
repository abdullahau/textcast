/* Poll the build queue while something is rendering, and reload when it lands. */
(function () {
  "use strict";
  var tries = 0;
  var card = document.getElementById("build-status");

  /* The page reloads to show what landed. A summary can run over an article
     that is already built and being listened to, and reloading under the
     listener would stop the audio, so that case just stops polling. */
  function finish() {
    var audio = document.getElementById("audio");
    if (audio && !audio.paused && !audio.ended) return;
    location.reload();
  }

  function tick() {
    fetch("/api/jobs", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var jobs = data.jobs || [];
        if (!jobs.length) { finish(); return; }

        var mine = jobs;
        if (card) {
          /* An article page is about one article. Without this, somebody
             else's job painted its progress over this one's a second after
             the page loaded. */
          mine = jobs.filter(function (job) { return String(job.article) === card.dataset.article; });
          if (!mine.length) { finish(); return; }
        }

        mine.forEach(function (job) {
          var meter = document.querySelector('[data-job-meter="' + job.id + '"]') || document.getElementById("build-meter");
          var note = document.querySelector('[data-job-message="' + job.id + '"]') || document.getElementById("build-message");
          if (meter) meter.style.width = (job.progress * 100).toFixed(1) + "%";
          if (note) note.textContent = job.message || job.state;

          /* Summarising is calls to a model, not synthesis, and the page can
             be opened before the job is claimed — so the heading follows the
             queue rather than whatever was true at load. */
          var title = document.getElementById("build-title");
          if (title) {
            title.textContent = job.kind === "summarise" ? "Writing summaries…" : "Building audio…";
          }
        });

        // Back off once a long build is clearly going to take a while.
        setTimeout(tick, ++tries > 30 ? 8000 : 2000);
      })
      .catch(function () { setTimeout(tick, 8000); });
  }

  if (!document.hidden) setTimeout(tick, 1500);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && tries === 0) tick();
  });
})();
