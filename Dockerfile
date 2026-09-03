# textcast — one image, two commands (web, worker).
#
# Deploy it anywhere: it binds a plain port and takes no view on how you reach
# it. Put a reverse proxy, a VPN or nothing in front, as you prefer.
#
# Two images, one Dockerfile. ACCEL picks the wheels:
#
#   docker build -t textcast:latest .                      # cpu, the default
#   docker build --build-arg ACCEL=cuda -t textcast:gpu .  # NVIDIA
#
# It cannot be a runtime setting. torch and onnxruntime each ship a different
# distribution per device, so the device is decided when the image is built.
# Both engines then ask the machine themselves — `torch.cuda.is_available()`
# and `onnxruntime.get_available_providers()` — so the GPU image still runs on
# a host with no device, just on the CPU. The CPU image cannot do the reverse.
# The GPU image is ~2.5 GB larger: 12 nvidia-* wheels and a CUDA build of
# torch. Run it with `docker compose -f docker-compose.yml -f
# docker-compose.gpu.yml up -d`, which needs the NVIDIA container toolkit.
#
# The TTS weights ARE baked in, at /opt/models. A first run then needs no
# network and no download, which is what you want from a fresh deploy, a CI
# run, or a container that scales to zero. It costs ~330 MB of image for
# Kokoro. Build with --build-arg BAKE_MODEL=0 to skip it and fetch at runtime.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# cpu or cuda. The two are declared as conflicting extras, so exactly one.
ARG ACCEL=cpu
ENV EXTRAS="--extra kokoro --extra kokoro-onnx --extra web --extra documents --extra summaries"

# Dependencies first: this layer survives every source-only change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra "$ACCEL" $EXTRAS

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra "$ACCEL" $EXTRAS

# --- warm the model cache into the image -----------------------------------
ARG BAKE_MODEL=1
ENV HF_HOME=/opt/models/huggingface
COPY docker/bake_model.py /tmp/bake_model.py
RUN if [ "$BAKE_MODEL" = "1" ]; then \
      /app/.venv/bin/python /tmp/bake_model.py; \
    fi && rm -f /tmp/bake_model.py


FROM python:3.12-slim-bookworm AS runtime

# ffmpeg encodes the Opus. espeak-ng is Kokoro's phonemiser — without its data
# directory the first synthesis dies on a missing phontab.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl espeak-ng espeak-ng-data \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 textcast

COPY --from=build --chown=textcast:textcast /app /app
# Read-only at runtime: the weights are part of the image, not of your data.
COPY --from=build --chown=textcast:textcast /opt/models /opt/models

# ESPEAK_DATA_PATH is deliberately not set: it was pinned to the arm64 path,
# which is wrong on an amd64 image. tts/kokoro.py probes both.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TEXTCAST_DATA_DIR=/data \
    HF_HOME=/opt/models/huggingface \
    # The weights are in the image. Without this the hub is still contacted on
    # every model load to revalidate them: it works offline either way, but it
    # is a round trip and a second of startup for nothing.
    HF_HUB_OFFLINE=1 \
    # The CUDA libraries torch brings are pip packages, not system ones, and
    # onnxruntime-gpu's loader does not look in a venv. Harmless on the CPU
    # image, where the directory does not exist. The driver itself comes from
    # the host, through the NVIDIA container toolkit.
    LD_LIBRARY_PATH="/app/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cufft/lib:/app/.venv/lib/python3.12/site-packages/nvidia/curand/lib" \
    # The app user has no home, so HOME is "/", which is not writable.
    # libespeak-ng links pulseaudio, which tries to make ~/.config/pulse on
    # every engine it phonemises with and prints three lines when it cannot.
    # Only the ONNX engine hits it: misaki's phonemizer-fork does not ask for
    # audio output. Four engines in a pool made twelve lines a build.
    HOME=/tmp \
    TEXTCAST_HOST=0.0.0.0

# Create /data owned by the app user *before* declaring the volume: Docker
# seeds a fresh named volume from the image path, so this is what makes the
# volume writable by an unprivileged container.
RUN mkdir -p /data && chown -R textcast:textcast /data

WORKDIR /app
USER textcast
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "textcast.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
