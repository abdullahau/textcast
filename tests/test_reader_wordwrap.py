"""The reader page actually renders word-level spans for a highlighted article.

Everything below `word_wrap` itself is covered by test_wordwrap.py's unit
tests; this is the one test that proves the whole chain -- build, align,
`build_payload`, `words_by_block`, the template -- produces real
`data-w` spans in the page a browser gets, not just in isolated pieces.
"""

from __future__ import annotations

import shutil

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="the web extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

from textcast import db, prefs  # noqa: E402
from textcast.jobs import Worker  # noqa: E402
from textcast.service import ingest  # noqa: E402
from textcast.tts.base import Clip, Voice  # noqa: E402
from textcast.web import app as web  # noqa: E402

NOTE = "# One\n\nThe $4.4tn company raised guidance for Q3 after profits since 2019 rose."


class FakeEngine:
    name = "fake"
    sample_rate = 24000

    def voices(self):
        return [Voice(id="v1", name="One")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        n = max(1, len(text) // 10) * self.sample_rate
        return Clip(samples=np.zeros(n, dtype=np.float32) + 0.5, sample_rate=self.sample_rate)


class FakeAligner:
    def align(self, waveform, sample_rate, text):
        from textcast.tts.aligner import AlignedWord, to_target_text

        target_words = [w for w in to_target_text(text).split("|") if w]
        if not target_words:
            return []
        dur_ms = round(len(waveform) / sample_rate * 1000)
        step = dur_ms / len(target_words)
        return [
            AlignedWord(text=w, start_ms=round(i * step), end_ms=round((i + 1) * step))
            for i, w in enumerate(target_words)
        ]


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(web, "settings", settings)
    monkeypatch.setattr(web, "_voices", lambda *a: [])
    db.init(settings.db_path)
    with TestClient(web.app) as running:
        yield running


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_reader_page_carries_word_level_spans_for_a_highlighted_article(
    client, conn, settings
):
    prefs.save_voice_defaults(conn, word_highlight=True)
    stored = ingest(text=NOTE, title="Word-wrapped")
    slug = db.get_article(stored.article_id, conn)["slug"]

    worker = Worker(settings)
    worker.engines_for = lambda name: [FakeEngine()]
    worker._aligner = FakeAligner()
    assert worker.step() is True  # build
    assert worker.step() is True  # align

    page = client.get(f"/a/{slug}").text
    assert 'class="w" data-w="0"' in page
    # The money expansion collapses onto the one displayed token, not six
    # separate spans -- proof the whole chain (not just word_wrap in
    # isolation) produced the merged group, not a naive per-word split.
    assert ">$4.4tn<" in page or "$4.4tn</span>" in page


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_reader_page_has_no_word_spans_when_word_highlight_is_off(client, conn, settings):
    stored = ingest(text=NOTE, title="Not wrapped")
    slug = db.get_article(stored.article_id, conn)["slug"]

    worker = Worker(settings)
    worker.engines_for = lambda name: [FakeEngine()]
    assert worker.step() is True  # build, no align enqueued

    page = client.get(f"/a/{slug}").text
    assert 'data-w="0"' not in page
    assert "The" in page  # the block still rendered, just unwrapped
