"""Engine registry.

An engine is declared here rather than imported: the wrapper is loaded lazily,
so a process that never synthesises never pays for PyTorch, and
``is_installed`` can answer without importing it.

Two engines run the same Kokoro v1.0 weights — one through torch, one through
onnxruntime — and their voice ids are identical. The names are not: an ONNX
voice says so, because "Heart" twice in one list is not a choice.

Instances are expensive — building one loads an 82M-parameter model — so
``shared_engine`` keeps one per process and hands it back to every caller.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading

from .base import Clip, EngineSpec, TTSEngine, Voice, silence

DEFAULT_ENGINE = "kokoro"

ENGINES: dict[str, EngineSpec] = {
    "kokoro": EngineSpec(
        name="kokoro",
        module="textcast.tts.kokoro",
        cls="KokoroEngine",
        extra="kokoro",
        requires="kokoro",
        description="Kokoro-82M — StyleTTS2, 20 American and 8 British voices",
        default_voice="af_heart",
    ),
    # The same weights without torch. Registered whether or not its model
    # files are present: `is_installed` tests the package, and the engine
    # says what is missing when it is built.
    "kokoro-onnx": EngineSpec(
        name="kokoro-onnx",
        module="textcast.tts.kokoro_onnx",
        cls="KokoroOnnxEngine",
        extra="kokoro-onnx",
        requires="kokoro_onnx",
        description="Kokoro-82M through onnxruntime — the same voices, no PyTorch",
        default_voice="af_heart",
    ),
}


class EngineNotAvailable(RuntimeError):
    """The engine is registered but its dependencies are not installed."""


def spec_for(name: str) -> EngineSpec:
    """The registered spec, or a clear error naming what is registered."""
    try:
        return ENGINES[name]
    except KeyError:
        known = ", ".join(sorted(ENGINES))
        raise ValueError(f"Unknown TTS engine {name!r}. Available: {known}") from None


def get_engine(name: str, **options) -> TTSEngine:
    """Build a fresh engine by name. Extra keyword arguments override defaults."""
    spec = spec_for(name)
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


_shared: dict[str, TTSEngine] = {}
_shared_lock = threading.Lock()


def shared_engine(name: str, **options) -> TTSEngine:
    """One engine per process, built on first use and kept.

    Loading the model costs seconds and hundreds of megabytes. A web process
    that built a new one per request paid that on every voice list, every
    preview and every phoneme lookup.

    Keyed by name alone, deliberately. When the worker runs inside the web
    process it publishes its first instance here, and the settings page then
    borrows the pipeline that is already loaded instead of loading a second.
    """
    with _shared_lock:
        engine = _shared.get(name)
        if engine is None:
            engine = _shared[name] = get_engine(name, **options)
        return engine


def publish_engine(engine: TTSEngine) -> None:
    """Offer an engine the caller already built to everyone else in the process."""
    with _shared_lock:
        _shared.setdefault(engine.name, engine)


def release_shared(engine: TTSEngine) -> bool:
    """Give up the shared slot, if it still holds this engine.

    The worker calls this when it drops its pool, or the pool's first instance
    would keep a model resident on this module's behalf alone. A caller in the
    middle of a synthesis holds its own reference, so nothing is ever taken
    away from work in progress.
    """
    with _shared_lock:
        if _shared.get(engine.name) is engine:
            del _shared[engine.name]
            return True
        return False


def loaded_engine(name: str) -> TTSEngine | None:
    """The shared engine if it is already built, never building one.

    Callers whose answer is only *improved* by an engine use this, so a page
    load can never be what pays to load the model.
    """
    return _shared.get(name)


def catalogue(name: str) -> list[Voice]:
    """Every voice an engine offers, without building it.

    The wrapper module always imports — its heavy import sits inside
    ``__init__`` — so a module-level ``voices()`` answers this for the price of
    an import. An engine that cannot say without loading is asked only if one
    is already loaded.
    """
    spec = spec_for(name)
    module = importlib.import_module(spec.module)
    lister = getattr(module, "voices", None)
    if callable(lister):
        return list(lister())
    engine = loaded_engine(name)
    return list(engine.voices()) if engine else []


def is_installed(spec: EngineSpec) -> bool:
    return importlib.util.find_spec(spec.requires) is not None


def available() -> dict[str, bool]:
    """Which registered engines can actually be built right now."""
    return {name: is_installed(spec) for name, spec in ENGINES.items()}


def default_voice(name: str) -> str:
    spec = ENGINES.get(name)
    return spec.default_voice if spec else ""


__all__ = [
    "Clip",
    "DEFAULT_ENGINE",
    "EngineNotAvailable",
    "EngineSpec",
    "ENGINES",
    "TTSEngine",
    "Voice",
    "available",
    "catalogue",
    "default_voice",
    "get_engine",
    "is_installed",
    "loaded_engine",
    "publish_engine",
    "release_shared",
    "shared_engine",
    "silence",
    "spec_for",
]
