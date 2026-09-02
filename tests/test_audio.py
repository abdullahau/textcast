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


class PaddedEngine:
    """Like Kokoro: silence, then a second of speech, then more silence."""

    name = "padded"
    sample_rate = 24000
    LEAD_MS, SPEECH_MS, TRAIL_MS = 300, 1000, 500

    def voices(self):
        from textcast.tts.base import Voice

        return [Voice(id="v1", name="One")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        sr = self.sample_rate
        lead = np.zeros(round(sr * self.LEAD_MS / 1000), dtype=np.float32)
        trail = np.zeros(round(sr * self.TRAIL_MS / 1000), dtype=np.float32)
        n = round(sr * self.SPEECH_MS / 1000)
        speech = (0.4 * np.sin(np.linspace(0, 220 * 2 * np.pi, n))).astype(np.float32)
        return Clip(samples=np.concatenate([lead, speech, trail]), sample_rate=sr)


def test_a_block_starts_on_its_first_word():
    """Kokoro pads every clip, and the padding used to land inside the timing.

    Seeking to a block then began about 300 ms before it said anything, and
    the pause between two blocks ran past a second when 350 ms was asked for.
    """
    from textcast.audio import SILENCE_MARGIN_MS, trim_silence

    engine = PaddedEngine()
    clip = engine.synthesize("anything")
    trimmed = trim_silence(clip.samples, engine.sample_rate)

    kept_ms = round(len(trimmed) / engine.sample_rate * 1000)
    assert abs(kept_ms - (engine.SPEECH_MS + 2 * SILENCE_MARGIN_MS)) <= 5
    assert abs(trimmed[0]) < 0.05, "a margin is left, so the first sound is not clipped"


def test_a_block_that_is_quiet_all_through_is_kept_whole():
    """Better a long pause than a block that disappears from the article."""
    from textcast.audio import trim_silence

    quiet = np.full(2400, 0.001, dtype=np.float32)

    assert len(trim_silence(quiet, 24000)) == len(quiet)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_gap_between_blocks_is_the_gap_that_was_asked_for(tmp_path):
    gap = 350
    manifest = render_article(
        sample_article(), PaddedEngine(), tmp_path, voice="v1", gap_ms=gap, heading_gap_ms=gap
    )
    timings = manifest.sections[0].blocks

    # What you hear, which is not where the cues fall: a cue opens half a gap
    # before its speech, so the audible silence has to be measured from where
    # the speech actually sits in the file.
    speech = timings[0].speech_ms
    second_speech_at = timings[1].start_ms + gap // 2
    between = second_speech_at - speech
    assert between == gap, f"expected the configured pause, got {between} ms"
    assert speech < 1200, f"the model's own silence is still in the block: {speech} ms"


def test_registry_reports_and_rejects():
    assert set(ENGINES) == {"kokoro", "kokoro-onnx"}
    assert set(available()) == {"kokoro", "kokoro-onnx"}
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


def test_voices_are_listed_without_building_an_engine():
    """A page with a voice picker must not wait for 82M parameters to load."""
    from textcast.tts import catalogue, loaded_engine

    listed = catalogue("kokoro")

    assert [v.id for v in listed][:1] == ["af_alloy"]
    assert all(v.lang == "en-us" for v in listed)
    assert loaded_engine("kokoro") is None, "listing must not have loaded the model"


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

    if not available()["kokoro"]:
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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_block_begins_in_the_silence_before_it_not_on_its_first_word(tmp_path):
    """Landing exactly on the attack means anything slow — a decoder spinning
    up, a browser a frame behind — starts you inside the first word. Half the
    gap in front absorbs that, and costs nothing to listen to."""
    gap = 350
    manifest = render_article(
        sample_article(), FakeEngine(), tmp_path, voice="v1", gap_ms=gap, heading_gap_ms=gap
    )
    timings = manifest.sections[0].blocks

    speech_at = 0
    for index, timing in enumerate(timings):
        run_up = speech_at - timing.start_ms
        if index == 0:
            assert run_up == 0, "the first block has no silence in front of it"
        else:
            assert run_up == gap // 2, f"{timing.id} had {run_up} ms of run-up"
        speech_at += timing.speech_ms + gap


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_cues_still_cover_the_section_without_a_gap(tmp_path):
    manifest = render_article(sample_article(), FakeEngine(), tmp_path, voice="v1", gap_ms=350)
    timings = manifest.sections[0].blocks

    for earlier, later in zip(timings[:-1], timings[1:], strict=True):
        assert earlier.start_ms + earlier.dur_ms == later.start_ms
    assert timings[0].start_ms == 0
    last = timings[-1]
    assert last.start_ms + last.dur_ms == manifest.sections[0].duration_ms


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_moving_the_cues_does_not_move_the_audio(tmp_path):
    """The map changed; the file did not. A rebuild for this is encode-only."""
    manifest = render_article(sample_article(), FakeEngine(), tmp_path / "a", voice="v1", gap_ms=350)

    spoken = sum(t.speech_ms for t in manifest.sections[0].blocks)
    pauses = 350 * len(manifest.sections[0].blocks)
    assert abs(manifest.sections[0].duration_ms - (spoken + pauses)) <= 2


def test_only_the_named_warnings_are_silenced():
    """Blanket suppression would hide phonemizer's "words count mismatch",
    which is the only signal that something reached the engine that should
    not have."""
    import warnings

    from textcast.tts import kokoro as engine

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine._quieted = False
        engine._quiet_known_warnings()
        warnings.warn(
            "dropout option adds dropout after all but last recurrent layer, "
            "so non-zero dropout expects num_layers greater than 1",
            UserWarning,
            stacklevel=1,
        )
        warnings.warn("phonemizer words count mismatch", UserWarning, stacklevel=1)

    engine._quieted = False
    assert [str(w.message) for w in caught] == ["phonemizer words count mismatch"]


def test_the_two_engines_offer_the_same_voices_by_the_same_names():
    """They are the same voices. A picker shows one engine's at a time and the
    engine select says which, so a label on every line only repeats it."""
    from textcast.tts import kokoro, kokoro_onnx

    torch = {(v.id, v.name, v.gender) for v in kokoro.voices()}
    onnx = {(v.id, v.name, v.gender) for v in kokoro_onnx.voices()}

    assert onnx == torch
    assert not any("ONNX" in v.name for v in kokoro_onnx.voices())


def test_misaki_ipa_markup_never_reaches_the_onnx_engine():
    """`[LIBOR](/lˈIbɔɹ/)` is misaki's own notation. espeak reads it aloud —
    "libber slash el stress eye bee open-or turned-ar slash"."""
    from textcast.tts.kokoro_onnx import strip_ipa_markup

    assert strip_ipa_markup("[LIBOR](/lˈIbɔɹ/) rates") == "LIBOR rates"
    assert strip_ipa_markup("no markup here") == "no markup here"
    assert strip_ipa_markup("[a](/x/) and [b](/y/)") == "a and b"


def test_the_onnx_engine_says_what_is_missing_rather_than_failing_late(tmp_path):
    from textcast.tts.kokoro_onnx import KokoroOnnxEngine

    with pytest.raises(FileNotFoundError, match="kokoro-v1.0.onnx"):
        KokoroOnnxEngine(models_dir=tmp_path)


def test_the_engine_decides_which_notation_the_text_carries(conn):
    """A phoneme rule is the one thing in the pipeline that is not
    engine-agnostic, so the engine has to be asked before the text is made."""
    from textcast import db
    from textcast.document import Block, BlockKind
    from textcast.tts import g2p_of

    db.add_pronunciation(
        "word", "LIBOR", "", conn, misaki="lˈIbɔɹ", espeak="lˈaɪbɔːɹ"
    )
    block = Block(kind=BlockKind.PARA, text="The LIBOR rate.")

    assert "lˈIbɔɹ" in block.spoken(g2p="misaki")
    assert "lˈaɪbɔːɹ" in block.spoken(g2p="espeak")
    assert "[" not in block.spoken(g2p="espeak", phonemes=False)

    assert g2p_of("kokoro") == ("misaki", True)
    assert g2p_of("kokoro-onnx") == ("espeak", True)
    # An engine that declares neither is read as the only one there used to be.
    assert g2p_of(FakeEngine()) == ("misaki", True)


def test_the_onnx_engine_splices_a_rule_into_its_own_phonemes():
    """espeak has never heard of `[word](/ipa/)` and read it out. The engine
    phonemises the prose itself and puts the rule's phonemes in between."""
    from textcast.tts.kokoro_onnx import _RULE_IPA

    parts = _RULE_IPA.split("The [LIBOR](/lˈaɪbɔːɹ/) rate rose.")

    assert parts == ["The ", "LIBOR", "lˈaɪbɔːɹ", " rate rose."]
