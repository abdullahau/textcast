"""SQLite storage.

WAL mode, one file, no ORM. The worker thread and the web request handlers
share a database but never a connection: each thread gets its own through
``connect()``, which is the only safe way to use sqlite3 across threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audio import AudioManifest
from .document import Article, Block, BlockKind, Section, slugify
from .settings import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")

_local = threading.local()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """One connection per thread, created on first use."""
    path = path or get_settings().db_path
    key = f"conn:{path}"
    conn = getattr(_local, key, None)
    if conn is not None:
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA foreign_keys = ON;
        PRAGMA busy_timeout = 30000;
        PRAGMA temp_store = MEMORY;
        """
    )
    setattr(_local, key, conn)
    return conn


def init(path: Path | None = None) -> sqlite3.Connection:
    from . import migrate

    conn = connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    migrate.run(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    conn = conn or connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def close() -> None:
    for key in list(vars(_local)):
        if key.startswith("conn:"):
            getattr(_local, key).close()
            delattr(_local, key)


# --------------------------------------------------------------------------
# articles
# --------------------------------------------------------------------------


class DuplicateArticle(Exception):
    """The same content is already stored."""

    def __init__(self, article_id: int, slug: str) -> None:
        super().__init__(f"already stored as #{article_id} ({slug})")
        self.article_id = article_id
        self.slug = slug


def unique_slug(conn: sqlite3.Connection, title: str) -> str:
    base = slugify(title)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM article WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def find_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    if not fingerprint:
        return None
    return conn.execute("SELECT * FROM article WHERE fingerprint = ?", (fingerprint,)).fetchone()


def save_article(article: Article, conn: sqlite3.Connection | None = None) -> int:
    """Insert an article with its sections and blocks. Raises on a re-ingest."""
    conn = conn or connect()
    article.renumber()
    fingerprint = article.fingerprint

    existing = find_by_fingerprint(conn, fingerprint)
    if existing:
        raise DuplicateArticle(existing["id"], existing["slug"])

    with transaction(conn):
        slug = unique_slug(conn, article.title)
        cursor = conn.execute(
            """
            INSERT INTO article
                (slug, title, subtitle, author, source, series, url, lang,
                 fingerprint, adapter, published_at, added_at, word_count, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')
            """,
            (
                slug, article.title, article.subtitle, article.author, article.source,
                article.series, article.url, article.lang, fingerprint, article.adapter,
                article.published_at, now(), article.word_count,
            ),
        )
        article_id = int(cursor.lastrowid)

        conn.executemany(
            "INSERT INTO section (article_id, idx, title, summary) VALUES (?,?,?,?)",
            [(article_id, s.idx, s.title, s.summary) for s in article.sections],
        )
        conn.executemany(
            """
            INSERT INTO block (article_id, section_idx, idx, block_id, kind, text, footnote_ref)
            VALUES (?,?,?,?,?,?,?)
            """,
            [
                (article_id, b.section_idx, b.idx, b.id, str(b.kind), b.text, b.footnote_ref)
                for _s, b in article.blocks()
            ],
        )
        # A detected newsletter is just a tag, so it filters like any other.
        if article.series:
            conn.execute(
                "INSERT OR IGNORE INTO tag (name, added_at) VALUES (?,?)", (article.series, now())
            )
            conn.execute(
                "INSERT OR IGNORE INTO article_tag (article_id, tag) VALUES (?,?)",
                (article_id, article.series),
            )
    return article_id


def get_article(article_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    conn = conn or connect()
    return conn.execute("SELECT * FROM article WHERE id = ?", (article_id,)).fetchone()


def get_by_slug(slug: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    conn = conn or connect()
    return conn.execute("SELECT * FROM article WHERE slug = ?", (slug,)).fetchone()


def load_article(article_id: int, conn: sqlite3.Connection | None = None) -> Article | None:
    """Rebuild the document model from storage."""
    conn = conn or connect()
    row = get_article(article_id, conn)
    if row is None:
        return None

    sections = {
        s["idx"]: Section(title=s["title"], summary=s["summary"], idx=s["idx"])
        for s in conn.execute(
            "SELECT * FROM section WHERE article_id = ? ORDER BY idx", (article_id,)
        )
    }
    for b in conn.execute(
        "SELECT * FROM block WHERE article_id = ? ORDER BY section_idx, idx", (article_id,)
    ):
        section = sections.setdefault(b["section_idx"], Section(title="", idx=b["section_idx"]))
        section.blocks.append(
            Block(
                kind=BlockKind(b["kind"]),
                text=b["text"],
                footnote_ref=b["footnote_ref"],
                section_idx=b["section_idx"],
                idx=b["idx"],
            )
        )

    return Article(
        title=row["title"],
        subtitle=row["subtitle"],
        author=row["author"],
        source=row["source"],
        series=row["series"],
        url=row["url"],
        lang=row["lang"],
        adapter=row["adapter"],
        published_at=row["published_at"],
        sections=[sections[i] for i in sorted(sections)],
    ).renumber()


def set_status(article_id: int, status: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute("UPDATE article SET status = ? WHERE id = ?", (status, article_id))


def save_manifest(
    article_id: int,
    manifest: AudioManifest,
    audio_bytes: int,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Write the timing map onto the blocks that already exist."""
    conn = conn or connect()
    with transaction(conn):
        conn.execute(
            """
            UPDATE article
               SET status = 'ready', audio_ms = ?, audio_bytes = ?, engine = ?, voice = ?
             WHERE id = ?
            """,
            (manifest.total_ms, audio_bytes, manifest.engine, manifest.voice, article_id),
        )
        for section in manifest.sections:
            conn.execute(
                "UPDATE section SET file = ?, duration_ms = ? WHERE article_id = ? AND idx = ?",
                (section.file, section.duration_ms, article_id, section.idx),
            )
            conn.executemany(
                """
                UPDATE block SET start_ms = ?, dur_ms = ?, speech_ms = ?
                 WHERE article_id = ? AND block_id = ?
                """,
                [
                    (b.start_ms, b.dur_ms, b.speech_ms, article_id, b.id)
                    for b in section.blocks
                ],
            )


def delete_article(article_id: int, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute("DELETE FROM article WHERE id = ?", (article_id,))


def set_flag(article_id: int, field: str, value: bool, conn: sqlite3.Connection | None = None) -> None:
    if field not in ("starred", "archived"):
        raise ValueError(f"not a flag: {field}")
    conn = conn or connect()
    conn.execute(f"UPDATE article SET {field} = ? WHERE id = ?", (int(value), article_id))


# --------------------------------------------------------------------------
# library and history
# --------------------------------------------------------------------------


def list_articles(
    conn: sqlite3.Connection | None = None,
    *,
    tag: str | None = None,
    status: str | None = None,
    starred: bool = False,
    archived: bool = False,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """The library, newest first, with playback progress joined in."""
    conn = conn or connect()
    where = ["a.archived = ?"]
    params: list[Any] = [int(archived)]
    join = ""
    if tag:
        join = "JOIN article_tag at ON at.article_id = a.id AND at.tag = ?"
        params.insert(0, tag)
    if status:
        where.append("a.status = ?")
        params.append(status)
    if starred:
        where.append("a.starred = 1")
    if query:
        where.append("(a.title LIKE ? OR a.subtitle LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    params.extend([limit, offset])
    return conn.execute(
        f"""
        SELECT a.*, p.ms AS position_ms, p.section_idx AS position_section, p.finished
          FROM article a
          {join}
          LEFT JOIN position p ON p.article_id = a.id
         WHERE {" AND ".join(where)}
         ORDER BY COALESCE(a.published_at, a.added_at) DESC, a.id DESC
         LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def count_articles(conn: sqlite3.Connection | None = None, *, archived: bool = False) -> int:
    conn = conn or connect()
    row = conn.execute("SELECT COUNT(*) AS n FROM article WHERE archived = ?", (int(archived),)).fetchone()
    return int(row["n"])


def list_tags(conn: sqlite3.Connection | None = None, with_counts: bool = True) -> list[sqlite3.Row]:
    """Every tag, with how many live articles carry it."""
    conn = conn or connect()
    if not with_counts:
        return conn.execute("SELECT name FROM tag ORDER BY name").fetchall()
    return conn.execute(
        """
        SELECT t.name,
               COUNT(a.id) AS articles,
               SUM(CASE WHEN a.status = 'ready' THEN 1 ELSE 0 END) AS ready,
               COALESCE(SUM(a.audio_ms), 0) AS audio_ms,
               MAX(COALESCE(a.published_at, a.added_at)) AS latest
          FROM tag t
          LEFT JOIN article_tag at ON at.tag = t.name
          LEFT JOIN article a ON a.id = at.article_id AND a.archived = 0
         GROUP BY t.name
         ORDER BY articles DESC, t.name
        """
    ).fetchall()


def tags_for(article_id: int, conn: sqlite3.Connection | None = None) -> list[str]:
    conn = conn or connect()
    return [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM article_tag WHERE article_id = ? ORDER BY tag", (article_id,)
        )
    ]


def tags_for_many(article_ids: list[int], conn: sqlite3.Connection | None = None) -> dict[int, list[str]]:
    """One query for a whole list page, rather than one per row."""
    if not article_ids:
        return {}
    conn = conn or connect()
    placeholders = ",".join("?" * len(article_ids))
    out: dict[int, list[str]] = {i: [] for i in article_ids}
    for row in conn.execute(
        f"SELECT article_id, tag FROM article_tag WHERE article_id IN ({placeholders}) ORDER BY tag",
        article_ids,
    ):
        out[row["article_id"]].append(row["tag"])
    return out


def create_tag(name: str, conn: sqlite3.Connection | None = None) -> str:
    name = clean_tag(name)
    if not name:
        raise ValueError("a tag needs a name")
    conn = conn or connect()
    conn.execute("INSERT OR IGNORE INTO tag (name, added_at) VALUES (?,?)", (name, now()))
    return name


def clean_tag(name: str) -> str:
    """Collapse whitespace and cap the length; tags are labels, not sentences."""
    return " ".join((name or "").split())[:48].strip()


def set_tags(article_id: int, names: list[str], conn: sqlite3.Connection | None = None) -> list[str]:
    """Replace an article's tags, creating any that are new."""
    conn = conn or connect()
    wanted = []
    for raw in names:
        name = clean_tag(raw)
        if name and name not in wanted:
            wanted.append(name)

    with transaction(conn):
        conn.execute("DELETE FROM article_tag WHERE article_id = ?", (article_id,))
        for name in wanted:
            conn.execute("INSERT OR IGNORE INTO tag (name, added_at) VALUES (?,?)", (name, now()))
            conn.execute(
                "INSERT OR IGNORE INTO article_tag (article_id, tag) VALUES (?,?)",
                (article_id, name),
            )
    return wanted


def add_tag(article_id: int, name: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    name = create_tag(name, conn)
    conn.execute(
        "INSERT OR IGNORE INTO article_tag (article_id, tag) VALUES (?,?)", (article_id, name)
    )


def remove_tag(article_id: int, name: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute(
        "DELETE FROM article_tag WHERE article_id = ? AND tag = ?", (article_id, name)
    )


def delete_tag(name: str, conn: sqlite3.Connection | None = None) -> None:
    """Remove a tag everywhere. The articles themselves are untouched."""
    conn = conn or connect()
    with transaction(conn):
        conn.execute("DELETE FROM article_tag WHERE tag = ?", (name,))
        conn.execute("DELETE FROM tag WHERE name = ?", (name,))


def rename_tag(old: str, new: str, conn: sqlite3.Connection | None = None) -> str:
    conn = conn or connect()
    new = clean_tag(new)
    if not new:
        raise ValueError("a tag needs a name")
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO tag (name, added_at) VALUES (?,?)", (new, now()))
        conn.execute(
            "UPDATE OR IGNORE article_tag SET tag = ? WHERE tag = ?", (new, old)
        )
        conn.execute("DELETE FROM article_tag WHERE tag = ?", (old,))
        conn.execute("DELETE FROM tag WHERE name = ?", (old,))
    return new


def get_build_options(article_id: int, conn: sqlite3.Connection | None = None) -> dict:
    conn = conn or connect()
    row = conn.execute("SELECT build_options FROM article WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["build_options"] or "{}")
    except json.JSONDecodeError:
        return {}


def set_build_options(article_id: int, options: dict, conn: sqlite3.Connection | None = None) -> None:
    """How this one article should be built: voice, quote voice, footnotes."""
    conn = conn or connect()
    clean = {k: v for k, v in (options or {}).items() if v not in (None, "")}
    conn.execute(
        "UPDATE article SET build_options = ? WHERE id = ?", (json.dumps(clean), article_id)
    )


def stats(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or connect()
    row = conn.execute(
        """
        SELECT COUNT(*) AS articles,
               COALESCE(SUM(audio_ms), 0) AS audio_ms,
               COALESCE(SUM(audio_bytes), 0) AS audio_bytes,
               COALESCE(SUM(word_count), 0) AS words,
               SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready
          FROM article WHERE archived = 0
        """
    ).fetchone()
    return dict(row)


def search(query: str, conn: sqlite3.Connection | None = None, limit: int = 40) -> list[sqlite3.Row]:
    """Full-text search across every block ever ingested.

    Returns one row per matching block with a highlighted snippet, so a hit can
    link straight to that block's position in the audio.
    """
    conn = conn or connect()
    if not query.strip():
        return []
    return conn.execute(
        """
        SELECT a.id AS article_id, a.slug, a.title, a.series, a.status,
               b.block_id, b.section_idx, b.start_ms, b.kind,
               snippet(block_fts, 0, '<mark>', '</mark>', '…', 18) AS snippet,
               bm25(block_fts) AS rank
          FROM block_fts
          JOIN block b ON b.id = block_fts.rowid
          JOIN article a ON a.id = b.article_id
         WHERE block_fts MATCH ?
           AND a.archived = 0
         ORDER BY rank
         LIMIT ?
        """,
        (query, limit),
    ).fetchall()


# --------------------------------------------------------------------------
# playback position
# --------------------------------------------------------------------------


def save_position(
    article_id: int,
    section_idx: int,
    ms: int,
    finished: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or connect()
    conn.execute(
        """
        INSERT INTO position (article_id, section_idx, ms, finished, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT (article_id) DO UPDATE
            SET section_idx = excluded.section_idx,
                ms          = excluded.ms,
                finished    = excluded.finished,
                updated_at  = excluded.updated_at
        """,
        (article_id, section_idx, ms, int(finished), now()),
    )


def get_position(article_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    conn = conn or connect()
    return conn.execute("SELECT * FROM position WHERE article_id = ?", (article_id,)).fetchone()


def continue_listening(conn: sqlite3.Connection | None = None, limit: int = 6) -> list[sqlite3.Row]:
    conn = conn or connect()
    return conn.execute(
        """
        SELECT a.*, p.ms AS position_ms, p.section_idx AS position_section
          FROM position p
          JOIN article a ON a.id = p.article_id
         WHERE p.finished = 0 AND p.ms > 5000 AND a.archived = 0 AND a.status = 'ready'
         ORDER BY p.updated_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


def enqueue(
    article_id: int,
    kind: str = "build",
    options: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    conn = conn or connect()
    with transaction(conn):
        # One pending job per article; re-queueing replaces the old one.
        conn.execute(
            "DELETE FROM job WHERE article_id = ? AND state IN ('queued', 'failed')",
            (article_id,),
        )
        cursor = conn.execute(
            "INSERT INTO job (article_id, kind, options, created_at) VALUES (?,?,?,?)",
            (article_id, kind, json.dumps(options or {}), now()),
        )
        conn.execute("UPDATE article SET status = 'queued' WHERE id = ?", (article_id,))
    return int(cursor.lastrowid)


def claim_job(conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    """Atomically take the oldest queued job."""
    conn = conn or connect()
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM job WHERE state = 'queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE job SET state = 'running', started_at = ?, progress = 0 WHERE id = ?",
            (now(), row["id"]),
        )
        conn.execute("UPDATE article SET status = 'building' WHERE id = ?", (row["article_id"],))
    return row


def update_job(
    job_id: int,
    conn: sqlite3.Connection | None = None,
    **fields,
) -> None:
    allowed = {"state", "progress", "message", "error", "finished_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = conn or connect()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE job SET {assignments} WHERE id = ?", [*updates.values(), job_id])


def get_job(job_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    conn = conn or connect()
    return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()


def active_jobs(conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    conn = conn or connect()
    return conn.execute(
        """
        SELECT j.*, a.title, a.slug
          FROM job j JOIN article a ON a.id = j.article_id
         WHERE j.state IN ('queued', 'running')
         ORDER BY j.created_at
        """
    ).fetchall()


def recent_jobs(conn: sqlite3.Connection | None = None, limit: int = 20) -> list[sqlite3.Row]:
    conn = conn or connect()
    return conn.execute(
        """
        SELECT j.*, a.title, a.slug
          FROM job j LEFT JOIN article a ON a.id = j.article_id
         ORDER BY j.created_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def get_setting(key: str, default: str = "", conn: sqlite3.Connection | None = None) -> str:
    conn = conn or connect()
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --------------------------------------------------------------------------
# pronunciation rules
# --------------------------------------------------------------------------


def _to_rule(row: sqlite3.Row):
    from .pronounce import Rule

    return Rule(
        id=row["id"],
        kind=row["kind"],
        pattern=row["pattern"],
        replacement=row["replacement"],
        is_phonemes=bool(row["is_phonemes"]),
        ignore_case=bool(row["ignore_case"]),
        note=row["note"],
        sort_order=row["sort_order"],
    )


def list_pronunciations(conn: sqlite3.Connection | None = None, enabled_only: bool = False) -> list:
    conn = conn or connect()
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"SELECT * FROM pronunciation {where} ORDER BY sort_order, id"
    ).fetchall()
    return [_to_rule(r) for r in rows]


def pronunciation_rows(conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    """Raw rows for the settings page, which needs enabled and builtin too."""
    conn = conn or connect()
    return conn.execute("SELECT * FROM pronunciation ORDER BY sort_order, id").fetchall()


def add_pronunciation(
    kind: str,
    pattern: str,
    replacement: str,
    conn: sqlite3.Connection | None = None,
    *,
    is_phonemes: bool = False,
    ignore_case: bool = False,
    note: str = "",
    sort_order: int = 100,
    builtin: bool = False,
) -> int:
    from .pronounce import KINDS, Rule, invalidate

    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("a rule needs something to match")
    if Rule(kind=kind, pattern=pattern, replacement=replacement).compile() is None:
        raise ValueError("that pattern is not a valid regular expression")

    conn = conn or connect()
    cursor = conn.execute(
        """
        INSERT INTO pronunciation
            (kind, pattern, replacement, is_phonemes, ignore_case, note, sort_order, builtin, added_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT (kind, pattern) DO UPDATE SET
            replacement = excluded.replacement,
            is_phonemes = excluded.is_phonemes,
            ignore_case = excluded.ignore_case,
            note        = excluded.note
        """,
        (kind, pattern, replacement, int(is_phonemes), int(ignore_case),
         note, sort_order, int(builtin), now()),
    )
    invalidate()
    return int(cursor.lastrowid)


def update_pronunciation(rule_id: int, conn: sqlite3.Connection | None = None, **fields) -> None:
    from .pronounce import invalidate

    allowed = {"pattern", "replacement", "note", "enabled", "is_phonemes", "ignore_case", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = conn or connect()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE pronunciation SET {assignments} WHERE id = ?", [*updates.values(), rule_id])
    invalidate()


def delete_pronunciation(rule_id: int, conn: sqlite3.Connection | None = None) -> None:
    from .pronounce import invalidate

    conn = conn or connect()
    conn.execute("DELETE FROM pronunciation WHERE id = ?", (rule_id,))
    invalidate()


def seed_pronunciations(conn: sqlite3.Connection | None = None, force: bool = False) -> int:
    """Install the built-in rules. Skipped once anything is stored.

    Re-seeding would resurrect rules the user deliberately deleted.
    """
    from .pronounce import builtin_rules

    conn = conn or connect()
    if not force and conn.execute("SELECT 1 FROM pronunciation LIMIT 1").fetchone():
        return 0

    added = 0
    for rule in builtin_rules():
        add_pronunciation(
            rule.kind, rule.pattern, rule.replacement, conn,
            is_phonemes=rule.is_phonemes, note=rule.note,
            sort_order=rule.sort_order, builtin=True,
        )
        added += 1
    return added
