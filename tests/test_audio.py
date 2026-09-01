from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from textcast.audio import encode_opus, render_article
from textcast.document import Article, Block, BlockKind, Section
from textcast.tts import ENGINES, EngineNotAvailable, available, get_engine
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


def test_availability_probes_the_dependency_not_the_wrapper():
    """The wrapper module always imports; only the real package tells the truth.

    Its heavy import sits inside ``__init__``, so importing
    ``textcast.tts.kokoro`` succeeds even with kokoro uninstalled. Probing the
    wrapper reported every engine as installed.
    """
    import importlib

    for spec in ENGINES.values():
        importlib.import_module(spec.module)  # always succeeds
        assert spec.requires != spec.module

    have = available()
    assert have["supertonic"] is True, "the default engine should be installed"

    if not have["kokoro"]:
        with pytest.raises(EngineNotAvailable, match="uv sync --extra kokoro"):
            get_engine("kokoro")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_pool_renders_blocks_in_order(tmp_path):
    """Parallel synthesis must not reorder the audio.

    Blocks come back from the pool as they finish, so the order has to be
    restored from the index rather than from completion time.
    """
    class Slow(FakeEngine):
        """Later blocks finish first, which would expose any ordering bug."""

        def __init__(self, tag):
            self.tag = tag

        def synthesize(self, text, voice=None, speed=1.0, lang="en"):
            import time

            time.sleep(0.05 if text.startswith("a") else 0.0)
            n = (len(text) // 10 + 1) * self.sample_rate
            return Clip(samples=np.zeros(n, dtype=np.float32), sample_rate=self.sample_rate)

    article = Article(
        title="Ordered",
        sections=[Section(title="One", blocks=[
            Block(kind=BlockKind.PARA, text="a" * 40),
            Block(kind=BlockKind.PARA, text="b" * 80),
            Block(kind=BlockKind.PARA, text="c" * 120),
            Block(kind=BlockKind.PARA, text="d" * 160),
        ])],
    ).renumber()

    primary = Slow("0")
    manifest = render_article(
        article, primary, tmp_path, pool=[Slow("1"), Slow("2"), Slow("3")],
        voice="v1", gap_ms=100,
    )

    blocks = manifest.sections[0].blocks
    assert [b.id for b in blocks] == ["b0-0", "b0-1", "b0-2", "b0-3"]
    # Durations grow with text length, so an out-of-order render shows up here.
    assert [b.speech_ms for b in blocks] == sorted(b.speech_ms for b in blocks)

    cursor = 0
    for block in blocks:
        assert block.start_ms == cursor
        cursor += block.dur_ms


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_pool_reports_progress_once_per_block(tmp_path):
    seen = []
    render_article(
        sample_article(), FakeEngine(), tmp_path,
        pool=[FakeEngine(), FakeEngine()], voice="v1",
        progress=lambda done, total, block_id: seen.append((done, total)),
    )
    assert len(seen) == 4
    assert sorted(d for d, _t in seen) == [1, 2, 3, 4], "counted once each, no duplicates"
