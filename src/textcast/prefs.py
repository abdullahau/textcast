"""Defaults you can change without a restart.

The engine, the voice, the quote voice and the reading pace are chosen once on
the Voice page and then used by every build that does not name its own. They live in the
``setting`` table for the same reason the summary settings do: the environment
gives the default, and a value saved in the app wins over it, or editing a
setting on the page would appear to do nothing.

Changing a default never touches audio that exists. It applies to the next
build of each article, so nothing is queued behind your back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .settings import Settings, get_settings

log = logging.getLogger("textcast.prefs")

KEY_ENGINE = "default_engine"
KEY_VOICE = "default_voice"
KEY_QUOTE_VOICE = "default_quote_voice"
KEY_SPEED = "default_speed"

#: Outside this a voice stops sounding like speech.
MIN_SPEED, MAX_SPEED = 0.5, 2.0


@dataclass(frozen=True)
class Defaults:
    engine: str
    voice: str
    quote_voice: str
    speed: float

    @property
    def speed_label(self) -> str:
        """One decimal place, so it matches an entry in the picker exactly."""
        return f"{self.speed:.1f}"


def _stored(key: str, conn) -> str:
    from . import db

    try:
        return db.get_setting(key, "", conn)
    except Exception:
        # A missing table must never be what stops a build.
        log.debug("could not read %s", key, exc_info=True)
        return ""


def voice_defaults(conn=None, settings: Settings | None = None) -> Defaults:
    settings = settings or get_settings()
    speed = _stored(KEY_SPEED, conn)
    try:
        speed_value = float(speed) if speed else settings.speed
    except ValueError:
        speed_value = settings.speed
    from .tts import ENGINES

    engine = _stored(KEY_ENGINE, conn)
    return Defaults(
        # A stored engine that no longer ships is worse than no answer at all:
        # every build would name something the registry has never heard of.
        engine=engine if engine in ENGINES else settings.engine,
        voice=_stored(KEY_VOICE, conn) or settings.voice,
        quote_voice=_stored(KEY_QUOTE_VOICE, conn) or settings.quote_voice,
        speed=min(MAX_SPEED, max(MIN_SPEED, speed_value)),
    )


def save_voice_defaults(
    conn=None,
    *,
    engine: str | None = None,
    voice: str | None = None,
    quote_voice: str | None = None,
    speed: str | float | None = None,
) -> None:
    """Store whichever fields were given. An empty string clears one."""
    from . import db
    from .tts import ENGINES

    if engine is not None and (engine.strip() in ENGINES or not engine.strip()):
        db.set_setting(KEY_ENGINE, engine.strip(), conn)
    if voice is not None:
        db.set_setting(KEY_VOICE, voice.strip(), conn)
    if quote_voice is not None:
        db.set_setting(KEY_QUOTE_VOICE, quote_voice.strip(), conn)
    if speed is not None:
        try:
            value = min(MAX_SPEED, max(MIN_SPEED, float(speed)))
        except (TypeError, ValueError):
            return
        db.set_setting(KEY_SPEED, f"{value:.1f}", conn)
