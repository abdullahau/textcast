"""Download TTS weights at image build time.

Run during the Docker build so the first synthesis needs no network. Only the
weights are fetched — no engine is constructed, so this does not need
espeak-ng or any of the runtime environment.
"""

from __future__ import annotations

import sys

KOKORO_REPO = "hexgrad/Kokoro-82M"


def bake_kokoro() -> None:
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        KOKORO_REPO,
        # The PyTorch weights and every voice, but not the ONNX duplicates.
        allow_patterns=["*.json", "*.pth", "voices/*.pt", "config.json"],
    )
    print(f"kokoro weights at {path}")


def bake_supertonic() -> None:
    from supertonic import TTS

    TTS(auto_download=True)
    print("supertonic weights downloaded")


def main(engine: str) -> int:
    if engine == "kokoro":
        bake_kokoro()
    elif engine == "supertonic":
        bake_supertonic()
    else:
        print(f"nothing to bake for {engine!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "kokoro"))
