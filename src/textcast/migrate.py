"""Schema migrations.

Small and explicit: each step checks what is already there and does nothing if
the database is current, so ``init`` can run it on every start.
"""

from __future__ import annotations

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
