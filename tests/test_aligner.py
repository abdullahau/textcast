"""`textcast.tts.aligner`: the alignment target text, and the Viterbi decoder
that finds a path through it.

Two kinds of test. First, the parts that never touch a model — spelling
digits out, the word-count parity `sourcemap.words()` must keep with the
aligner's own tokenisation — checked against every block of every page in
`tests/corpus`, because the composition layer downstream trusts a positional
zip between the two lists and a length mismatch silently mis-times every
word after the first one. Second, the Viterbi decoder itself, checked
against synthetic emissions with a known right answer rather than a real
model: deterministic, no network, no download, and the failure mode that
matters (the DP recovering the wrong path) is exactly as visible on four
made-up frames as on a real spectrogram.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

from textcast.document import Block, BlockKind
from textcast.ingest import parse_html
from textcast.sourcemap import words as sm_words
from textcast.tts.aligner import (
    SAMPLE_RATE,
    AlignmentError,
    _merge_repeats,
    _viterbi_align,
    _words_from_segments,
    spell_number,
    survives_tokenization,
    to_target_text,
)

CORPUS = Path(__file__).with_name("corpus")
PAGES = sorted(CORPUS.glob("*.html"))


# --------------------------------------------------------------------------
# spelling digits out for the alignment target
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("digits", "spelled"),
    [
        ("0", "zero"),
        ("4", "four"),
        ("12", "twelve"),
        ("40", "forty"),
        ("401", "four hundred one"),
        ("999", "nine hundred ninety nine"),
        ("1000", "one thousand"),
        ("4400000000000", "four trillion four hundred billion"),
    ],
)
def test_spell_number(digits, spelled):
    assert spell_number(digits) == spelled


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("The 4 point four trillion dollar company", "THE|FOUR|POINT|FOUR|TRILLION|DOLLAR|COMPANY"),
        # A bare year reads in pairs, not as one integer -- "twenty
        # nineteen", not "two thousand nineteen" -- and fused into one
        # aligner-word, matching sourcemap's own single "2019" token.
        ("Profits since 2019 rose", "PROFITS|SINCE|TWENTYNINETEEN|ROSE"),
        ("The year 2000 arrived", "THE|YEAR|TWOTHOUSAND|ARRIVED"),
        ("It's a 5 kilometer walk.", "IT'S|A|FIVE|KILOMETER|WALK"),
        # Not a year: three digits, so the generic reading applies and stays
        # fused into the one word "401" was.
        ("filed 401 forms", "FILED|FOURHUNDREDONE|FORMS"),
    ],
)
def test_to_target_text(text, target):
    assert to_target_text(text) == target


def test_a_punctuation_only_word_does_not_survive_tokenization():
    assert not survives_tokenization("...")
    assert not survives_tokenization("--")
    assert survives_tokenization("2019")
    assert survives_tokenization("it's")


# --------------------------------------------------------------------------
# word-count parity: sourcemap.words() vs. the aligner's own tokenisation
# --------------------------------------------------------------------------


def _parity(text: str, g2p: str = "misaki") -> tuple[list[str], list[str]]:
    block = Block(kind=BlockKind.PARA, text=text)
    tracked = block.spoken_tracked(g2p=g2p)
    sourcemap_words = [w for w, orig in sm_words(tracked) if survives_tokenization(w)]
    aligner_words = [w for w in to_target_text(tracked.text).split("|") if w]
    return sourcemap_words, aligner_words


@pytest.mark.parametrize(
    "text",
    [
        "The $4.4tn company raised its earnings guidance for Q3.",
        "Profits since 2019 rose 12 percent, filed 401 forms.",
        "It's a 5 kilometer walk.",
        "The deal closed in 2000 for $1 million.",
        "A claim [Footnote 1: a citation] follows $5bn strong.",
        "*Emphasis* survives, and so does an ellipsis ... standing alone.",
    ],
)
@pytest.mark.parametrize("g2p", ["misaki", "espeak"])
def test_word_counts_match_between_sourcemap_and_the_aligner(text, g2p):
    sourcemap_words, aligner_words = _parity(text, g2p)
    assert len(sourcemap_words) == len(aligner_words), (sourcemap_words, aligner_words)


@pytest.mark.skipif(not PAGES, reason="corpus not present")
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem[:30])
def test_word_counts_match_over_the_corpus(page):
    article = parse_html(page.read_text(encoding="utf-8", errors="replace"))
    for _section, block in article.blocks():
        for g2p in ("misaki", "espeak"):
            tracked = block.spoken_tracked(g2p=g2p)
            sourcemap_words = [w for w, orig in sm_words(tracked) if survives_tokenization(w)]
            aligner_words = [w for w in to_target_text(tracked.text).split("|") if w]
            assert len(sourcemap_words) == len(aligner_words), (
                block.id, g2p, sourcemap_words, aligner_words,
            )


# --------------------------------------------------------------------------
# the Viterbi decoder, against synthetic emissions with a known right answer
# --------------------------------------------------------------------------


def _emissions_favoring(frame_labels: list[int], vocab_size: int, sharp: float = 20.0) -> np.ndarray:
    """Log-probs where frame ``i`` overwhelmingly favours ``frame_labels[i]``.

    Not a real model's output shape (real logits are never this confident),
    but the decoder only has to find the best path through whatever it is
    given, so a lattice with one obvious answer is what proves it finds
    obvious answers -- the failure mode that matters here is a DP bug, not
    an acoustic one.
    """
    t = len(frame_labels)
    logits = np.zeros((t, vocab_size), dtype=np.float32)
    for i, label in enumerate(frame_labels):
        logits[i, label] = sharp
    log_probs = logits - np.log(np.sum(np.exp(logits), axis=-1, keepdims=True))
    return log_probs.astype(np.float32)


def test_viterbi_recovers_an_unambiguous_path():
    # blank=0, A=1, B=2. Two blank frames, two of A, one blank, three of B.
    frames = [0, 0, 1, 1, 0, 2, 2, 2]
    log_probs = _emissions_favoring(frames, vocab_size=3)
    path = _viterbi_align(log_probs, np.array([1, 2]), blank=0)
    assert list(path) == frames


def test_viterbi_requires_a_blank_between_two_equal_adjacent_letters():
    # Target "A A" needs a blank between the two, or two adjacent frames of
    # the same label collapse (CTC's own rule) into what reads as one A.
    frames = [1, 1, 0, 1, 1]
    log_probs = _emissions_favoring(frames, vocab_size=2)
    path = _viterbi_align(log_probs, np.array([1, 1]), blank=0)
    segments = _merge_repeats(path, {0: "<pad>", 1: "A"})
    letters = [label for label, _s, _e in segments if label != "<pad>"]
    assert letters == ["A", "A"]


def test_viterbi_raises_rather_than_guessing_when_no_path_fits():
    # Three frames cannot spell out five target characters even with every
    # skip available -- too little audio for too much text.
    log_probs = _emissions_favoring([0, 1, 2], vocab_size=4)
    with pytest.raises(AlignmentError):
        _viterbi_align(log_probs, np.array([1, 2, 3, 1, 2]), blank=0)


class RampSession:
    """A stand-in for the ONNX session, with a known right answer.

    One frame per 320 samples, and each frame votes for the quarter of the
    0..1 ramp its own samples sit in. Run over a ramp, the argmax sequence
    that comes back must climb 0,1,2,3 and never go backwards -- so a frame
    returned twice, or a stretch of a window trimmed that should not have
    been, shows up as a step down.
    """

    stride = 320

    def run(self, names, feed):
        chunk = feed["input_values"][0]
        n = len(chunk) // self.stride
        out = np.full((max(n, 1), 4), -10.0, dtype=np.float32)
        for i in range(n):
            mean = float(chunk[i * self.stride : (i + 1) * self.stride].mean())
            out[i, min(3, max(0, int(mean * 4)))] = 10.0
        return [out[None, ...]]


def test_windowing_hands_back_each_stretch_of_the_clip_once():
    """The multi-window path never trimmed the context *between* two
    windows -- it is interior to the concatenation, and only the two ends
    were cut -- and measured frames-per-second by counting the run's context
    once for the whole run rather than once per window, 1.5x out at two
    windows. Both were silent: the decode still found a path, and the block
    got wrong timings rather than falling back."""
    from textcast.tts.aligner import _generate_emissions

    waveform = np.linspace(0.0, 1.0, 6 * SAMPLE_RATE, dtype=np.float32)
    emissions, stride_ms = _generate_emissions(RampSession(), waveform, window_length=2.0)

    steps = np.argmax(emissions, axis=1)
    assert np.all(np.diff(steps) >= 0), "the clip came back out of order or twice over"
    assert set(steps.tolist()) == {0, 1, 2, 3}, "a stretch of the clip went missing"
    assert 15.0 < stride_ms < 25.0, stride_ms


def test_a_clip_that_fits_one_window_is_never_split_across_two():
    """`int(len(waveform) / SAMPLE_RATE * SAMPLE_RATE)` does not round-trip.
    For about one clip length in a hundred it came back one sample short, so
    `extension` became nearly a whole window and a clip that fits in one
    window was split across two -- into the path above, which then mistimed
    it."""
    from textcast.tts.aligner import _generate_emissions

    session = RampSession()
    for samples in (2 * SAMPLE_RATE + 1, 2 * SAMPLE_RATE + 4, 3 * SAMPLE_RATE + 7):
        calls = []
        session.run = lambda names, feed, _c=calls: (
            _c.append(1), RampSession.run(session, names, feed)
        )[1]
        emissions, _stride = _generate_emissions(
            session, np.linspace(0.0, 1.0, samples, dtype=np.float32)
        )
        assert len(calls) == 1, f"{samples} samples ran the model {len(calls)} times"
        steps = np.argmax(emissions, axis=1)
        assert np.all(np.diff(steps) >= 0)
        del session.run


def test_viterbi_keeps_one_row_of_scores_not_one_per_frame():
    """The scores used to be a (frames x states) float64 array and nothing
    read a row of it but the one before. Frames and characters both scale
    with a block's duration, so it was quadratic in it: the longest block in
    a real library wanted 370 MB, and `align_article` runs four decodes at
    once. The path must not change, only what it costs to find."""
    import tracemalloc

    rng = np.random.default_rng(11)
    frames, states = 1200, 180
    logits = rng.normal(size=(frames, 32))
    log_probs = (logits - np.log(np.sum(np.exp(logits), axis=-1, keepdims=True))).astype(np.float32)
    targets = rng.integers(1, 32, size=states)

    tracemalloc.start()
    path = _viterbi_align(log_probs, targets, blank=0)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert len(path) == frames
    # One byte a cell for the backpointers, plus slack for the working rows.
    # The old shape would need eight times that for the scores alone.
    assert peak < frames * (2 * states + 1) * 3, f"peak was {peak} bytes"


def test_viterbi_refuses_a_block_too_long_to_decode_rather_than_running_out():
    """The backpointers are still quadratic in duration, just eight times
    smaller. A block long enough to matter keeps block-level highlighting;
    the alternative is the OOM killer taking the whole align process."""
    from textcast.tts import aligner as aligner_module

    log_probs = _emissions_favoring([0, 1, 0, 2, 0], vocab_size=4)
    with pytest.raises(AlignmentError, match="too long to align"):
        with _lattice_cap(aligner_module, 4):
            _viterbi_align(log_probs, np.array([1, 2]), blank=0)


@contextmanager
def _lattice_cap(module, cells: int):
    was = module.MAX_LATTICE_CELLS
    module.MAX_LATTICE_CELLS = cells
    try:
        yield
    finally:
        module.MAX_LATTICE_CELLS = was


def test_merge_repeats_and_words_from_segments_split_on_the_boundary_token():
    id_to_token = {0: "<pad>", 1: "T", 2: "H", 3: "E", 4: "|", 5: "A"}
    # "THE" then "|" then "A", each letter held for a couple of frames.
    path = np.array([1, 1, 2, 3, 3, 4, 4, 0, 5])
    segments = _merge_repeats(path, id_to_token)
    words = _words_from_segments(segments, stride_ms=20.0, blank_token="<pad>")
    assert [w.text for w in words] == ["THE", "A"]
    assert words[0].start_ms == 0
    assert words[1].start_ms > words[0].end_ms
