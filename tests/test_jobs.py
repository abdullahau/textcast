"""The build worker: the two job kinds, and what runs on which thread."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from textcast import db
from textcast.jobs import Worker
from textcast.service import ingest
from textcast.tts.base import Clip, Voice


class FakeEngine:
    name = "fake"
    sample_rate = 24000

    def voices(self):
        return [Voice(id="v1", name="One")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        n = max(1, len(text) // 10) * self.sample_rate
        return Clip(samples=np.zeros(n, dtype=np.float32), sample_rate=self.sample_rate)


LONG_NOTE = "# One\n\n" + "\n\n".join(f"Paragraph number {n} of the first section." for n in range(8))


def stub_pool(worker: Worker, count: int = 3) -> None:
    """Replace the model with a pool of cheap fakes."""
    worker.engines_for = lambda name: [FakeEngine() for _ in range(count)]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_parallel_build_can_report_its_progress(conn, settings):
    """Progress is written from the render pool, not from the worker thread.

    A sqlite3 connection may only be used by the thread that opened it, so
    passing the worker's connection into the callback failed every build that
    ran more than one engine.
    """
    stored = ingest(text=LONG_NOTE, title="A long note")
    worker = Worker(settings)
    stub_pool(worker)

    assert worker.step() is True

    job = db.get_job(stored.job_id, conn)
    assert job["state"] == "done", job["error"]
    assert db.get_article(stored.article_id, conn)["status"] == "ready"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_summarise_job_writes_the_blocks_and_stops_there(conn, settings, monkeypatch):
    from textcast import summarize
    from textcast.document import BlockKind

    summarize.save_config(conn, api_key="k", model="stub-1")
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="In short."))])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply))
    )
    monkeypatch.setattr(summarize, "_client", lambda cfg: client)

    stored = ingest(text=LONG_NOTE, title="A long note", options={"summarize": True})
    worker = Worker(settings)
    stub_pool(worker)

    assert worker.step() is True, "the summarise job"
    article = db.load_article(stored.article_id, conn)
    assert article.sections[0].blocks[0].kind is BlockKind.SUMMARY

    # Building is a separate decision, made on the article page.
    assert worker.step() is False, "no build was queued behind it"
    assert db.active_jobs(conn) == []
    assert db.get_article(stored.article_id, conn)["status"] == "new"


def test_an_article_asking_for_a_retired_engine_still_builds(conn, settings, caplog):
    """A build option naming an engine that no longer ships must not be fatal."""
    stored = ingest(text=LONG_NOTE, title="A long note", build=False)
    db.set_build_options(stored.article_id, {"engine": "gone"}, conn)
    db.enqueue(stored.article_id, conn=conn)

    worker = Worker(settings)
    stub_pool(worker, count=1)
    worker.step()

    assert db.get_article(stored.article_id, conn)["status"] == "ready"
    assert "gone" in caplog.text
