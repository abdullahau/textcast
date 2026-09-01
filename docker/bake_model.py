"""Download TTS weights at image build time.

Downloads only. The model is never constructed and never run here: a build
should assemble the image, not do the work the app does at runtime. The point
is simply that the weights are already on disk when the container starts, so a
fresh deploy needs no network and no first-run wait.
"""

from __future__ import annotations

import sys

KOKORO_REPO = "hexgrad/Kokoro-82M"
SUPERTONIC_REPO = "Supertone/supertonic-3"


def bake_kokoro() -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(
        KOKORO_REPO,
        # The PyTorch weights and every voice, but not the ONNX duplicates.
        allow_patterns=["*.json", "*.pth", "voices/*.pt", "config.json"],
    )


def bake_supertonic() -> str:
    """Fetch the ONNX files directly.

    Constructing supertonic.TTS would also download them, but it opens ONNX
    Runtime sessions as a side effect — loading a model during a build, which
    is what this script exists to avoid.
    """
    from huggingface_hub import snapshot_download

    return snapshot_download(
        SUPERTONIC_REPO,
        allow_patterns=["onnx/*", "voice_styles/*", "config.json"],
    )


BAKERS = {"kokoro": bake_kokoro, "supertonic": bake_supertonic}


def main(engine: str) -> int:
    baker = BAKERS.get(engine)
    if baker is None:
        print(f"nothing to bake for {engine!r}", file=sys.stderr)
        return 1
    print(f"{engine} weights at {baker()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "kokoro"))
