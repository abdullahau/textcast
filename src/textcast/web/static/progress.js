/* Poll the build queue while something is rendering, and reload when it lands. */
(function () {
  "use strict";
  var tries = 0;

  function tick() {
    fetch("/api/jobs", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var jobs = data.jobs || [];
        if (!jobs.length) { location.reload(); return; }

        jobs.forEach(function (job) {
          var meter = document.querySelector('[data-job-meter="' + job.id + '"]') || document.getElementById("build-meter");
          var note = document.querySelector('[data-job-message="' + job.id + '"]') || document.getElementById("build-message");
          if (meter) meter.style.width = (job.progress * 100).toFixed(1) + "%";
          if (note) note.textContent = job.message || job.state;

          /* A summarise job is calls to a language model, not synthesis. The
             page can be opened before the job is claimed, so the heading has
             to follow the queue rather than the render at load. */
          var title = document.getElementById("build-title");
          if (title) {
            title.textContent = job.kind === "summarise" ? "Summarising…" : "Building audio…";
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
