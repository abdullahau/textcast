"""Kokoro-82M — kept as a swappable alternative.

Slower than Supertonic here (RTF 0.65 against 0.31) and it needs the espeak-ng
shared library plus its phoneme data on disk, but it offers 54 voices. If
espeak-ng lives somewhere unusual, point ``ESPEAK_DATA_PATH`` and
``PHONEMIZER_ESPEAK_LIBRARY`` at it before the first synthesis.
"""

from __future__ import annotations

import os
import threading

import numpy as np

from .base import Clip, Voice

# Kokoro's language codes, keyed by the code its pipeline expects.
_LANGS = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "zh",
}

# The v1.0 voice pack. Prefix: first letter language, second letter gender.
_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M", lang_code: str = "a", **_ignored) -> None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        from kokoro import KPipeline

        self.lang_code = lang_code
        self._pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id)
        self._lock = threading.Lock()

    def voices(self) -> list[Voice]:
        out = []
        for v in _VOICES:
            lang, gender = v[0], v[1]
            if lang != self.lang_code:
                continue
            out.append(
                Voice(
                    id=v,
                    name=v.split("_", 1)[1].title(),
                    gender="female" if gender == "f" else "male",
                    lang=_LANGS.get(lang, "en-us"),
                )
            )
        return out

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        lang: str = "en",
    ) -> Clip:
        import torch

        voice = voice or "af_heart"
        with self._lock:
            parts = [
                audio
                for _graphemes, _phonemes, audio in self._pipeline(text, voice=voice, speed=speed)
                if audio is not None and len(audio) > 0
            ]
        if not parts:
            return Clip(samples=np.zeros(0, dtype=np.float32), sample_rate=self.sample_rate)

        samples = torch.cat(parts).squeeze().cpu().numpy().astype(np.float32)
        return Clip(samples=samples, sample_rate=self.sample_rate)
