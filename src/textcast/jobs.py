"""The build queue.

One worker thread polling a SQLite table. A broker for a single-user app is
machinery you would have to maintain; a table you already back up is not.

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
from .settings import Settings, get_settings
from .tts import TTSEngine, get_engine

log = logging.getLogger("textcast.jobs")


class Worker:
    """Polls for queued builds and renders them, one at a time."""

    def __init__(self, settings: Settings | None = None, poll_seconds: float = 2.0) -> None:
        self.settings = settings or get_settings()
        self.poll_seconds = poll_seconds
        self._engines: list[TTSEngine] = []
        self._engine_key: tuple[str, int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="textcast-worker", daemon=True)
        self._thread.start()
        log.info("worker started (engine=%s)", self.settings.engine)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def run(self) -> None:
        db.init(self.settings.db_path)
        self._requeue_orphans()
        while not self._stop.is_set():
            try:
                if not self.step():
                    self._stop.wait(self.poll_seconds)
            except Exception:
                log.exception("worker loop failed")
                self._stop.wait(self.poll_seconds)

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

    def engines_for(self, name: str, steps: int) -> list[TTSEngine]:
        """A pool of instances, kept between jobs because loading is slow.

        The pipelines are not safe to share, so parallelism means one instance
        per worker rather than one instance driven by several threads.
        """
        count = self.settings.build_concurrency()
        key = (name, steps, count)
        if self._engines and self._engine_key == key:
            return self._engines

        options = dict(self.settings.engine_options())
        if name == "supertonic":
            options["steps"] = steps
        if count > 1:
            # Each instance takes one core; the pool provides the parallelism.
            options["threads"] = 1

        log.info("loading %d %s instance(s) %s", count, name, options)
        self._engines = [get_engine(name, **options) for _ in range(count)]
        self._engine_key = key
        return self._engines

    def step(self) -> bool:
        """Run one job. Returns False when the queue is empty."""
        conn = db.connect(self.settings.db_path)
        job = db.claim_job(conn)
        if job is None:
            return False

        article_id = job["article_id"]
        try:
            self._build(conn, job)
            db.update_job(job["id"], conn, state="done", progress=1.0, finished_at=db.now(), message="")
        except Exception as exc:
            log.exception("build failed for article %s", article_id)
            db.update_job(
                job["id"], conn, state="failed", error=str(exc)[:800], finished_at=db.now()
            )
            db.set_status(article_id, "failed", conn)
        return True

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
        steps = int(options.get("steps") or settings.steps)
        voice = options.get("voice") or settings.voice
        quote_voice = options.get("quote_voice") or settings.quote_voice

        engines = self.engines_for(engine_name, steps)
        engine = engines[0]
        if not voice:
            from .tts import ENGINES

            voice = ENGINES[engine_name].default_voice

        include = set(BlockKind)
        if options.get("skip_footnotes"):
            include.discard(BlockKind.FOOTNOTE)
        if options.get("skip_summaries"):
            include.discard(BlockKind.SUMMARY)

        out_dir = settings.media_dir / row["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)

        last_write = [0.0]

        def progress(done: int, total: int, block_id: str) -> None:
            # Throttle: a write per block would be thousands of transactions.
            now = time.monotonic()
            if now - last_write[0] < 1.0 and done != total:
                return
            last_write[0] = now
            db.update_job(
                job["id"], conn, progress=done / total, message=f"block {done} of {total}"
            )

        manifest = render_article(
            article,
            engine,
            out_dir,
            pool=engines[1:],
            voice=voice,
            quote_voice=quote_voice or None,
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


def media_dir_for(slug: str, settings: Settings | None = None) -> Path:
    return (settings or get_settings()).media_dir / slug
