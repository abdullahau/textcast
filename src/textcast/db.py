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
    conn = connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
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
        if article.series:
            conn.execute(
                "INSERT OR IGNORE INTO series (name, display, added_at) VALUES (?,?,?)",
                (article.series, article.series, now()),
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
    series: str | None = None,
    status: str | None = None,
    starred: bool = False,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """The library, newest first, with playback progress joined in."""
    conn = conn or connect()
    where = ["a.archived = ?"]
    params: list[Any] = [int(archived)]
    if series:
        where.append("a.series = ?")
        params.append(series)
    if status:
        where.append("a.status = ?")
        params.append(status)
    if starred:
        where.append("a.starred = 1")

    params.extend([limit, offset])
    return conn.execute(
        f"""
        SELECT a.*, p.ms AS position_ms, p.section_idx AS position_section, p.finished
          FROM article a
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


def list_series(conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    """Newsletters, with issue counts — the spine of the library view."""
    conn = conn or connect()
    return conn.execute(
        """
        SELECT s.*,
               COUNT(a.id) AS issues,
               SUM(CASE WHEN a.status = 'ready' THEN 1 ELSE 0 END) AS ready,
               SUM(a.audio_ms) AS audio_ms,
               MAX(COALESCE(a.published_at, a.added_at)) AS latest
          FROM series s
          LEFT JOIN article a ON a.series = s.name AND a.archived = 0
         GROUP BY s.name
         ORDER BY latest DESC
        """
    ).fetchall()


def get_series(name: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    conn = conn or connect()
    return conn.execute("SELECT * FROM series WHERE name = ?", (name,)).fetchone()


def update_series(name: str, conn: sqlite3.Connection | None = None, **fields) -> None:
    allowed = {"display", "voice", "quote_voice", "auto_build", "skip_footnotes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = conn or connect()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE series SET {assignments} WHERE name = ?", [*updates.values(), name])


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
