"""What runs on every start.

`schema.sql` is idempotent and creates anything missing, so this is only for
what a `CREATE TABLE IF NOT EXISTS` cannot express.

Adding or dropping a *column* is the one thing it cannot: `CREATE TABLE IF
NOT EXISTS` does nothing at all to a table that already exists. That is what
`_add_phoneme_columns` and `_drop_is_phonemes` are for.

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
    _seed_account(conn)
    _add_block_media(conn)
    _add_built_at(conn)
    _retire_embed_blocks(conn)
    _add_phoneme_columns(conn)
    _drop_is_phonemes(conn)
    # Separate from adding the columns, and run every start: a column may have
    # been added by a release that shipped no spelling for it yet, and this
    # only ever writes where nothing is written.
    _fill_builtin_phonemes(conn)
    _seed_pronunciations(conn)
    _scope_summary_key(conn)
    _adopt_env_summary_key(conn)
    _name_summary_keys(conn)


def _add_built_at(conn: sqlite3.Connection) -> None:
    """When this article's audio was last written, as epoch seconds.

    It goes in the media URL. `/media/<slug>/section-000.opus` promised
    `immutable` for a year and was rewritten by every build, so a browser or a
    service worker holding the old file played it against the *new* timing map
    and the read-along drifted further behind with every paragraph. A stamp in
    the query makes the promise true instead of dropping it, which was tried
    and does not work: the audio element asks for byte ranges, so without a
    long-lived header Chromium answers the worker's own plain GET as a ranged
    one and `Cache.addAll` refuses the batch.

    Everything already built is stamped once, now. That is not when it was
    built, but it is a value that changes exactly when the file does from here
    on, and the one wrong stamp costs a single re-download.
    """
    if has_column(conn, "article", "built_at"):
        return
    conn.execute("ALTER TABLE article ADD COLUMN built_at INTEGER NOT NULL DEFAULT 0")
    stamped = conn.execute(
        "UPDATE article SET built_at = CAST(strftime('%s','now') AS INTEGER)"
        " WHERE status = 'ready' OR audio_ms > 0"
    ).rowcount
    log.info("added article.built_at and stamped %d built article(s)", stamped)


def _adopt_env_summary_key(conn: sqlite3.Connection) -> None:
    """Take `TEXTCAST_SUMMARY_API_KEY` into the database, once.

    The environment used to supply a key to any endpoint that had none. That
    is gone: one variable standing behind every provider meant the page could
    not say whose key was in use, and the answer changed with the endpoint.
    Keys are typed on the Summaries page now.

    So a library upgrading with a key only in its environment would stop
    summarising. This files it under the endpoint that was selected, and only
    where nothing is stored — it never overwrites a key typed in the app.
    """
    import os

    from .summarize import (
        DEFAULT_BASE_URL,
        KEY_BASE_URL,
        PREFIX_API_KEY,
        endpoint_id,
    )

    key = os.environ.get("TEXTCAST_SUMMARY_API_KEY", "").strip()
    if not key or not has_table(conn, "setting"):
        return

    where = conn.execute("SELECT value FROM setting WHERE key = ?", (KEY_BASE_URL,)).fetchone()
    endpoint = endpoint_id((where["value"] if where else "") or DEFAULT_BASE_URL)
    if not endpoint:
        return

    setting = PREFIX_API_KEY + endpoint
    held = conn.execute("SELECT value FROM setting WHERE key = ?", (setting,)).fetchone()
    if held and (held["value"] or "").strip():
        return

    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?,?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (setting, key),
    )
    log.info("adopted TEXTCAST_SUMMARY_API_KEY for %s; it can go from the environment", endpoint)


def _name_summary_keys(conn: sqlite3.Connection) -> None:
    """Turn endpoint-scoped keys into named ones, once.

    A key used to be filed under its endpoint, which allowed exactly one per
    provider — so a second account, or a second gateway of your own, had
    nowhere to go. A key is a named thing now, in `summary_key`, and the model
    picker offers those names.

    Each `summary_api_key:<endpoint>` becomes a credential named for its
    provider, carrying the model it was last used with. Whichever endpoint was
    selected becomes the key in use. The old rows go: left behind they would
    be a second answer to the same question.
    """
    from . import db
    from .summarize import (
        KEY_BASE_URL,
        KEY_CREDENTIAL,
        PREFIX_API_KEY,
        PREFIX_MODEL,
        PROVIDERS,
        endpoint_id,
    )

    if not has_table(conn, "setting") or not has_table(conn, "summary_key"):
        return
    rows = conn.execute(
        "SELECT key, value FROM setting WHERE key LIKE ?", (PREFIX_API_KEY + "%",)
    ).fetchall()
    if not rows:
        return

    by_url = {endpoint_id(url): (pid, name) for pid, name, url in PROVIDERS}
    models = {
        row["key"][len(PREFIX_MODEL):]: row["value"]
        for row in conn.execute(
            "SELECT key, value FROM setting WHERE key LIKE ?", (PREFIX_MODEL + "%",)
        )
    }
    where = conn.execute("SELECT value FROM setting WHERE key = ?", (KEY_BASE_URL,)).fetchone()
    selected = endpoint_id(where["value"] if where else "")
    in_use = ""

    for row in rows:
        endpoint = row["key"][len(PREFIX_API_KEY):]
        key = (row["value"] or "").strip()
        if not key:
            continue
        provider, name = by_url.get(endpoint, ("", endpoint))
        conn.execute(
            """
            INSERT INTO summary_key (name, provider, base_url, api_key, model, added_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT (name) DO NOTHING
            """,
            (name, provider, "" if provider else endpoint, key, models.get(endpoint, ""), db.now()),
        )
        if endpoint == selected:
            in_use = name

    conn.execute("DELETE FROM setting WHERE key LIKE ?", (PREFIX_API_KEY + "%",))
    conn.execute("DELETE FROM setting WHERE key LIKE ?", (PREFIX_MODEL + "%",))
    if in_use:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?,?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (KEY_CREDENTIAL, in_use),
        )
    # The endpoint is stored only as an override now, and the one it held is
    # the provider's own. Left in place it would pin the address against a
    # provider that later moves it.
    if selected in by_url:
        conn.execute("DELETE FROM setting WHERE key = ?", (KEY_BASE_URL,))
    log.info("named %d stored summary key(s)", len(rows))


def _scope_summary_key(conn: sqlite3.Connection) -> None:
    """File the one summary key under the endpoint it was for.

    There used to be a single `summary_api_key`, which meant a library could
    hold one key however many providers it used. Switching provider kept the
    old key and sent it to the new endpoint. The key now lives under
    `summary_api_key:<endpoint>`, so this moves the flat one across to
    whichever endpoint was selected when it was stored, and deletes it. Left
    in place it would be a second answer to the same question.
    """
    from .summarize import (
        DEFAULT_BASE_URL,
        KEY_API_KEY,
        KEY_BASE_URL,
        KEY_MODEL,
        PREFIX_API_KEY,
        PREFIX_MODEL,
        endpoint_id,
    )

    if not has_table(conn, "setting"):
        return
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (KEY_API_KEY,)).fetchone()
    if row is None:
        return

    key = (row["value"] or "").strip()
    where = conn.execute("SELECT value FROM setting WHERE key = ?", (KEY_BASE_URL,)).fetchone()
    endpoint = endpoint_id((where["value"] if where else "") or DEFAULT_BASE_URL)

    if key and endpoint:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?,?) ON CONFLICT (key) DO NOTHING",
            (PREFIX_API_KEY + endpoint, key),
        )
        # The model it was used with belongs to that endpoint too, or picking
        # the provider again would offer the first name in the built-in list.
        model = conn.execute("SELECT value FROM setting WHERE key = ?", (KEY_MODEL,)).fetchone()
        if model and (model["value"] or "").strip():
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?,?) ON CONFLICT (key) DO NOTHING",
                (PREFIX_MODEL + endpoint, model["value"].strip()),
            )
        log.info("summary key filed under %s", endpoint)
    conn.execute("DELETE FROM setting WHERE key = ?", (KEY_API_KEY,))


def _add_block_media(conn: sqlite3.Connection) -> None:
    """A block can now show something as well as say it.

    Nothing backfills: an article parsed before visuals existed has no record
    of the picture it dropped. Re-parse reads the stored source again and
    fills it in, which is what Re-parse is for.
    """
    if not has_table(conn, "block") or has_column(conn, "block", "media"):
        return
    conn.execute("ALTER TABLE block ADD COLUMN media TEXT")
    log.info("added block.media")


def _seed_account(conn: sqlite3.Connection) -> None:
    """Take the username and the password out of the environment, once.

    After this the account lives in the database and `.env` stops being read,
    which is the whole point: a password you can change from the Settings page
    cannot also be a value in a file the container was started with.

    With no `TEXTCAST_AUTH_TOKEN` set there is nothing to seed and no row is
    written. `require_auth` is off by default, so that is the ordinary case for
    a private network; the login page says what to set when it is not.
    """
    from . import accounts
    from .settings import get_settings

    if not has_table(conn, "account") or accounts.get(conn) is not None:
        return
    settings = get_settings()
    if not settings.auth_token:
        return
    accounts.create(conn, settings.username, settings.auth_token)
    log.info("account %r seeded from the environment", settings.username)


def _retire_embed_blocks(conn: sqlite3.Connection) -> None:
    """`embed` is not a kind any more, and `BlockKind` would raise on one.

    A frame is stored as the still picture of the same chart now. Anything
    written before that is turned into a figure where it has a picture to
    show and a plain paragraph where it does not — the text reads "Chart: ..."
    either way, so nothing is lost but the frame. Re-parse restores the still.
    """
    if not has_table(conn, "block"):
        return
    rows = conn.execute(
        "SELECT id, media FROM block WHERE kind = 'embed'"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        media = row["media"] or ""
        kind = "figure" if '"file"' in media else "para"
        conn.execute("UPDATE block SET kind = ? WHERE id = ?", (kind, row["id"]))
    log.info("retired %d embed block(s); re-parse to get the chart as a picture", len(rows))


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


def _drop_is_phonemes(conn: sqlite3.Connection) -> None:
    """Whether a rule speaks in phonemes is derived from its fields.

    It was a stored flag while there was one phonemiser and the IPA shared a
    column with the respelling. With three replacement fields the flag can
    only repeat what they already say, or disagree with them.

    The step that moved each rule's IPA out of `replacement` and into
    `replacement_misaki` has run and was removed; git still has it. This drop
    must come after it, and it is the last thing the old column is needed for.
    """
    if not has_column(conn, "pronunciation", "is_phonemes"):
        return
    # SQLite has had DROP COLUMN since 3.35 (2021); the image ships 3.40.
    conn.execute("ALTER TABLE pronunciation DROP COLUMN is_phonemes")
    log.info("dropped pronunciation.is_phonemes")


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
