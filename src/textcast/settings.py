"""Settings, read once from the environment.

Deliberately plain: a dataclass and ``os.environ``. Every value has a working
default so the app runs with no configuration at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(f"TEXTCAST_{key}", default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).expanduser())

    # --- text to speech ---
    engine: str = field(default_factory=lambda: _env("TTS_ENGINE", "kokoro"))
    voice: str = field(default_factory=lambda: _env("TTS_VOICE", "af_heart"))
    quote_voice: str = field(default_factory=lambda: _env("TTS_QUOTE_VOICE", ""))
    threads: int = field(default_factory=lambda: _env_int("TTS_THREADS", 0))
    #: How fast the voice reads. 1.0 is the model's own pace. This changes
    #: the audio itself, unlike the player's speed control.
    speed: float = field(default_factory=lambda: _env_float("TTS_SPEED", 1.0))

    # --- encoding ---
    bitrate: str = field(default_factory=lambda: _env("AUDIO_BITRATE", "32k"))
    gap_ms: int = field(default_factory=lambda: _env_int("AUDIO_GAP_MS", 350))
    heading_gap_ms: int = field(default_factory=lambda: _env_int("AUDIO_HEADING_GAP_MS", 700))

    # --- worker ---
    workers: int = field(default_factory=lambda: _env_int("WORKERS", 1))
    #: Engine instances rendering blocks side by side. 0 means one per core.
    #: Measured on 4 cores: four single-threaded instances beat one
    #: four-threaded instance by ~11%, and six are worse than four.
    concurrency: int = field(default_factory=lambda: _env_int("CONCURRENCY", 0))
    #: Minutes of an empty queue before the engine pool is dropped. Loading it
    #: again costs seconds; holding it costs gigabytes for as long as the
    #: process lives, and most of a day is an empty queue. 0 keeps it loaded.
    idle_unload_minutes: int = field(
        default_factory=lambda: _env_int("TTS_IDLE_MINUTES", 10)
    )
    #: Run jobs in a child process that exits when its queue empties. A C
    #: extension cannot be unimported, so this is the only way their memory
    #: comes back: torch and the model for a build, openai and httpx for a
    #: summary. Off runs them in the worker, as they used to.
    job_subprocess: bool = field(
        default_factory=lambda: _env_bool("JOB_SUBPROCESS", True)
    )
    #: Minutes between IMAP polls. 0 disables the poller.
    mail_poll_minutes: int = field(default_factory=lambda: _env_int("MAIL_POLL_MINUTES", 0))

    # --- access ---
    # Off by default, which suits a private network. Turn it on in .env for
    # anything reachable from the internet.
    require_auth: bool = field(default_factory=lambda: _env_bool("REQUIRE_AUTH", False))
    auth_token: str = field(default_factory=lambda: _env("AUTH_TOKEN", ""))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "textcast.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def source_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def models_dir(self) -> Path:
        """Weights that do not come from Hugging Face.

        Kokoro's ONNX export is published as a GitHub release, not a hub repo,
        so it has nowhere else to live. Two files, and nothing writes here at
        runtime.
        """
        return self.data_dir / "models"

    def engine_options(self) -> dict:
        """Constructor arguments for the engine, from the environment."""
        return {"threads": self.threads} if self.threads else {}

    def build_concurrency(self) -> int:
        """How many engines to render with. Capped so it cannot thrash."""
        if self.concurrency > 0:
            return min(self.concurrency, 8)
        return max(1, min(os.cpu_count() or 1, 4))

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.media_dir, self.cache_dir, self.source_dir):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings


def use_settings(settings: Settings) -> None:
    """Install these as the process-wide settings.

    The build child does this with what the parent handed it, so anything
    reaching for ``get_settings()`` down in the render sees the same values as
    the worker that queued the job, not a second reading of the environment.
    """
    global _settings
    _settings = settings
