"""Engine registry.

Engines are declared here but imported lazily, so installing one extra
(``uv sync --extra supertonic``) never drags in the other's dependencies.
Kokoro pulls PyTorch and spaCy; Supertonic pulls only ONNX Runtime.
"""

from __future__ import annotations

import importlib

from .base import Clip, EngineSpec, TTSEngine, Voice, silence

ENGINES: dict[str, EngineSpec] = {
    "supertonic": EngineSpec(
        name="supertonic",
        module="textcast.tts.supertonic",
        cls="SupertonicEngine",
        extra="supertonic",
        description="Supertonic 3 — 99M params, ONNX, 44.1 kHz, no espeak-ng",
        default_voice="M1",
        options={"steps": 4},
    ),
    "kokoro": EngineSpec(
        name="kokoro",
        module="textcast.tts.kokoro",
        cls="KokoroEngine",
        extra="kokoro",
        description="Kokoro-82M — StyleTTS2, PyTorch, 24 kHz, needs espeak-ng",
        default_voice="af_heart",
        options={},
    ),
}


class EngineNotAvailable(RuntimeError):
    """The engine is registered but its dependencies are not installed."""


def get_engine(name: str, **options) -> TTSEngine:
    """Build an engine by name. Extra keyword arguments override its defaults."""
    try:
        spec = ENGINES[name]
    except KeyError:
        known = ", ".join(sorted(ENGINES))
        raise ValueError(f"Unknown TTS engine {name!r}. Available: {known}") from None

    try:
        module = importlib.import_module(spec.module)
    except ImportError as exc:
        raise EngineNotAvailable(
            f"Engine {name!r} needs its optional dependencies. "
            f"Install them with: uv sync --extra {spec.extra}"
        ) from exc

    merged = {**spec.options, **options}
    return getattr(module, spec.cls)(**merged)


def available() -> dict[str, bool]:
    """Which registered engines can actually be built right now."""
    out = {}
    for name, spec in ENGINES.items():
        try:
            importlib.import_module(spec.module)
            out[name] = True
        except ImportError:
            out[name] = False
    return out


__all__ = [
    "Clip",
    "EngineNotAvailable",
    "EngineSpec",
    "ENGINES",
    "TTSEngine",
    "Voice",
    "available",
    "get_engine",
    "silence",
]
