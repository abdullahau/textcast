"""What runs on every start.

`schema.sql` is idempotent and creates anything missing, so this is only for
what a `CREATE TABLE IF NOT EXISTS` cannot express.

There is deliberately nothing here for older schemas. The repair steps that
once lived here — adding `article.build_options`, folding the `series` table
into tags, dropping a retired engine from stored build options, removing
`section.summary` — have all run, and the only database they existed for now
matches `schema.sql` exactly. Restoring a backup old enough to need them means
re-adding the step, which git still has.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("textcast.migrate")


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def run(conn: sqlite3.Connection) -> None:
    _seed_pronunciations(conn)


def _seed_pronunciations(conn: sqlite3.Connection) -> None:
    """Install the shipped pronunciation rules once, on an empty table."""
    if not has_table(conn, "pronunciation"):
        return
    from . import db

    added = db.seed_pronunciations(conn)
    if added:
        log.info("seeded %d pronunciation rules", added)
