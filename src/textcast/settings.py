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


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).expanduser())

    # --- text to speech ---
    engine: str = field(default_factory=lambda: _env("TTS_ENGINE", "supertonic"))
    voice: str = field(default_factory=lambda: _env("TTS_VOICE", ""))
    quote_voice: str = field(default_factory=lambda: _env("TTS_QUOTE_VOICE", ""))
    steps: int = field(default_factory=lambda: _env_int("TTS_STEPS", 4))
    threads: int = field(default_factory=lambda: _env_int("TTS_THREADS", 0))

    # --- encoding ---
    bitrate: str = field(default_factory=lambda: _env("AUDIO_BITRATE", "32k"))
    gap_ms: int = field(default_factory=lambda: _env_int("AUDIO_GAP_MS", 350))
    heading_gap_ms: int = field(default_factory=lambda: _env_int("AUDIO_HEADING_GAP_MS", 700))

    # --- worker ---
    workers: int = field(default_factory=lambda: _env_int("WORKERS", 1))

    # --- optional summaries ---
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash"))

    # --- served behind tailscale, so auth is off by default ---
    require_auth: bool = field(default_factory=lambda: _env_bool("REQUIRE_AUTH", False))
    auth_token: str = field(default_factory=lambda: _env("AUTH_TOKEN", ""))

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

    def engine_options(self) -> dict:
        opts: dict = {}
        if self.engine == "supertonic":
            opts["steps"] = self.steps
            if self.threads:
                opts["threads"] = self.threads
        return opts

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.media_dir, self.cache_dir, self.source_dir):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
