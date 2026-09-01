"""Kokoro-82M — the engine.

StyleTTS2 over PyTorch, 24 kHz output. The v1.0 pack holds 28 English voices;
the pipeline loads one accent at a time, so ``lang_code='a'`` offers 20 of them
and ``'b'`` the other 8. It measures RTF 0.65
on a 4-core Neoverse-N1 with no GPU. A faster ONNX engine was tried and
dropped: it was about twice the speed, but its delivery drifted in volume and
glitched on long paragraphs.

Its one deployment hazard is espeak-ng. Kokoro reaches it through
``espeakng-loader``, whose bundled data path is frequently wrong, and the
failure is a bare "phontab: No such file or directory" during the first
synthesis. ``_locate_espeak`` finds a real installation and points the
phonemiser at it before Kokoro is imported.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

import numpy as np

from .base import Clip, Voice

log = logging.getLogger("textcast.tts.kokoro")

# Kokoro's language codes, keyed by the code its pipeline expects.
_LANGS = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "zh",
}

# The v1.0 voice pack.
_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

#: Where espeak-ng data lives, across the package managers people actually use.
_DATA_HINTS = [
    "/home/linuxbrew/.linuxbrew/share/espeak-ng-data",
    "/opt/homebrew/share/espeak-ng-data",
    "/usr/local/share/espeak-ng-data",
    "/usr/share/espeak-ng-data",
    "/usr/lib/x86_64-linux-gnu/espeak-ng-data",
    "/usr/lib/aarch64-linux-gnu/espeak-ng-data",
]

_LIB_HINTS = [
    "/home/linuxbrew/.linuxbrew/lib/libespeak-ng.so",
    "/opt/homebrew/lib/libespeak-ng.dylib",
    "/usr/local/lib/libespeak-ng.so",
    "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1",
    "/usr/lib/aarch64-linux-gnu/libespeak-ng.so.1",
]


def _locate_espeak() -> tuple[str | None, str | None]:
    """Find espeak-ng's data directory and shared library."""
    data = os.environ.get("ESPEAK_DATA_PATH") or ""
    if data and Path(data, "phontab").exists():
        found_data = data
    else:
        found_data = next(
            (h for h in _DATA_HINTS if Path(h, "phontab").exists()),
            None,
        )
        # Fall back to walking out from the binary, for unusual prefixes.
        if found_data is None and (binary := shutil.which("espeak-ng")):
            candidate = Path(binary).resolve().parent.parent / "share" / "espeak-ng-data"
            if (candidate / "phontab").exists():
                found_data = str(candidate)

    library = os.environ.get("PHONEMIZER_ESPEAK_LIBRARY") or ""
    if not (library and Path(library).exists()):
        library = next((h for h in _LIB_HINTS if Path(h).exists()), None)

    return found_data, library


def _configure_espeak() -> None:
    data, library = _locate_espeak()
    if data:
        # phonemizer wants the parent of espeak-ng-data; espeakng-loader wants
        # the directory itself. Set both rather than guess which is in play.
        os.environ.setdefault("ESPEAK_DATA_PATH", str(Path(data).parent))
        os.environ.setdefault("ESPEAKNG_DATA_PATH", data)
    if library:
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", library)
    if not data:
        log.warning(
            "espeak-ng data not found; Kokoro may fail on its first synthesis. "
            "Install it (brew install espeak-ng, or apt install espeak-ng-data) "
            "or set ESPEAK_DATA_PATH."
        )


def voices(lang_code: str = "a") -> list[Voice]:
    """The voice list, without loading anything.

    Kokoro's voices are a fixed table, so a page with a voice picker has no
    business constructing an 82M-parameter model to read it.
    """
    return [
        Voice(
            id=v,
            name=v.split("_", 1)[1].title(),
            gender="female" if v[1] == "f" else "male",
            lang=_LANGS.get(v[0], "en-us"),
        )
        for v in _VOICES
        if v[0] == lang_code
    ]


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24000

    def __init__(
        self,
        repo_id: str = "hexgrad/Kokoro-82M",
        lang_code: str = "a",
        threads: int | None = None,
        **_ignored,
    ) -> None:
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        _configure_espeak()

        if threads:
            import torch

            # Several single-threaded instances beat one wide one here, so the
            # pool sets this to 1 and gets its parallelism from the pool.
            torch.set_num_threads(threads)

        from kokoro import KPipeline

        self.lang_code = lang_code
        self._pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id)
        self._lock = threading.Lock()

    def voices(self) -> list[Voice]:
        return voices(self.lang_code)

    def phonemes(self, text: str, voice: str | None = None) -> str:
        """What the model will actually be given, for the pronunciation page.

        Exposed here so the web layer never reaches into the pipeline itself.
        """
        with self._lock:
            return " ".join(
                ps for _gs, ps, _audio in self._pipeline(text, voice=voice or "af_heart") if ps
            )

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
