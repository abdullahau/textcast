"""The contract every TTS engine implements.

Engines keep their own native sample rate. Nothing upstream resamples; the
encoder is told the rate and lets ffmpeg handle it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    gender: str | None = None
    lang: str = "en"


@dataclass
class Clip:
    """Mono float32 audio in [-1, 1]."""

    samples: np.ndarray
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate

    @property
    def duration_ms(self) -> int:
        return round(self.duration_s * 1000)


@runtime_checkable
class TTSEngine(Protocol):
    """Implemented by every engine in this package.

    Engine-specific knobs (Kokoro's voice blending, Supertonic's step count)
    belong in the constructor, never in ``synthesize``. That keeps this call
    signature identical across engines so callers stay engine-agnostic.
    """

    name: str
    sample_rate: int

    def voices(self) -> list[Voice]: ...

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0, lang: str = "en") -> Clip: ...


@dataclass
class EngineSpec:
    """How to build one engine, without importing its dependencies."""

    name: str
    module: str
    cls: str
    extra: str
    description: str
    default_voice: str
    options: dict = field(default_factory=dict)


def silence(sample_rate: int, ms: int) -> np.ndarray:
    return np.zeros(round(sample_rate * ms / 1000), dtype=np.float32)
