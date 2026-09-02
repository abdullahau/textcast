"""What runs on every start.

`schema.sql` is idempotent and creates anything missing, so this is only for
what a `CREATE TABLE IF NOT EXISTS` cannot express.

Adding a *column* is the one thing it cannot: `CREATE TABLE IF NOT EXISTS`
does nothing at all to a table that already exists. That is what
`_add_espeak_column` is for, and it is the only such step.

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


def has_column(conn: sqlite3.Connection, table: str, name: str) -> bool:
    return any(row["name"] == name for row in conn.execute(f"PRAGMA table_info({table})"))


def run(conn: sqlite3.Connection) -> None:
    _add_espeak_column(conn)
    # Separate from adding the column, and run every start: the column may
    # have been added by a release that shipped no espeak spellings yet, and
    # it only ever writes where nothing is written.
    _fill_builtin_espeak(conn)
    _seed_pronunciations(conn)


def _add_espeak_column(conn: sqlite3.Connection) -> None:
    """A phoneme rule needs a second spelling, for an engine whose G2P is
    espeak rather than misaki.

    `CREATE TABLE IF NOT EXISTS` cannot add a column to a table that already
    exists, and this one is new. It is a plain `ADD COLUMN` with a default, so
    it costs nothing and every existing rule gets an empty espeak spelling —
    which is what "this rule has nothing to say to that engine" means.
    """
    if not has_table(conn, "pronunciation"):
        return
    if has_column(conn, "pronunciation", "replacement_espeak"):
        return
    conn.execute(
        "ALTER TABLE pronunciation ADD COLUMN replacement_espeak TEXT NOT NULL DEFAULT ''"
    )
    log.info("added pronunciation.replacement_espeak")


def _fill_builtin_espeak(conn: sqlite3.Connection) -> None:
    """Give the shipped phoneme rules their espeak spelling.

    Seeding cannot do this: it skips any rule it has offered before, which is
    what keeps a deleted built-in deleted. A library that already holds LIBOR
    would otherwise carry an empty espeak column for ever and the rule would
    never fire on an espeak engine.

    Matched on the misaki spelling rather than the ``builtin`` flag: a rule
    that arrived through import carries ``builtin = 0`` even when it is
    word-for-word the shipped one, and this library's LIBOR is exactly that.
    A rule still holding the spelling this release ships is the same rule
    whatever the flag says; an edited one is left alone, because the espeak
    spelling of a sound nobody can see would be a guess.
    """
    from .pronounce import builtin_rules

    if not has_column(conn, "pronunciation", "replacement_espeak"):
        return

    filled = 0
    for rule in builtin_rules():
        if not (rule.is_phonemes and rule.espeak):
            continue
        cursor = conn.execute(
            """
            UPDATE pronunciation SET replacement_espeak = ?
             WHERE kind = ? AND pattern = ?
               AND replacement = ? AND replacement_espeak = ''
            """,
            (rule.espeak, rule.kind, rule.pattern, rule.replacement),
        )
        filled += cursor.rowcount
    if filled:
        log.info("filled the espeak spelling of %d built-in rule(s)", filled)


def _seed_pronunciations(conn: sqlite3.Connection) -> None:
    """Install the shipped pronunciation rules once, on an empty table."""
    if not has_table(conn, "pronunciation"):
        return
    from . import db

    added = db.seed_pronunciations(conn)
    if added:
        log.info("seeded %d pronunciation rules", added)
