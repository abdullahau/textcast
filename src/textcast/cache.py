"""What the block cache holds, and what it may forget.

Its own module, not part of ``service``, because the build worker's parent
process calls the sweep and must not grow to do it. ``service`` drags in
requests and the parsers: importing it beside ``jobs`` took the parent from
38 MB to 45 MB, and the parent staying small is the whole reason a build runs
in a child at all. Everything here is already in the parent's import graph.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from . import db
from .audio import CACHE_SUFFIX, _cache_key
from .document import Block, BlockKind
from .prefs import voice_defaults
from .settings import Settings, get_settings
from .tts import g2p_of

log = logging.getLogger("textcast.cache")


def cache_keys(article_id: int, conn, settings: Settings, chosen=None) -> set[str]:
    """The cache keys this article's blocks would read.

    ``chosen`` is the saved voice defaults, for a caller walking the whole
    library: they are the same for every article and reading them per article
    is a database round trip per article for one answer.

    The engine comes off the article's own build options, then off the *saved*
    default -- the same three layers, in the same order, that `jobs._build`
    reads. Getting that layering wrong made a rebuild empty its own cache; see
    docs/decisions.md ("Where the data lives") for the two shapes that were
    wrong before this one.

    The phonemiser matters for the same reason. A rule written in IPA reaches
    only the engine it was written for, so the spoken text is not the same
    string on both, and neither is the key.
    """
    chosen = chosen or voice_defaults(conn, settings)
    options = db.get_build_options(article_id, conn)
    engine = options.get("engine") or chosen.engine
    voice = options.get("voice") or chosen.voice or "af_heart"
    quote_voice = options.get("quote_voice") or chosen.quote_voice
    speed = float(options.get("speed") or chosen.speed or 1.0)
    g2p, takes_ipa = g2p_of(engine)

    keys: set[str] = set()
    for row in conn.execute(
        "SELECT kind, text FROM block WHERE article_id = ?", (article_id,)
    ):
        block = Block(kind=BlockKind(row["kind"]), text=row["text"])
        quoted = block.kind is BlockKind.QUOTE and quote_voice
        spoken = block.spoken(quote_markers=not quoted, g2p=g2p, phonemes=takes_ipa)
        keys.add(_cache_key(spoken, engine, quote_voice if quoted else voice, speed))
    return keys


def library_keys(conn, settings: Settings) -> set[str]:
    """Every key any article in the library still wants, in one pass.

    Reachability is computed over the whole library at once, which is what
    makes deleting the rest of the cache safe: a render two articles share is
    kept while either wants it.
    """
    chosen = voice_defaults(conn, settings)
    keys: set[str] = set()
    for row in conn.execute("SELECT id FROM article"):
        keys |= cache_keys(row["id"], conn, settings, chosen)
    return keys


def cached_renders(article_id: int, conn, settings: Settings) -> list[Path]:
    """The cache files only this article would read.

    A key is a hash of the spoken text, the engine, the voice and the pace, so
    a file can belong to more than one article — two pieces quoting the same
    paragraph share one render. Keys any *other* article still wants are held
    back, or dropping one article's audio would silently cost another its
    cheap rebuild.
    """
    chosen = voice_defaults(conn, settings)
    mine = cache_keys(article_id, conn, settings, chosen)
    for row in conn.execute("SELECT id FROM article WHERE id != ?", (article_id,)):
        mine -= cache_keys(row["id"], conn, settings, chosen)
        if not mine:
            # Every render this article reads is read by another one too.
            break
    return [settings.cache_dir / f"{key}{CACHE_SUFFIX}" for key in sorted(mine)]


def sweep_cache(
    settings: Settings | None = None, conn=None, wanted: set[str] | None = None
) -> tuple[int, int]:
    """Delete every render no block in the library can reach any more.

    Nothing collected these before. A rule change, a text edit, a re-parse or
    a deleted article each leave their old renders behind, and the key is a
    hash so nothing ever overwrites them. Measured before this existed: 363 of
    691 files, 0.96 GB, 43% of the cache, unreachable.

    Reachability is computed over the whole library at once, which is what
    makes it safe -- a file two articles share is kept while either wants it.
    ``wanted`` lets a caller that has already worked it out say so.
    Returns the count and the bytes freed.
    """
    settings = settings or get_settings()
    conn = conn or db.connect(settings.db_path)

    if wanted is None:
        wanted = library_keys(conn, settings)

    removed = freed = 0
    for path in settings.cache_dir.glob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".part":
            # A half-written render, left by a build that was killed. Nothing
            # can ever read one -- the name a reader looks for carries no
            # `.part` -- and stepping over them meant they accumulated for
            # ever. Only the cold ones: `service.delete` sweeps from a web
            # request, and a build may be part-way through writing one.
            if not _is_stale(path):
                continue
        # A file from an older format is unreachable whatever its name says.
        elif path.stem in wanted and path.suffix == CACHE_SUFFIX:
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("swept %d orphaned renders, freed %s", removed, _size(freed))
    return removed, freed


#: How long a `.part` must have sat still before it counts as abandoned. One
#: block takes seconds to render, so an hour is far beyond any live write.
STALE_PART_SECONDS = 3600


def _is_stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > STALE_PART_SECONDS
    except OSError:
        return False


def _size(count: int) -> str:
    """MB below a gigabyte. "0.00 GB" for a swept 4 KB file said nothing."""
    for unit, step in (("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if count >= step:
            return f"{count / step:.1f} {unit}"
    return f"{count} bytes"
