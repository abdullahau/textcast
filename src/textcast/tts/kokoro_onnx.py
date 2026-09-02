"""Kokoro-82M, again — the same voices through onnxruntime instead of torch.

The weights are the same v1.0 checkpoint the PyTorch engine loads, exported to
ONNX by `thewh1teagle/kokoro-onnx`. What differs is everything around them:

* **No torch.** onnxruntime, protobuf and flatbuffers are about 40 MB of
  wheels against torch's 1.4 GB, and there is no spaCy and no transformers.
* **A different G2P.** kokoro reaches espeak through misaki, which knows
  American gold and silver dictionaries and spells acronyms out well. This
  reaches espeak through phonemizer directly. Every pronunciation rule in this
  project was measured against misaki, so the same text can come out
  differently here — that is the thing to listen for, not the speed.
* **Its own model files.** They are published as a GitHub release rather than
  a hub repo, so they are not baked into the image and not downloaded on
  demand. Put them in ``data/models`` and this engine appears; leave them out
  and it says so.

The voices carry the same ids as the PyTorch engine's, so the display name
says which is which. An article built with one and an article built with the
other must never look like the same choice.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import weakref
from pathlib import Path

import numpy as np

from .base import Clip, Voice

log = logging.getLogger("textcast.tts.kokoro_onnx")

#: What the engine is called everywhere a person can see it.
LABEL = "ONNX"

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

#: Where the release publishes them, for the message when they are missing.
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

#: The English half of the v1.0 pack, in the same order the PyTorch engine
#: lists it. The bin file carries 54 voices across nine languages; this
#: project reads English.
_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

_LANGS = {"a": "en-us", "b": "en-gb"}

#: A pronunciation rule may carry IPA, which ``pronounce.py`` emits as
#: ``[word](/ipa/)``. That is *misaki's* markup: misaki reads it and hands the
#: phonemes to the model. espeak has never heard of it and read the whole
#: thing aloud — "[LIBOR](/lˈIbɔɹ/)" came out as "libber slash el stress eye
#: bee open-or turned-ar slash", 3.7 s of audio for two words.
#:
#: So this engine does misaki's job itself: it phonemises the plain stretches
#: and splices the rule's phonemes in between, then hands the whole string to
#: the model as phonemes. The two engines share one phoneme vocabulary — 114
#: symbols, checked — so a rule's IPA is valid input either way.
_RULE_IPA = re.compile(r"\[([^\]]+)\]\(/([^)]*)/\)")


def strip_ipa_markup(text: str) -> str:
    """Take ``[word](/ipa/)`` back to ``word``, for anything that only reads."""
    return _RULE_IPA.sub(r"\1", text)


def model_paths(models_dir: Path | None = None) -> tuple[Path, Path]:
    """Where the two files are, environment first, then the data directory."""
    if models_dir is None:
        from ..settings import get_settings

        models_dir = get_settings().models_dir

    model = os.environ.get("TEXTCAST_KOKORO_ONNX_MODEL", "").strip()
    voices = os.environ.get("TEXTCAST_KOKORO_ONNX_VOICES", "").strip()
    return (
        Path(model) if model else models_dir / MODEL_FILE,
        Path(voices) if voices else models_dir / VOICES_FILE,
    )


def voices(lang_code: str = "a") -> list[Voice]:
    """The voice list, without loading the model.

    Every name carries the label. The ids are identical to the PyTorch
    engine's, so a picker showing both would otherwise offer "Heart" twice.
    """
    return [
        Voice(
            id=v,
            name=f"{v.split('_', 1)[1].title()} ({LABEL})",
            gender="female" if v[1] == "f" else "male",
            lang=_LANGS.get(v[0], "en-us"),
        )
        for v in _VOICES
        if v[0] == lang_code
    ]


#: One onnxruntime session behind every instance in a process, held weakly.
#: The same trade the PyTorch engine makes with ``KModel``: the pool wants
#: four instances for four cores, but four *copies of the weights* is 311 MB
#: each for nothing. onnxruntime's ``Run`` is thread-safe, so one session
#: serves them all.
#:
#: Weak on purpose, so this registry is never what keeps the weights resident
#: after the worker drops its pool.
_sessions: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_sessions_lock = threading.Lock()


def shared_session(model: Path, threads: int | None = None):
    """The onnxruntime session for this file, built once per process."""
    import onnxruntime

    key = f"{model}:{threads or 0}"
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            options = onnxruntime.SessionOptions()
            if threads:
                # One core each; the pool provides the parallelism.
                options.intra_op_num_threads = threads
                options.inter_op_num_threads = threads
            session = onnxruntime.InferenceSession(str(model), sess_options=options)
            _sessions[key] = session
        return session


class KokoroOnnxEngine:
    name = "kokoro-onnx"
    sample_rate = 24000
    #: espeak, not misaki, so a phoneme rule needs its espeak spelling. The
    #: markup itself is handled here rather than by the G2P.
    g2p = "espeak"
    accepts_phonemes = True

    def __init__(
        self,
        lang_code: str = "a",
        threads: int | None = None,
        models_dir: Path | None = None,
        **_ignored,
    ) -> None:
        model, voices_file = model_paths(models_dir)
        missing = [str(p) for p in (model, voices_file) if not p.exists()]
        if missing:
            raise FileNotFoundError(
                f"kokoro-onnx needs its model files: {', '.join(missing)}. "
                f"Download {MODEL_FILE} and {VOICES_FILE} from {RELEASE}"
            )

        from kokoro_onnx import Kokoro

        self.lang_code = lang_code
        self.language = _LANGS.get(lang_code, "en-us")
        # The instances differ in their tokenizer and voice cache, not in
        # their weights. onnxruntime spreads over every core unless told
        # otherwise; the build pool says threads=1 and takes its parallelism
        # from having four instances.
        self._session = shared_session(model, threads)
        self._kokoro = Kokoro.from_session(self._session, str(voices_file))
        self._tok = None
        # The session takes concurrent calls, but the wrapper around it keeps
        # state through a batch, so one call at a time per instance.
        self._lock = threading.Lock()

    def voices(self) -> list[Voice]:
        return voices(self.lang_code)

    def _tokenizer(self):
        from kokoro_onnx.tokenizer import Tokenizer

        if self._tok is None:
            self._tok = Tokenizer()
        return self._tok

    def phonemes(self, text: str, voice: str | None = None) -> str:
        """What the model will actually be given, for the pronunciation page.

        Not the same answer the PyTorch engine gives: that one asks misaki,
        which has its own dictionaries in front of espeak. A phoneme rule's
        own IPA passes through untouched, exactly as it will at synthesis.
        """
        return self._phonemise(text)

    def _phonemise(self, text: str) -> str:
        """espeak for the prose, the rule's own phonemes where a rule fired.

        The stretches around a rule are phonemised on their own, so espeak
        loses the words either side as context. That is the same bargain
        misaki makes when it hands verbatim IPA through, and it is only ever a
        word the author has already said the engine gets wrong.
        """
        parts = _RULE_IPA.split(text)
        if len(parts) == 1:
            return self._tokenizer().phonemize(text, lang=self.language)

        # re.split with two groups yields plain, word, ipa, plain, word, ipa...
        out: list[str] = []
        for index in range(0, len(parts), 3):
            plain = parts[index]
            if plain:
                out.append(self._tokenizer().phonemize(plain, lang=self.language))
            if index + 2 < len(parts):
                # A rule with no espeak spelling reaches here with an empty
                # target; then the word itself is phonemised, as if no rule.
                ipa = parts[index + 2].strip()
                out.append(ipa or self._tokenizer().phonemize(parts[index + 1], lang=self.language))
        # Phonemised separately, the pieces have no space between them, and a
        # space is a word boundary to the model.
        return " ".join(piece for piece in out if piece)

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        lang: str = "en",
    ) -> Clip:
        with self._lock:
            samples, rate = self._kokoro.create(
                self._phonemise(text), voice=voice or "af_heart",
                speed=speed, lang=self.language, is_phonemes=True,
            )
        samples = np.asarray(samples, dtype=np.float32)
        if rate != self.sample_rate:
            # Nothing upstream resamples, so a surprise here would be silent
            # and wrong. The export is 24 kHz; say so if it ever is not.
            raise ValueError(f"kokoro-onnx returned {rate} Hz, not {self.sample_rate}")
        return Clip(samples=samples, sample_rate=self.sample_rate)
