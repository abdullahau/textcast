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
    _add_phoneme_columns(conn)
    _move_ipa_into_its_own_field(conn)
    # Separate from adding the columns, and run every start: a column may have
    # been added by a release that shipped no spelling for it yet, and this
    # only ever writes where nothing is written.
    _fill_builtin_phonemes(conn)
    _seed_pronunciations(conn)


def _add_phoneme_columns(conn: sqlite3.Connection) -> None:
    """A rule has three replacements now: plain text, and IPA per phonemiser.

    `CREATE TABLE IF NOT EXISTS` cannot add a column to a table that already
    exists. These are plain `ADD COLUMN`s with a default, so they cost nothing
    and every existing rule gets an empty spelling — which is what "this rule
    has nothing to say to that phonemiser" means.
    """
    if not has_table(conn, "pronunciation"):
        return
    for column in ("replacement_misaki", "replacement_espeak"):
        if has_column(conn, "pronunciation", column):
            continue
        conn.execute(
            f"ALTER TABLE pronunciation ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
        )
        log.info("added pronunciation.%s", column)


def _move_ipa_into_its_own_field(conn: sqlite3.Connection) -> None:
    """An IPA rule used to keep its phonemes in `replacement`, with a flag.

    That worked while there was one phonemiser. With two, the plain
    replacement and the phonemes are different things and need different
    boxes: left where it was, misaki's notation would be read as a respelling
    by anything that is not misaki.
    """
    if not has_column(conn, "pronunciation", "replacement_misaki"):
        return
    moved = conn.execute(
        """
        UPDATE pronunciation
           SET replacement_misaki = replacement, replacement = ''
         WHERE is_phonemes = 1 AND replacement_misaki = '' AND replacement <> ''
        """
    ).rowcount
    if moved:
        log.info("moved %d IPA rule(s) into replacement_misaki", moved)


def _fill_builtin_phonemes(conn: sqlite3.Connection) -> None:
    """Give the shipped phoneme rules any spelling they are missing.

    Seeding cannot do this: it skips any rule it has offered before, which is
    what keeps a deleted built-in deleted. A library that already holds LIBOR
    would otherwise carry an empty espeak column for ever and the rule would
    never fire on an espeak engine.

    Matched on the misaki spelling rather than the `builtin` flag: a rule that
    arrived through import carries `builtin = 0` even when it is word for word
    the shipped one. A rule still holding the spelling this release ships is
    the same rule whatever the flag says; an edited one is left alone, because
    the other notation for a sound nobody can see would be a guess.
    """
    from .pronounce import builtin_rules

    if not has_column(conn, "pronunciation", "replacement_espeak"):
        return

    filled = 0
    for rule in builtin_rules():
        if not rule.misaki:
            continue
        for column, value in (
            ("replacement_misaki", rule.misaki),
            ("replacement_espeak", rule.espeak),
        ):
            if not value:
                continue
            filled += conn.execute(
                f"""
                UPDATE pronunciation SET {column} = ?
                 WHERE kind = ? AND pattern = ? AND {column} = ''
                   AND (replacement_misaki = ? OR replacement_misaki = '')
                """,
                (value, rule.kind, rule.pattern, rule.misaki),
            ).rowcount
    if filled:
        log.info("filled %d missing phoneme spelling(s) on built-in rules", filled)


def _seed_pronunciations(conn: sqlite3.Connection) -> None:
    """Install the shipped pronunciation rules once, on an empty table."""
    if not has_table(conn, "pronunciation"):
        return
    from . import db

    added = db.seed_pronunciations(conn)
    if added:
        log.info("seeded %d pronunciation rules", added)
