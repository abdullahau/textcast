"""Download TTS weights at image build time.

Downloads only. The model is never constructed and never run here: a build
should assemble the image, not do the work the app does at runtime. The point
is simply that the weights are already on disk when the container starts, so a
fresh deploy needs no network and no first-run wait.
"""

from __future__ import annotations

KOKORO_REPO = "hexgrad/Kokoro-82M"

#: The ONNX export is published as a GitHub release, not a hub repo, so it is
#: fetched by URL. It is the default engine, so the image has to carry it or a
#: fresh container cannot build anything.
ONNX_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
)
ONNX_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def bake_kokoro() -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(
        KOKORO_REPO,
        # The PyTorch weights and every voice, but not the ONNX duplicates.
        allow_patterns=["*.json", "*.pth", "voices/*.pt", "config.json"],
    )


def bake_kokoro_onnx(target: str = "/opt/models/kokoro-onnx") -> str:
    """Download the ONNX model and voices, unless they are already there."""
    import urllib.request
    from pathlib import Path

    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ONNX_FILES:
        path = directory / name
        if path.exists():
            continue
        # To a temporary name first: a half-written model that looks complete
        # would fail at the first synthesis rather than at build time.
        partial = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(f"{ONNX_RELEASE}/{name}", partial)
        partial.rename(path)
    return str(directory)


def main() -> int:
    print(f"kokoro weights at {bake_kokoro()}")
    print(f"kokoro-onnx weights at {bake_kokoro_onnx()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
