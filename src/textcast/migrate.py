"""Schema migrations.

Small and explicit: each step checks what is already there and does nothing if
the database is current, so ``init`` can run it on every start.
"""

from __future__ import annotations

import json
import logging
import sqlite3

log = logging.getLogger("textcast.migrate")


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def run(conn: sqlite3.Connection) -> None:
    _add_build_options(conn)
    _series_become_tags(conn)
    _drop_series_table(conn)
    _drop_retired_engine(conn)
    _drop_section_summary(conn)
    _seed_pronunciations(conn)


def _add_build_options(conn: sqlite3.Connection) -> None:
    """Per-article build preferences, replacing the per-series defaults."""
    if "build_options" in columns(conn, "article"):
        return
    conn.execute("ALTER TABLE article ADD COLUMN build_options TEXT NOT NULL DEFAULT '{}'")
    log.info("added article.build_options")


def _series_become_tags(conn: sqlite3.Connection) -> None:
    """Fold the old series table into tags, then leave it behind.

    Grouping newsletters was worth keeping; a separate concept for it was not.
    A detected newsletter is now just a tag, so it filters the same way as any
    tag the user makes.
    """
    if not has_table(conn, "series") or not has_table(conn, "article_tag"):
        return

    moved = conn.execute(
        "SELECT DISTINCT series FROM article WHERE series IS NOT NULL AND series <> ''"
    ).fetchall()
    if not moved:
        return

    already = conn.execute("SELECT COUNT(*) AS n FROM article_tag").fetchone()["n"]
    if already:
        return

    for row in moved:
        name = row["series"]
        conn.execute(
            "INSERT OR IGNORE INTO tag (name, added_at) VALUES (?, datetime('now'))", (name,)
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO article_tag (article_id, tag)
            SELECT id, ? FROM article WHERE series = ?
            """,
            (name, name),
        )
    log.info("migrated %d series into tags", len(moved))


def _drop_series_table(conn: sqlite3.Connection) -> None:
    """Remove the table tags replaced.

    Runs after ``_series_become_tags``, which uses the table's existence to
    decide whether there is anything to fold. Nothing has read it since; the
    newsletter a parser recognises is kept on ``article.series`` and becomes a
    tag on the way in.
    """
    if not has_table(conn, "series"):
        return
    conn.execute("DROP TABLE series")
    log.info("dropped the series table; tags replaced it")


def _drop_retired_engine(conn: sqlite3.Connection) -> None:
    """Forget an engine choice the app no longer ships.

    A stored ``{"engine": "supertonic"}`` would otherwise sit in the article's
    build options for ever, asking on every rebuild for something gone.
    """
    from .tts import ENGINES

    rows = conn.execute(
        "SELECT id, build_options FROM article WHERE build_options LIKE '%\"engine\"%'"
    ).fetchall()
    changed = 0
    for row in rows:
        try:
            options = json.loads(row["build_options"] or "{}")
        except json.JSONDecodeError:
            continue
        if options.get("engine") in ENGINES or "engine" not in options:
            continue
        options.pop("engine")
        conn.execute(
            "UPDATE article SET build_options = ? WHERE id = ?", (json.dumps(options), row["id"])
        )
        changed += 1
    if changed:
        log.info("cleared a retired engine from %d article(s)", changed)


def _drop_section_summary(conn: sqlite3.Connection) -> None:
    """Remove the column the retired summary blocks used.

    Nothing ever wrote it: the kind was plumbed end to end but never produced.
    """
    if "summary" not in columns(conn, "section"):
        return
    try:
        conn.execute("ALTER TABLE section DROP COLUMN summary")
    except sqlite3.Error as exc:
        # Older SQLite cannot drop a column. An unused nullable one is harmless.
        log.debug("leaving section.summary in place: %s", exc)
    else:
        log.info("dropped section.summary")


def _seed_pronunciations(conn: sqlite3.Connection) -> None:
    """Install the shipped pronunciation rules once, on an empty table."""
    if not has_table(conn, "pronunciation"):
        return
    from . import db

    added = db.seed_pronunciations(conn)
    if added:
        log.info("seeded %d pronunciation rules", added)
