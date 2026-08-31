"""Supertonic 3 — the default engine.

99M parameters over ONNX Runtime, 44.1 kHz output, no espeak-ng. ``steps``
trades quality against speed; measured on a 4-core Neoverse-N1 with no GPU,
RTF runs 0.20 at 2 steps, 0.31 at 4, and 0.49 at 8.
"""

from __future__ import annotations

import threading

import numpy as np

from .base import Clip, Voice

_GENDER = {"F": "female", "M": "male"}


class SupertonicEngine:
    name = "supertonic"

    def __init__(
        self,
        model: str = "supertonic-3",
        steps: int = 4,
        model_dir: str | None = None,
        threads: int | None = None,
        **_ignored,
    ) -> None:
        from supertonic import TTS

        self.steps = steps
        self._tts = TTS(
            model=model,
            model_dir=model_dir,
            auto_download=True,
            intra_op_num_threads=threads,
        )
        self.sample_rate = int(self._tts.sample_rate)
        self._styles: dict[str, object] = {}
        # ONNX Runtime sessions are not safe to drive from several threads at
        # once; the worker and a web request can both land here.
        self._lock = threading.Lock()

    def voices(self) -> list[Voice]:
        return [
            Voice(id=v, name=f"Supertonic {v}", gender=_GENDER.get(v[0]), lang="multi")
            for v in self._tts.voice_style_names
        ]

    def _style(self, voice: str):
        if voice not in self._styles:
            self._styles[voice] = self._tts.get_voice_style(voice_name=voice)
        return self._styles[voice]

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        lang: str = "en",
    ) -> Clip:
        voice = voice or "M1"
        with self._lock:
            wav, _duration = self._tts.synthesize(
                text,
                voice_style=self._style(voice),
                total_steps=self.steps,
                # The SDK treats 1.05 as neutral pace, so scale around that.
                speed=1.05 * speed,
                lang=lang,
            )
        samples = np.asarray(wav, dtype=np.float32).squeeze()
        return Clip(samples=samples, sample_rate=self.sample_rate)
