from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from textcast.audio import encode_opus, render_article
from textcast.document import Article, Block, BlockKind, Section
from textcast.tts import ENGINES, available, get_engine
from textcast.tts.base import Clip, TTSEngine


class FakeEngine:
    """Stands in for a real engine: one second of tone per ten characters."""

    name = "fake"
    sample_rate = 24000

    def voices(self):
        from textcast.tts.base import Voice

        return [Voice(id="v1", name="One"), Voice(id="v2", name="Two")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        n = max(1, len(text) // 10) * self.sample_rate
        return Clip(samples=np.zeros(n, dtype=np.float32), sample_rate=self.sample_rate)


def sample_article() -> Article:
    return Article(
        title="Test",
        sections=[
            Section(title="One", blocks=[
                Block(kind=BlockKind.PARA, text="a" * 40),
                Block(kind=BlockKind.QUOTE, text="b" * 40),
                Block(kind=BlockKind.FOOTNOTE, text="c" * 40),
            ]),
            Section(title="Two", blocks=[Block(kind=BlockKind.PARA, text="d" * 40)]),
        ],
    ).renumber()


def test_fake_engine_satisfies_the_protocol():
    assert isinstance(FakeEngine(), TTSEngine)


def test_registry_reports_and_rejects():
    assert set(ENGINES) == {"supertonic", "kokoro"}
    assert set(available()) == {"supertonic", "kokoro"}
    with pytest.raises(ValueError, match="Unknown TTS engine"):
        get_engine("nope")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_encode_opus_writes_a_playable_file(tmp_path):
    out = tmp_path / "a.opus"
    tone = np.sin(np.linspace(0, 400, 24000)).astype(np.float32)
    encode_opus(tone, 24000, out)
    assert out.exists() and out.stat().st_size > 500


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_produces_one_file_per_section_and_a_gapless_timeline(tmp_path):
    manifest = render_article(
        sample_article(), FakeEngine(), tmp_path, voice="v1", gap_ms=200, heading_gap_ms=400
    )

    assert len(manifest.sections) == 2
    for section in manifest.sections:
        assert (tmp_path / section.file).exists()
        # Every block starts exactly where the previous one ended.
        cursor = 0
        for block in section.blocks:
            assert block.start_ms == cursor
            cursor += block.dur_ms
        assert abs(cursor - section.duration_ms) <= 2
        # The trailing pause is inside dur_ms, so speech is always shorter.
        assert all(b.speech_ms < b.dur_ms for b in section.blocks)

    assert manifest.total_ms == sum(s.duration_ms for s in manifest.sections)
    assert json.loads((tmp_path / "manifest.json").read_text())["engine"] == "fake"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_excluding_footnotes_drops_only_those_blocks(tmp_path):
    include = set(BlockKind) - {BlockKind.FOOTNOTE}
    manifest = render_article(sample_article(), FakeEngine(), tmp_path, voice="v1", include=include)
    kinds = [b.kind for s in manifest.sections for b in s.blocks]
    assert "footnote" not in kinds
    assert "quote" in kinds and "para" in kinds


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_block_cache_avoids_resynthesising(tmp_path):
    class Counting(FakeEngine):
        calls = 0

        def synthesize(self, text, voice=None, speed=1.0, lang="en"):
            Counting.calls += 1
            return super().synthesize(text, voice, speed, lang)

    cache = tmp_path / "cache"
    render_article(sample_article(), Counting(), tmp_path / "a", voice="v1", cache_dir=cache)
    first = Counting.calls
    assert first == 4

    render_article(sample_article(), Counting(), tmp_path / "b", voice="v1", cache_dir=cache)
    assert Counting.calls == first, "second render should be served entirely from cache"
