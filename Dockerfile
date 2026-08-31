# textcast — one image, two commands (web, worker).
#
# The TTS model is NOT baked in. It is ~400 MB and belongs in a volume, so the
# image stays small and a model change does not mean a rebuild.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer survives every source-only change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra supertonic --extra web

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra supertonic --extra web


FROM python:3.12-slim-bookworm AS runtime

# ffmpeg encodes the Opus; nothing else is needed at runtime. Supertonic runs
# on ONNX Runtime, so there is no PyTorch and no espeak-ng here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 textcast

COPY --from=build --chown=textcast:textcast /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TEXTCAST_DATA_DIR=/data \
    HF_HOME=/data/huggingface \
    SUPERTONIC_HOME=/data/supertonic

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
