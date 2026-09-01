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

import json
import logging
import threading
import time
from pathlib import Path

from . import db
from .audio import render_article
from .document import BlockKind
from .prefs import voice_defaults
from .settings import Settings, get_settings
from .tts import ENGINES, TTSEngine, get_engine, publish_engine

log = logging.getLogger("textcast.jobs")

#: One thread each. Synthesis is CPU-bound and long; summarising is a handful
#: of network calls. Nothing is gained by making either wait for the other.
LANES = (("build",), ("summarise",))


class Worker:
    """Polls for queued builds and renders them, one at a time."""

    def __init__(self, settings: Settings | None = None, poll_seconds: float = 2.0) -> None:
        self.settings = settings or get_settings()
        self.poll_seconds = poll_seconds
        self._engines: list[TTSEngine] = []
        self._engine_key: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
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
        while not self._stop.is_set():
            try:
                if "summarise" in kinds:
                    self._poll_mail()
                if not self.step(kinds):
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
        if self._engines and self._engine_key == key:
            return self._engines

        options = dict(self.settings.engine_options())
        if count > 1:
            # Each instance takes one core; the pool provides the parallelism.
            options["threads"] = 1

        log.info("loading %d %s instance(s) %s", count, name, options)
        self._engines = [get_engine(name, **options) for _ in range(count)]
        self._engine_key = key
        # Web requests in this process reuse the first one rather than paying
        # to load a second copy of the model.
        publish_engine(self._engines[0])
        return self._engines

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
        """
        from .summarize import config, summarize_article

        article_id = job["article_id"]
        article = db.load_article(article_id, conn)
        if article is None:
            raise ValueError(f"article {article_id} is gone")

        cfg = config(conn)
        if not cfg.ready:
            raise ValueError("summaries are not configured: set a model and an API key")

        def progress(done: int, total: int) -> None:
            db.update_job(job["id"], conn, progress=done / total, message=f"section {done} of {total}")

        replace = bool(json.loads(job["options"] or "{}").get("replace"))
        added = summarize_article(article, cfg, progress=progress, replace=replace)
        if added or replace:
            db.replace_blocks(article_id, article, conn)
        log.info("summarised %d section(s) of article %s", added, article_id)

        # claim_job marked the article as building. Nothing is being built, so
        # put it back to where it actually is: text, and no audio yet.
        db.set_status(article_id, "ready" if row_has_audio(conn, article_id) else "new", conn)

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
        engine_name = options.get("engine") or settings.engine
        if engine_name not in ENGINES:
            # An article saved against an engine that no longer ships still
            # builds, with the engine that does.
            log.warning("article %s asks for engine %r; using %s",
                        article_id, engine_name, settings.engine)
            engine_name = settings.engine
        # Three layers again: this article's own choice, the saved default,
        # then whatever the engine ships with.
        chosen = voice_defaults(conn, settings)
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
