"""Download TTS weights at image build time.

Downloads only. The model is never constructed and never run here: a build
should assemble the image, not do the work the app does at runtime. The point
is simply that the weights are already on disk when the container starts, so a
fresh deploy needs no network and no first-run wait.
"""

from __future__ import annotations

KOKORO_REPO = "hexgrad/Kokoro-82M"


def bake_kokoro() -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(
        KOKORO_REPO,
        # The PyTorch weights and every voice, but not the ONNX duplicates.
        allow_patterns=["*.json", "*.pth", "voices/*.pt", "config.json"],
    )


def main() -> int:
    print(f"kokoro weights at {bake_kokoro()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
