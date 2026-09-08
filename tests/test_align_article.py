"""`align_article`: the phase-two orchestration -- finding each block's
cached audio, running the aligner, composing and caching the result.

Uses a `FakeAligner` rather than the real ONNX model: what this file tests
is the plumbing (does it find the right cache file, does the quote-voice
block get the quote voice's key, does it cache what it computes, does a
failure degrade one block rather than the build) — not alignment quality,
which `test_aligner.py` and `test_sourcemap.py` already cover against real
audio and the real corpus.
"""

from __future__ import annotations

import numpy as np
import pytest

from textcast.audio import align_article, render_article
from textcast.document import Article, Block, BlockKind, Section
from textcast.tts.aligner import AlignedWord, to_target_text


class FakeEngine:
    name = "fake"
    sample_rate = 24000

    def voices(self):
        from textcast.tts.base import Voice

        return [Voice(id="v1", name="One"), Voice(id="v2", name="Two")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        from textcast.tts.base import Clip

        n = max(1, len(text) // 10) * self.sample_rate
        return Clip(samples=np.zeros(n, dtype=np.float32) + 0.5, sample_rate=self.sample_rate)


class FakeAligner:
    """Evenly spaces one `AlignedWord` per target word across the clip.

    Real alignment is not the point here — the composition and cache
    plumbing is. Counts calls, so a test can assert the cache was actually
    used on a second pass rather than recomputed.
    """

    def __init__(self):
        self.calls = 0

    def align(self, waveform, sample_rate, text):
        self.calls += 1
        target_words = [w for w in to_target_text(text).split("|") if w]
        if not target_words:
            return []
        dur_ms = round(len(waveform) / sample_rate * 1000)
        step = dur_ms / len(target_words)
        return [
            AlignedWord(text=w, start_ms=round(i * step), end_ms=round((i + 1) * step))
            for i, w in enumerate(target_words)
        ]


class WrongCountAligner:
    """Always returns one word too few, to exercise the mismatch fallback."""

    def align(self, waveform, sample_rate, text):
        return [AlignedWord(text="X", start_ms=0, end_ms=1)]


def sample_article() -> Article:
    return Article(
        title="Test",
        sections=[
            Section(title="One", blocks=[
                Block(kind=BlockKind.PARA, text="Markets steadied on Thursday."),
                Block(kind=BlockKind.QUOTE, text="This changes everything."),
                Block(kind=BlockKind.FOOTNOTE, text="A citation follows here."),
            ]),
        ],
    ).renumber()


def test_align_article_fills_in_word_timings_and_caches_them(tmp_path):
    article = sample_article()
    cache = tmp_path / "cache"
    manifest = render_article(article, FakeEngine(), tmp_path / "out", voice="v1", cache_dir=cache)

    aligner = FakeAligner()
    align_article(article, manifest, aligner, voice="v1", cache_dir=cache)

    all_timings = [t for section in manifest.sections for t in section.blocks]
    assert all_timings, "no blocks in the manifest"
    for timing in all_timings:
        assert timing.words, f"{timing.id} got no word timings"
        for word in timing.words:
            assert timing.speech_start_ms <= word.start_ms
            assert word.dur_ms > 0

    words_files = list(cache.glob("*.words.json"))
    assert len(words_files) == len(all_timings)


def test_align_article_reads_cached_words_without_recomputing(tmp_path):
    article = sample_article()
    cache = tmp_path / "cache"
    manifest = render_article(article, FakeEngine(), tmp_path / "out", voice="v1", cache_dir=cache)

    first = FakeAligner()
    align_article(article, manifest, first, voice="v1", cache_dir=cache)
    calls_first_pass = first.calls
    assert calls_first_pass > 0

    # A second manifest for the same article and cache -- as a rebuild with
    # an unchanged text would produce -- must read the cached .words.json
    # rather than asking the aligner again.
    manifest2 = render_article(article, FakeEngine(), tmp_path / "out2", voice="v1", cache_dir=cache)
    second = FakeAligner()
    align_article(article, manifest2, second, voice="v1", cache_dir=cache)
    assert second.calls == 0

    words_first = [t.words for section in manifest.sections for t in section.blocks]
    words_second = [t.words for section in manifest2.sections for t in section.blocks]
    assert words_first == words_second


def test_align_article_uses_the_quote_voice_s_own_cache_key(tmp_path):
    article = sample_article()
    cache = tmp_path / "cache"
    manifest = render_article(
        article, FakeEngine(), tmp_path / "out", voice="v1", quote_voice="v2", cache_dir=cache
    )

    aligner = FakeAligner()
    align_article(article, manifest, aligner, voice="v1", quote_voice="v2", cache_dir=cache)

    quote_timing = next(
        t for section in manifest.sections for t in section.blocks
        if t.kind == str(BlockKind.QUOTE)
    )
    # Found its own cache file (keyed on the quote voice, matching
    # render_article's own choice) and got real word timings from it --
    # if the keys had disagreed, the .i16 lookup would have missed and
    # this block would have come back with words == [].
    assert quote_timing.words


def test_align_article_leaves_a_mismatched_block_with_no_words_and_does_not_raise(tmp_path):
    article = sample_article()
    cache = tmp_path / "cache"
    manifest = render_article(article, FakeEngine(), tmp_path / "out", voice="v1", cache_dir=cache)

    align_article(article, manifest, WrongCountAligner(), voice="v1", cache_dir=cache)

    for section in manifest.sections:
        for timing in section.blocks:
            assert timing.words == []
    # No .words.json written for a failed block either -- a bad result must
    # not be trusted on the next build just because it was cheap to cache.
    assert not list(cache.glob("*.words.json"))


def test_align_article_reports_progress_once_per_block(tmp_path):
    article = sample_article()
    cache = tmp_path / "cache"
    manifest = render_article(article, FakeEngine(), tmp_path / "out", voice="v1", cache_dir=cache)

    seen = []
    align_article(
        article, manifest, FakeAligner(), voice="v1", cache_dir=cache,
        progress=lambda done, total, block_id: seen.append((done, total, block_id)),
    )
    total_blocks = sum(len(section.blocks) for section in manifest.sections)
    assert len(seen) == total_blocks
    assert seen[-1][0] == total_blocks


@pytest.mark.parametrize("kind", [BlockKind.PARA])
def test_align_article_is_a_noop_on_an_article_with_no_manifest_blocks(tmp_path, kind):
    article = Article(title="Empty", sections=[Section(title="One", blocks=[])]).renumber()
    from textcast.audio import AudioManifest

    manifest = AudioManifest(engine="fake", voice="v1", sample_rate=24000, bitrate="32k", total_ms=0)
    align_article(article, manifest, FakeAligner(), voice="v1", cache_dir=tmp_path / "cache")
    assert manifest.sections == []


def test_cached_word_timings_follow_the_block_when_an_earlier_one_changes(tmp_path):
    """The cache is keyed on the spoken text, which says nothing about where
    the block sits. It once stored absolute times, so editing any earlier
    block left every later one aligned to the previous build's layout --
    measured at six seconds adrift after one paragraph changed."""
    cache = tmp_path / "cache"

    def article_after(opening: str) -> Article:
        return Article(title="T", sections=[Section(title="One", blocks=[
            Block(kind=BlockKind.PARA, text=opening),
            Block(kind=BlockKind.PARA, text="Markets steadied on Thursday."),
        ])]).renumber()

    first = article_after("Short one.")
    m1 = render_article(first, FakeEngine(), tmp_path / "o1", voice="v1", cache_dir=cache)
    align_article(first, m1, FakeAligner(), voice="v1", cache_dir=cache)

    # Only the *first* block's text changes, so the second block's audio and
    # its word timings are both cache hits -- and its speech_start_ms moves.
    second = article_after("A very much longer opening paragraph than before, by some margin.")
    m2 = render_article(second, FakeEngine(), tmp_path / "o2", voice="v1", cache_dir=cache)
    aligner = FakeAligner()
    align_article(second, m2, aligner, voice="v1", cache_dir=cache)

    unchanged = m2.sections[0].blocks[1]
    assert aligner.calls == 1, "the unchanged block should have come from the cache"
    assert m2.sections[0].blocks[1].speech_start_ms > m1.sections[0].blocks[1].speech_start_ms
    assert unchanged.words[0].start_ms == unchanged.speech_start_ms


def test_a_version_one_words_file_is_ignored_rather_than_trusted(tmp_path):
    """The old format was a bare list of absolute times. Reading one as if it
    were relative would shift it twice, so it is discarded and realigned."""
    import json

    article = sample_article()
    cache = tmp_path / "cache"
    manifest = render_article(article, FakeEngine(), tmp_path / "out", voice="v1", cache_dir=cache)
    align_article(article, manifest, FakeAligner(), voice="v1", cache_dir=cache)

    stale = sorted(cache.glob("*.words.json"))[0]
    stale.write_text(json.dumps([{"text": "x", "start_ms": 999999, "dur_ms": 1}]))

    manifest2 = render_article(article, FakeEngine(), tmp_path / "out2", voice="v1", cache_dir=cache)
    aligner = FakeAligner()
    align_article(article, manifest2, aligner, voice="v1", cache_dir=cache)

    assert aligner.calls == 1, "the version 1 file should have forced one realignment"
    assert all(
        w.start_ms < 999999 for s in manifest2.sections for b in s.blocks for w in b.words
    )
