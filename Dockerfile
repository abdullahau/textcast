# textcast — one image, two commands (web, worker).
#
# ACCEL picks the wheels, because torch and onnxruntime ship a different
# distribution per device and it cannot be changed at runtime:
#
#   docker build -t textcast:latest .                      # CPU
#   docker build --build-arg ACCEL=cuda -t textcast:gpu .  # NVIDIA
#
# The engines then ask the machine themselves, so the GPU image still runs
# where no device was passed through.
#
# The Kokoro weights are baked in at /opt/models, so a fresh deploy needs no
# network. Costs ~330 MB. Build with --build-arg BAKE_MODEL=0 to skip it.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

ARG ACCEL=cpu
ENV EXTRAS="--extra kokoro --extra kokoro-onnx --extra web --extra documents --extra summaries"

# Dependencies first: this layer survives every source-only change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra "$ACCEL" $EXTRAS

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra "$ACCEL" $EXTRAS

ARG BAKE_MODEL=1
ENV HF_HOME=/opt/models/huggingface
COPY docker/bake_model.py /tmp/bake_model.py
RUN if [ "$BAKE_MODEL" = "1" ]; then \
      /app/.venv/bin/python /tmp/bake_model.py; \
    fi && rm -f /tmp/bake_model.py


FROM python:3.12-slim-bookworm AS runtime

# ffmpeg encodes the Opus. espeak-ng is the phonemiser — without its data
# directory the first synthesis dies on a missing phontab.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl espeak-ng espeak-ng-data \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 textcast

COPY --from=build --chown=textcast:textcast /app /app
COPY --from=build --chown=textcast:textcast /opt/models /opt/models

# ESPEAK_DATA_PATH is deliberately not set: pinning it broke the other
# architecture. tts/kokoro.py probes for it.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TEXTCAST_DATA_DIR=/data \
    HF_HOME=/opt/models/huggingface \
    # The weights are already here; without this the hub is contacted on every
    # kokoro load just to revalidate them.
    HF_HUB_OFFLINE=1 \
    # torch's CUDA libraries are pip packages, and onnxruntime-gpu's loader
    # does not look inside a venv. Harmless on the CPU image.
    LD_LIBRARY_PATH="/app/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.12/site-packages/nvidia/cufft/lib:/app/.venv/lib/python3.12/site-packages/nvidia/curand/lib" \
    # The app user has no writable home, and libespeak-ng links pulseaudio,
    # which tries to create ~/.config/pulse on every engine it loads.
    HOME=/tmp \
    TEXTCAST_HOST=0.0.0.0

# Before VOLUME, or a fresh named volume is seeded root-owned and an
# unprivileged container cannot write to it.
RUN mkdir -p /data && chown -R textcast:textcast /data

WORKDIR /app
USER textcast
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "textcast.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
