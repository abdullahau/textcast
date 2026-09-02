# textcast — one image, two commands (web, worker).
#
# Deploy it anywhere: it binds a plain port and takes no view on how you reach
# it. Put a reverse proxy, a VPN or nothing in front, as you prefer.
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

# Dependencies first: this layer survives every source-only change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra kokoro --extra kokoro-onnx --extra web --extra documents --extra summaries

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra kokoro --extra kokoro-onnx --extra web --extra documents --extra summaries

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
