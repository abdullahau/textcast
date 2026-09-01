"""Engine registry.

Engines are declared here but imported lazily, so installing one extra
(``uv sync --extra supertonic``) never drags in the other's dependencies.
Kokoro pulls PyTorch and spaCy; Supertonic pulls only ONNX Runtime.
"""

from __future__ import annotations

import importlib
import importlib.util

from .base import Clip, EngineSpec, TTSEngine, Voice, silence

ENGINES: dict[str, EngineSpec] = {
    "supertonic": EngineSpec(
        name="supertonic",
        module="textcast.tts.supertonic",
        cls="SupertonicEngine",
        extra="supertonic",
        requires="supertonic",
        description="Supertonic 3 — 99M params, ONNX, 44.1 kHz, faster but thinner",
        default_voice="M1",
        options={"steps": 4},
    ),
    "kokoro": EngineSpec(
        name="kokoro",
        module="textcast.tts.kokoro",
        cls="KokoroEngine",
        extra="kokoro",
        requires="kokoro",
        description="Kokoro-82M — StyleTTS2, 54 voices, steadier delivery (default)",
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

    if not is_installed(spec):
        raise EngineNotAvailable(
            f"Engine {name!r} needs its optional dependencies. "
            f"Install them with: uv sync --extra {spec.extra}"
        )

    module = importlib.import_module(spec.module)
    merged = {**spec.options, **options}
    try:
        return getattr(module, spec.cls)(**merged)
    except ImportError as exc:
        raise EngineNotAvailable(
            f"Engine {name!r} failed to load its dependencies: {exc}"
        ) from exc


def is_installed(spec: EngineSpec) -> bool:
    return importlib.util.find_spec(spec.requires) is not None


def available() -> dict[str, bool]:
    """Which registered engines can actually be built right now."""
    return {name: is_installed(spec) for name, spec in ENGINES.items()}


__all__ = [
    "Clip",
    "EngineNotAvailable",
    "EngineSpec",
    "ENGINES",
    "TTSEngine",
    "Voice",
    "available",
    "get_engine",
    "is_installed",
    "silence",
]
