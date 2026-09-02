"""The build queue.

One worker thread polling a SQLite table. A broker for a single-user app is
machinery you would have to maintain; a table you already back up is not.

It also polls the mailbox, when one is configured. That used to be a CLI
command on a systemd timer; it is a setting now, so a container is the whole
deployment.

Two kinds of job, and neither leads to the other. A ``build`` renders the
audio. A ``summarise`` asks a language model for a summary of each section and
writes those in as blocks. Summarising does not then build: inserting a block
moves every id after it, so the audio has to be made after the text is final,
and when that happens is yours to say.

They run in their own lanes, side by side. One is the CPU for minutes and the
other is the network for seconds; queueing the quick one behind the long one
bought nothing. ``claim_job`` keeps them off the same article, which is the
only place they actually collide.

The engine is loaded once and kept, because loading it costs more than a short
article's synthesis.
"""

from __future__ import annotations

import ctypes
import gc
import json
import logging
import multiprocessing
import threading
import time
from pathlib import Path

from . import db
from .audio import render_article
from .document import BlockKind
from .prefs import voice_defaults
from .settings import Settings, get_settings, use_settings
from .tts import ENGINES, TTSEngine, get_engine, publish_engine, release_shared

log = logging.getLogger("textcast.jobs")

#: One thread each. Synthesis is CPU-bound and long; summarising is a handful
#: of network calls. Nothing is gained by making either wait for the other.
LANES = (("build",), ("summarise",))


def _trim_heap() -> None:
    """Hand the freed arenas back to the operating system.

    Dropping the tensors returns them to the allocator, not to the OS: glibc
    keeps large arenas mapped and RSS does not move until it is asked to. On
    any other allocator this is a no-op, and the memory comes back on its own
    terms.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def drain_jobs(settings: Settings, kinds: tuple[str, ...]) -> None:
    """Run every queued job of these kinds, then exit. The child's entry point.

    Whatever the work needs is imported inside this process and dies with it.
    That is the only way the memory comes back: a C extension cannot be
    unimported, and deleting it from ``sys.modules`` leaves the shared
    libraries mapped and the allocator's arenas grown.

    Measured here. A build loads torch, the model, spaCy and misaki: a worker
    that had built once held about a gigabyte for the rest of its life,
    against 37 MB before its first build. A summary loads openai, which brings
    httpx and pydantic: 38 MB became 79 MB, and stayed.

    It drains rather than doing one job, so a queue of nine pays the start-up
    once instead of nine times.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s"
    )
    use_settings(settings)
    db.init(settings.db_path)
    worker = Worker(settings)
    while worker.step(kinds):
        pass


class Worker:
    """Polls for queued builds and renders them, one at a time."""

    def __init__(self, settings: Settings | None = None, poll_seconds: float = 2.0) -> None:
        self.settings = settings or get_settings()
        self.poll_seconds = poll_seconds
        self._engines: list[TTSEngine] = []
        self._engine_key: tuple[str, int] | None = None
        self._engines_used: float | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._children: dict[str, multiprocessing.process.BaseProcess] = {}
        self._mail_checked: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start both lanes in the background."""
        if self._threads:
            return
        db.init(self.settings.db_path)
        self._requeue_orphans()
        for kinds in LANES:
            thread = threading.Thread(
                target=self._loop, args=(kinds,), name=f"textcast-{kinds[0]}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info("worker started (engine=%s)", self.settings.engine)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for child in list(self._children.values()):
            if not child.is_alive():
                continue
            # Nothing else will reap them. watchfiles restarts this process on
            # every source edit, and an orphaned build would hold the model
            # and go on writing to the job it no longer owns.
            child.terminate()
            child.join(timeout=timeout)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def run(self) -> None:
        """Start the lanes and stay in the foreground until interrupted."""
        self.start()
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _loop(self, kinds: tuple[str, ...]) -> None:
        # Mail polling stays in the parent: imaplib is the standard library
        # and costs nothing to keep.
        in_child = self.settings.job_subprocess
        while not self._stop.is_set():
            try:
                if "summarise" in kinds:
                    self._poll_mail()
                worked = self._run_in_child(kinds) if in_child else self.step(kinds)
                if not worked:
                    # The build lane owns the pool, so it is the one that
                    # decides the pool is no longer earning its memory. With
                    # the child process this is a fallback: the child takes
                    # the model with it when it exits.
                    if "build" in kinds:
                        self._release_idle_engines()
                    self._stop.wait(self.poll_seconds)
            except Exception:
                log.exception("%s lane failed", kinds[0])
                self._stop.wait(self.poll_seconds)

    def _poll_mail(self) -> None:
        """Fetch newsletters on a schedule, if a mailbox is configured.

        Failures are logged and dropped: a mailbox that is down must not stop
        the queue from building what is already in it.
        """
        minutes = self.settings.mail_poll_minutes
        if minutes <= 0:
            return
        now = time.monotonic()
        if self._mail_checked and now - self._mail_checked < minutes * 60:
            return
        self._mail_checked = now
        try:
            from .mail import fetch

            result = fetch(settings=self.settings)
            if result.added:
                log.info("mail: added %d article(s)", result.added)
        except Exception as exc:
            log.warning("mail poll failed: %s", exc)

    def _requeue_orphans(self) -> None:
        """A job left 'running' means the process died mid-build. Retry it."""
        conn = db.connect(self.settings.db_path)
        rows = conn.execute("SELECT id, article_id FROM job WHERE state = 'running'").fetchall()
        for row in rows:
            conn.execute("UPDATE job SET state = 'queued', progress = 0 WHERE id = ?", (row["id"],))
            conn.execute("UPDATE article SET status = 'queued' WHERE id = ?", (row["article_id"],))
        if rows:
            log.info("requeued %d interrupted job(s)", len(rows))

    # -- work --------------------------------------------------------------

    def engines_for(self, name: str) -> list[TTSEngine]:
        """A pool of instances, kept between jobs because loading is slow.

        The pipelines are not safe to share, so parallelism means one instance
        per worker rather than one instance driven by several threads.
        """
        count = self.settings.build_concurrency()
        key = (name, count)
        self._engines_used = time.monotonic()
        if self._engines and self._engine_key == key:
            return self._engines

        options = dict(self.settings.engine_options())
        if count > 1:
            # Each instance takes one core; the pool provides the parallelism.
            options["threads"] = 1

        log.info("loading %d %s instance(s) %s", count, name, options)
        self._engines = [get_engine(name, **options) for _ in range(count)]
        self._engine_key = key
        self._engines_used = time.monotonic()
        # Web requests in this process reuse the first one rather than paying
        # to load a second copy of the model.
        publish_engine(self._engines[0])
        return self._engines

    def _start_job_process(self, kinds: tuple[str, ...]) -> multiprocessing.process.BaseProcess:
        """Spawn, never fork: this process runs a thread per lane."""
        context = multiprocessing.get_context("spawn")
        child = context.Process(
            target=drain_jobs, args=(self.settings, kinds), name=f"textcast-{kinds[0]}"
        )
        child.start()
        return child

    def _run_in_child(self, kinds: tuple[str, ...]) -> bool:
        """Hand this lane's queued jobs to a child process and wait for it.

        Returns True when there was something to do, so the lane knows not to
        sleep. The parent imports neither torch nor openai, which is the whole
        point: it polls a table and stays under 40 MB.
        """
        conn = db.connect(self.settings.db_path)
        holes = ", ".join("?" for _ in kinds)
        queued = conn.execute(
            f"SELECT 1 FROM job WHERE kind IN ({holes}) AND state = 'queued' LIMIT 1", kinds
        ).fetchone()
        if queued is None:
            return False

        lane = kinds[0]
        child = self._children[lane] = self._start_job_process(kinds)
        try:
            child.join()
        finally:
            self._children.pop(lane, None)
        if child.exitcode:
            # The child marks its own failures. This is the other kind: killed
            # outright, by the OOM killer or a shutdown, with a job still
            # marked running and nobody left to finish it.
            log.error("the %s process exited with %s", lane, child.exitcode)
            self._requeue_orphans()
        return True

    def _release_idle_engines(self) -> None:
        """Drop the pool once nothing has needed it for a while.

        The pool is kept between jobs because loading it costs seconds. That
        trade stops paying when the queue has been empty for minutes: an idle
        worker was holding gigabytes to save one reload on a build that
        happens a few times a day.

        Only this module's references are given up. Anything mid-synthesis
        holds its own, so the model outlives the drop and dies when that
        finishes.
        """
        minutes = self.settings.idle_unload_minutes
        if minutes <= 0 or not self._engines or self._engines_used is None:
            return
        idle = time.monotonic() - self._engines_used
        if idle < minutes * 60:
            return

        engines, self._engines = self._engines, []
        self._engine_key = None
        self._engines_used = None
        release_shared(engines[0])
        count = len(engines)
        del engines
        # The pipelines hold cycles, so the collector has to run before the
        # weights are unreachable and the allocator can be asked for them.
        gc.collect()
        _trim_heap()
        log.info("dropped %d engine(s) after %.0f idle minute(s)", count, idle / 60)

    def step(self, kinds: tuple[str, ...] | None = None) -> bool:
        """Run one job of these kinds. Returns False when there is none."""
        conn = db.connect(self.settings.db_path)
        job = db.claim_job(conn, kinds)
        if job is None:
            return False

        article_id = job["article_id"]
        try:
            if job["kind"] == "summarise":
                self._summarise(conn, job)
            else:
                self._build(conn, job)
            db.update_job(job["id"], conn, state="done", progress=1.0, finished_at=db.now(), message="")
        except Exception as exc:
            log.exception("%s failed for article %s", job["kind"], article_id)
            db.update_job(
                job["id"], conn, state="failed", error=str(exc)[:800], finished_at=db.now()
            )
            # Only a build failing means the audio failed. A summary that did
            # not arrive leaves the article exactly as it was.
            if job["kind"] == "build":
                db.set_status(article_id, "failed", conn)
        return True

    def _summarise(self, conn, job) -> None:
        """Ask the model for a summary of each section and store them.

        No build follows. The summary changes the text, and choosing when to
        turn text into audio is a separate decision made on the article page.

        Every section that lands is stored the moment it lands, and every one
        that fails is named in the job's error. The pass used to be all or
        nothing: one refused call — a free tier's rate limit is the usual
        reason — threw away the summaries that had arrived alongside it, and
        the only record was a line in the worker's log.
        """
        from .summarize import SummaryError, config, summarize_article

        article_id = job["article_id"]
        article = db.load_article(article_id, conn)
        if article is None:
            raise ValueError(f"article {article_id} is gone")

        cfg = config(conn)
        if not cfg.ready:
            raise ValueError("summaries are not configured: set a model and an API key")

        def landed(outcome) -> None:
            # Store on arrival, not at the end: what the model has already
            # written must survive a sibling call failing, or the worker
            # being killed halfway through.
            if not outcome.error:
                db.replace_blocks(article_id, article, conn)
            note = f"section {outcome.done} of {outcome.total}"
            if outcome.failed:
                note += f" · {outcome.failed} failed"
            db.update_job(
                job["id"], conn, progress=outcome.done / outcome.total, message=note
            )

        replace = bool(json.loads(job["options"] or "{}").get("replace"))
        db.update_job(job["id"], conn, progress=0.0, message=f"asking {cfg.model}")
        run = summarize_article(article, cfg, on_section=landed, replace=replace)
        log.info(
            "summarised %d of %d section(s) of article %s, %d failed",
            run.added, run.total, article_id, run.failed,
        )

        # claim_job leaves a summary's article alone, but replace_blocks has
        # just cleared the audio counters if anything landed. Say where the
        # audio actually stands.
        db.set_status(article_id, "ready" if row_has_audio(conn, article_id) else "new", conn)

        if run.errors:
            detail = "; ".join(run.errors[:3])
            if run.failed > 3:
                detail += f"; and {run.failed - 3} more"
            raise SummaryError(
                f"{run.added} of {run.total} sections summarised, {run.failed} failed. {detail}"
            )

    def _build(self, conn, job) -> None:
        article_id = job["article_id"]
        article = db.load_article(article_id, conn)
        if article is None:
            raise ValueError(f"article {article_id} is gone")

        row = db.get_article(article_id, conn)

        # Three layers, most specific first: what this job asked for, what the
        # article was saved with, then the global default.
        stored = db.get_build_options(article_id, conn)
        options = {**stored, **json.loads(job["options"] or "{}")}

        settings = self.settings
        # Three layers again: this article's own choice, the saved default,
        # then the environment. `voice_defaults` folds the last two.
        chosen = voice_defaults(conn, settings)
        engine_name = options.get("engine") or chosen.engine
        if engine_name not in ENGINES:
            # An article saved against an engine that no longer ships still
            # builds, with the engine that does.
            log.warning("article %s asks for engine %r; using %s",
                        article_id, engine_name, settings.engine)
            engine_name = settings.engine
        voice = options.get("voice") or chosen.voice or ENGINES[engine_name].default_voice
        quote_voice = options.get("quote_voice") or chosen.quote_voice
        speed = float(options.get("speed") or chosen.speed or 1.0)

        engines = self.engines_for(engine_name)
        engine = engines[0]

        include = set(BlockKind)
        if options.get("skip_footnotes"):
            include.discard(BlockKind.FOOTNOTE)
        if options.get("skip_summaries"):
            include.discard(BlockKind.SUMMARY)

        out_dir = settings.media_dir / row["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)

        last_write = [0.0]
        throttle = threading.Lock()

        def progress(done: int, total: int, block_id: str) -> None:
            # Throttle: a write per block would be thousands of transactions.
            now = time.monotonic()
            with throttle:
                if now - last_write[0] < 1.0 and done != total:
                    return
                last_write[0] = now
            # The render pool calls this, not the worker thread, and a sqlite3
            # connection may only be used by the thread that opened it.
            # db.connect hands back this thread's own.
            db.update_job(
                job["id"],
                db.connect(self.settings.db_path),
                progress=done / total,
                message=f"block {done} of {total}",
            )

        manifest = render_article(
            article,
            engine,
            out_dir,
            pool=engines[1:],
            voice=voice,
            quote_voice=quote_voice or None,
            speed=speed,
            bitrate=settings.bitrate,
            gap_ms=settings.gap_ms,
            heading_gap_ms=settings.heading_gap_ms,
            include=include,
            cache_dir=settings.cache_dir,
            progress=progress,
        )

        # Idle is measured from when the work stopped, not when it started,
        # or a long build would leave the pool eligible to drop the moment it
        # finished.
        self._engines_used = time.monotonic()

        audio_bytes = sum(f.stat().st_size for f in out_dir.glob("*.opus"))
        db.save_manifest(article_id, manifest, audio_bytes, conn)
        log.info("built %s: %.1f min, %.1f MB", row["slug"], manifest.total_ms / 60000, audio_bytes / 1e6)


def row_has_audio(conn, article_id: int) -> bool:
    row = conn.execute("SELECT audio_ms FROM article WHERE id = ?", (article_id,)).fetchone()
    return bool(row and row["audio_ms"])


def media_dir_for(slug: str, settings: Settings | None = None) -> Path:
    return (settings or get_settings()).media_dir / slug


def main() -> int:
    """Run the worker in the foreground. `python -m textcast`.

    The only thing here that starts from a terminal. The web app is uvicorn's
    to run, and everything else is done in the app itself.
    """
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s"
    )
    settings = get_settings()
    settings.ensure_dirs()
    log.info("worker started (engine=%s, data=%s)", settings.engine, settings.data_dir)
    Worker(settings).run()
    return 0
