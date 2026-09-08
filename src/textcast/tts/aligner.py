"""Word-level forced alignment: known text, known audio, unknown timing.

Runs after a build's own synthesis is done and its engine pool released —
see `jobs.py` and
`docs/superpowers/specs/2026-09-08-word-level-highlighting-design.md` for
why this is its own phase rather than folded into synthesis. It reads a
block's audio straight back out of the block cache; nothing here
resynthesizes anything.

The model is `wav2vec2-base-960h` (Meta, Apache-2.0), exported to ONNX and
quantized to int8 by the `onnx-community` project on Hugging Face — not
`mms-300m-1130-forced-aligner`, the default of the `ctc-forced-aligner`
package this was prototyped against: a third the parameters, an
unrestricted licence, and — once the window a clip is padded to before
encoding is sized to the clip itself rather than left at that package's
30-second default — comfortably faster than real time. Only two ideas were
worth keeping from that package: the windowing scheme, and grouping a raw
per-frame path into runs. Both are under twenty lines, so they are
reimplemented here directly against `onnxruntime` — already a dependency of
the ONNX engine — rather than installing a whole extra package (`librosa`,
`numba`, `scikit-learn`, `scipy`) to reach a compiled Viterbi decoder this
module writes for itself in plain numpy instead: at the token counts one
block ever produces, a few hundred vectorized steps is not something that
needs a C extension.

Nothing here imports `onnxruntime` or fetches anything at module load.
`Aligner()` is built once per build job, inside the same child process
synthesis already ran in — the parent worker and the web app never
construct one. See `docs/traps.md`, "The parent must stay clean."
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..normalize import YEAR_WORDS, _year_words

log = logging.getLogger("textcast.tts.aligner")

MODEL_URL = (
    "https://huggingface.co/onnx-community/wav2vec2-base-960h-ONNX"
    "/resolve/main/onnx/model_int8.onnx"
)
VOCAB_URL = "https://huggingface.co/onnx-community/wav2vec2-base-960h-ONNX/resolve/main/vocab.json"

#: Fixed by the model's own architecture — the total downsampling of its
#: convolutional feature extractor — not a choice made here. Used only as a
#: sanity fallback; the real stride is always measured from a run's own
#: output shape, the same as the package this was prototyped against did,
#: because windowing arithmetic can shift it by a frame either way.
SAMPLE_RATE = 16000
NOMINAL_STRIDE_MS = 20.0

#: The most lattice the Viterbi decode may allocate for one block, one byte
#: a cell. Frames and characters both scale with the block's duration, so the
#: lattice is quadratic in it, and `audio.align_article` runs four decodes at
#: once. 128 MB is about five minutes of unbroken speech in a single block --
#: two and a half times the longest one in a real library, and well short of
#: what a small box can lose to four of them at once. A block over it keeps
#: block-level highlighting; see `_viterbi_align`.
MAX_LATTICE_CELLS = 128 << 20


@dataclass(frozen=True)
class AlignedWord:
    #: As the aligner tokenised it — uppercase, punctuation stripped. The
    #: caller zips this positionally against `sourcemap.words()`, which is
    #: why both sides must tokenise the exact same string; see `Aligner.align`.
    text: str
    start_ms: int
    end_ms: int


class AlignmentError(RuntimeError):
    """The model could not align this audio against this text.

    Caught by the caller and treated as "this block keeps block-level
    highlighting" — never as a reason to fail a build. See the design doc's
    degradation section.
    """


def _ensure_file(path: Path, url: str) -> None:
    if path.exists():
        return
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    # The writer's own name, not the final one — two build jobs racing to
    # fetch the same model for the first time must not hand each other a
    # half-written file. Same reason `audio._speak`'s cache write does this.
    tmp = path.with_suffix(f".{os.getpid()}.part")
    log.info("fetching %s", url)
    with requests.get(url, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(path)


_NOT_TARGET_CHARS = re.compile(r"[^A-Za-z' ]")
_WHITESPACE = re.compile(r"\s+")
_DIGIT_RUN = re.compile(r"\d+")

_ONES_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS_WORDS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCALE_WORDS = ("", "thousand", "million", "billion", "trillion")


def _spell_hundreds(n: int) -> list[str]:
    words = []
    if n >= 100:
        words += [_ONES_WORDS[n // 100], "hundred"]
        n %= 100
    if n >= 20:
        words.append(_TENS_WORDS[n // 10])
        if n % 10:
            words.append(_ONES_WORDS[n % 10])
    elif n > 0:
        words.append(_ONES_WORDS[n])
    return words


def spell_number(digits: str) -> str:
    """An integer as English words — for the alignment target only.

    ``normalize`` deliberately leaves some numbers as raw digits — the whole
    part of a spelled-out decimal, an ordinary quantity, a year under
    misaki's g2p — and relies on the engine's own number reading rather than
    spelling every one out itself. That is right for what gets spoken, and
    wrong for what gets aligned against: a digit run has no letters, so it
    would otherwise vanish from the alignment target entirely rather than
    merely being imprecise, corrupting every word after it in that block.
    This never changes what is spoken; see `Aligner.align`.
    """
    try:
        n = int(digits)
    except ValueError:
        return digits
    if n == 0:
        return "zero"
    if n < 0:
        return "negative " + spell_number(str(-n))

    groups: list[str] = []
    scale = 0
    remaining = n
    while remaining > 0 and scale < len(_SCALE_WORDS):
        remaining, chunk = divmod(remaining, 1000)
        if chunk:
            words = _spell_hundreds(chunk)
            if _SCALE_WORDS[scale]:
                words.append(_SCALE_WORDS[scale])
            groups.append(" ".join(words))
        scale += 1
    if remaining > 0:
        # Beyond what this table names a scale for — implausibly rare in
        # this app's own writing. Digit by digit rather than the raw string:
        # `to_target_text` strips anything that is not a letter, and an
        # empty replacement would vanish this word from the aligner's list
        # entirely rather than merely being an imprecise reading of it.
        return " ".join(_ONES_WORDS[int(d)] for d in digits)
    return " ".join(reversed(groups))


def _spell_digit_run(match: re.Match) -> str:
    digits = match.group(0)
    # A bare year reads in pairs ("twenty nineteen"), not as one integer
    # ("two thousand nineteen") — the same range `normalize.YEAR_WORDS`
    # uses, reused rather than re-derived so the two cannot silently
    # disagree about what counts as a year. Under espeak's g2p this pattern
    # never matches anything: `normalize` already spelled the year out
    # before `Block.spoken()` returned, so there is no bare run left here to
    # confuse with a plain quantity that happens to be four digits long.
    year_match = YEAR_WORDS.fullmatch(digits) if len(digits) == 4 else None
    spelled = _year_words(year_match) if year_match else spell_number(digits)
    # Fused, not space-joined: `sourcemap.words()` tokenised the source text
    # with "2019" as *one* word, and the aligner's word list must come back
    # the same length or the caller's positional zip lines up the wrong
    # timestamps with the wrong displayed words. "TWENTYNINETEEN" gives the
    # acoustic model a real letter sequence to search for without asking it
    # to find an internal boundary nothing downstream needs split further.
    return spelled.replace(" ", "")


def survives_tokenization(word: str) -> bool:
    """Whether one already-spoken word leaves anything for the aligner to see.

    A digit run always survives — `_spell_digit_run` never returns empty —
    but a word built entirely from punctuation (the ellipsis either side of
    a footnote insertion, mainly) strips to nothing and vanishes from the
    aligner's word list rather than appearing as an empty slot. The
    composition step that zips `sourcemap.words()` against `Aligner.align`'s
    output must filter with this same predicate first, or the two lists
    come back different lengths and every word after the first mismatch
    gets the wrong timestamp.
    """
    return bool(to_target_text(word))


def to_target_text(text: str) -> str:
    """Uppercase, punctuation stripped, whitespace collapsed to one ``|`` each.

    The model's own convention: it predicts ``|`` as an explicit word
    boundary, so alignment needs nothing cleverer than splitting the merged
    per-frame path on that one symbol — unlike MMS_FA, which has no such
    symbol and must recover word boundaries from the known length of each
    target word instead.
    """
    text = _DIGIT_RUN.sub(_spell_digit_run, text)
    text = _NOT_TARGET_CHARS.sub("", text.upper())
    return _WHITESPACE.sub("|", text.strip())


class Aligner:
    """One loaded model. Construct once per build job; never inside a request."""

    def __init__(self, model_path: Path, vocab_path: Path):
        import onnxruntime

        _ensure_file(model_path, MODEL_URL)
        _ensure_file(vocab_path, VOCAB_URL)

        with open(vocab_path) as f:
            self.vocab: dict[str, int] = json.load(f)
        self.id_to_token = {v: k for k, v in self.vocab.items()}
        self.blank_id = self.vocab["<pad>"]
        # Single-threaded *inside* one call, on purpose: `audio.align_article`
        # gets its parallelism by calling this session from several Python
        # threads at once, one block each -- 3.7x on four cores, measured,
        # because the session's own inference releases the GIL. Letting
        # intra-op parallelism run too would have each of those concurrent
        # calls competing for the same cores from underneath, and a single
        # call was measured to gain nothing from extra intra-op threads
        # anyway (this box is bound by memory bandwidth more than cores,
        # the same finding `docs/decisions.md` already has for the TTS
        # engines) -- so the only threading that should exist here is the
        # caller's.
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(str(model_path), sess_options=opts)

    def align(self, waveform: np.ndarray, sample_rate: int, text: str) -> list[AlignedWord]:
        """Word-level timestamps for ``text`` against ``waveform``.

        ``waveform`` is resampled to the model's own 16 kHz first — every
        engine here renders at a different rate, and none of them is 16 kHz.
        Raises `AlignmentError` rather than returning a wrong answer; the
        caller decides what "no words for this block" means.
        """
        target = to_target_text(text)
        chars = [c for c in target if c in self.vocab]
        if not chars:
            return []

        if sample_rate != SAMPLE_RATE:
            waveform = _resample(waveform, sample_rate, SAMPLE_RATE)
        if not len(waveform):
            return []

        emissions, stride_ms = _generate_emissions(self.session, waveform)
        targets = np.asarray([self.vocab[c] for c in chars], dtype=np.int64)
        try:
            path = _viterbi_align(emissions, targets, self.blank_id)
        except Exception as exc:  # noqa: BLE001 -- see AlignmentError's own docstring
            raise AlignmentError(f"could not align {len(chars)} characters: {exc}") from exc
        segments = _merge_repeats(path, self.id_to_token)
        return _words_from_segments(segments, stride_ms, blank_token=self.id_to_token[self.blank_id])


def _resample(waveform: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Linear resample.

    Forced alignment tolerates far more resampling error than free
    transcription would: the token sequence is already known and fixed, so
    the search only has to find *where* it happened, not *what* was said. A
    proper polyphase resample was measured against this and not worth a new
    dependency (`scipy`) for what a listener's timing tolerance never
    notices — see the design doc's measurement section.
    """
    if from_rate == to_rate:
        return waveform.astype(np.float32)
    duration = len(waveform) / from_rate
    n_out = max(1, round(duration * to_rate))
    x_old = np.linspace(0, duration, num=len(waveform), endpoint=False)
    x_new = np.linspace(0, duration, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, waveform).astype(np.float32)


def _generate_emissions(
    session, waveform: np.ndarray, window_length: float | None = None, context_s: float = 2.0
) -> tuple[np.ndarray, float]:
    """Run the model over ``waveform``, windowed to the clip's own length.

    ``window_length`` defaults to the clip's own duration (a two-second
    floor, so a one-word block still gets the model's expected minimum
    context) rather than a fixed constant either way: the alignment-cost
    spike behind this module found the package's own 30-second default pads
    a five-second block up to encoding 34 seconds of mostly silence, and
    that a window much *smaller* than the clip re-pays its own 2-second
    context on every extra window it needs, which is worse than one bigger
    window for anything over a few seconds.
    """
    duration = len(waveform) / SAMPLE_RATE
    window_length = window_length or max(2.0, duration)
    context = int(context_s * SAMPLE_RATE)
    window = int(window_length * SAMPLE_RATE)

    extension = (-len(waveform)) % window if window else 0
    padded = np.pad(waveform, (context, context + extension), mode="constant")
    n_windows = max(1, (len(padded) - 2 * context) // window)

    frames = []
    for i in range(n_windows):
        chunk = padded[i * window : i * window + window + 2 * context]
        outputs = session.run(["logits"], {"input_values": chunk[None, :].astype(np.float32)})
        frames.append(outputs[0][0])
    emissions = np.concatenate(frames, axis=0)

    # Measured from this run's own output shape, not assumed from
    # `NOMINAL_STRIDE_MS`: windowing arithmetic (the padding, the context)
    # can shift the frame count by one or two either way, and a stride off
    # by even a few percent drifts a long block's timings visibly.
    total_audio_s = window_length * n_windows + 2 * context_s
    frames_per_sec = emissions.shape[0] / total_audio_s if total_audio_s else 0
    context_frames = round(context_s * frames_per_sec)
    if context_frames:
        emissions = emissions[context_frames:-context_frames]
    extension_frames = round(extension / SAMPLE_RATE * frames_per_sec)
    if extension_frames:
        emissions = emissions[:-extension_frames]

    if not emissions.shape[0]:
        raise AlignmentError("windowing produced no frames")
    stride_ms = len(waveform) * 1000 / emissions.shape[0] / SAMPLE_RATE
    log_probs = emissions - np.log(np.sum(np.exp(emissions), axis=-1, keepdims=True))
    return log_probs.astype(np.float32), stride_ms


def _viterbi_align(log_probs: np.ndarray, targets: np.ndarray, blank: int) -> np.ndarray:
    """The best single path through ``log_probs`` that spells out ``targets``.

    A plain numpy Viterbi over the CTC lattice: the extended target sequence
    interleaves a blank before, between and after every real token, and each
    state may stay, advance from the state before it, or — skipping a
    single blank — advance from two states before it, when doing so does
    not run two equal tokens together with no blank to keep them apart. The
    inner state dimension is vectorized; only the T (frame) dimension is a
    Python loop, which is what keeps this fast enough without a compiled
    extension at the token counts one block ever produces.

    Only the backpointers are kept for every frame. The scores were too, in
    a (frames x states) float64 array, and nothing ever read a row of it but
    the one before — so the cost of a long block was eight bytes per lattice
    cell where one will do. Frames and characters both scale with duration,
    so that array was quadratic in it: the longest block in one real library
    (2,684 characters, about three minutes of speech) wanted 370 MB, and
    `audio.align_article` runs four of these at once over one shared session.
    A single row costs nothing and the answer is identical.
    """
    t_total = log_probs.shape[0]
    targets = np.asarray(targets, dtype=np.int64)
    s_total = len(targets)
    ext = np.empty(2 * s_total + 1, dtype=np.int64)
    ext[0::2] = blank
    if s_total:
        ext[1::2] = targets
    length = len(ext)

    if t_total < 1:
        raise AlignmentError("no frames to align against")
    # What is left after the row above is still quadratic, just eight times
    # smaller. A block long enough to matter is one nobody wrote; refusing it
    # costs that block its word highlighting and costs the build nothing,
    # which is the whole degradation contract. See AlignmentError.
    if t_total * length > MAX_LATTICE_CELLS:
        raise AlignmentError(
            f"too long to align: {t_total} frames over {length} states "
            f"needs more than {MAX_LATTICE_CELLS >> 20} MB"
        )

    neg_inf = -1e9
    backptr = np.zeros((t_total, length), dtype=np.int8)

    alpha = np.full(length, neg_inf, dtype=np.float64)
    alpha[0] = log_probs[0, blank]
    if length > 1:
        alpha[1] = log_probs[0, ext[1]]

    can_skip = np.zeros(length, dtype=bool)
    can_skip[2:] = (ext[2:] != blank) & (ext[2:] != ext[:-2])

    for t in range(1, t_total):
        stay = alpha
        step1 = np.concatenate(([neg_inf], alpha[:-1]))
        if length > 1:
            step2 = np.concatenate(([neg_inf, neg_inf], alpha[:-2]))
            step2 = np.where(can_skip, step2, neg_inf)
            stacked = np.stack([stay, step1, step2])
        else:
            stacked = np.stack([stay, step1])
        choice = np.argmax(stacked, axis=0)
        best = np.take_along_axis(stacked, choice[None, :], axis=0)[0]
        backptr[t] = choice
        alpha = best + log_probs[t, ext]

    # A valid path must end on the final blank or the final real token —
    # anywhere else means the target sequence was not fully consumed.
    end = length - 1 if length == 1 else (length - 2) + int(np.argmax(alpha[-2:]))
    if alpha[end] <= neg_inf / 2:
        raise AlignmentError("no valid path reached the end of the target sequence")

    states = np.empty(t_total, dtype=np.int64)
    states[t_total - 1] = end
    for t in range(t_total - 1, 0, -1):
        states[t - 1] = states[t] - backptr[t, states[t]]

    return ext[states]


def _merge_repeats(path: np.ndarray, id_to_token: dict[int, str]) -> list[tuple[str, int, int]]:
    """Collapse a raw per-frame path into ``(token, start_frame, end_frame)`` runs."""
    segments: list[tuple[str, int, int]] = []
    i, n = 0, len(path)
    while i < n:
        j = i + 1
        while j < n and path[j] == path[i]:
            j += 1
        segments.append((id_to_token[int(path[i])], i, j - 1))
        i = j
    return segments


def _words_from_segments(
    segments: list[tuple[str, int, int]], stride_ms: float, blank_token: str
) -> list[AlignedWord]:
    """Group runs on the ``|`` word-boundary token into `AlignedWord`s."""
    words: list[AlignedWord] = []
    current: list[tuple[str, int, int]] = []

    def flush() -> None:
        if not current:
            return
        text = "".join(label for label, _s, _e in current)
        start_ms = round(current[0][1] * stride_ms)
        end_ms = round((current[-1][2] + 1) * stride_ms)
        words.append(AlignedWord(text=text, start_ms=start_ms, end_ms=end_ms))
        current.clear()

    for label, start, end in segments:
        if label == blank_token:
            continue
        if label == "|":
            flush()
        else:
            current.append((label, start, end))
    flush()
    return words
