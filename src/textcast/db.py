"""SQLite storage.

WAL mode, one file, no ORM. The worker thread and the web request handlers
share a database but never a connection: each thread gets its own through
``connect()``, which is the only safe way to use sqlite3 across threads.
"""

from __future__ import annotations

import html
import json
import re
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


def dump_media(media: dict | None) -> str | None:
    """A visual's payload as JSON, and nothing at all for every other block.

    Stored as text rather than as columns because the three visual kinds hold
    different things — a picture has an address, a table has cells — and a
    column per kind would be empty on every row but its own.
    """
    return json.dumps(media, separators=(",", ":")) if media else None


def load_media(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


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


def save_article(
    article: Article,
    conn: sqlite3.Connection | None = None,
    *,
    slug: str | None = None,
) -> int:
    """Insert an article with its sections and blocks. Raises on a re-ingest.

    ``slug`` keeps an address a re-parse would otherwise move. The slug is
    derived from the title, and a re-parse that finds a better title deletes
    the row and stores a new one — so `media/<old-slug>/`, its `images/` and
    `sources/<old-slug>.*` were left with nothing able to reach them, and
    nothing sweeps by slug. The reader never sees the slug; the files do.
    """
    conn = conn or connect()
    article.renumber()
    fingerprint = article.fingerprint

    with transaction(conn):
        # Checked again here, not only before the transaction opened: `BEGIN
        # IMMEDIATE` takes the write lock a moment later, and two concurrent
        # ingests of the same content -- the mail poll and a manual add
        # landing at once -- could otherwise both pass a check made before
        # either held it, and the second would hit the unique index as a raw
        # IntegrityError instead of this.
        existing = find_by_fingerprint(conn, fingerprint)
        if existing:
            raise DuplicateArticle(existing["id"], existing["slug"])

        # Defensively re-derived if the asked-for one is taken: a caller that
        # has not deleted the old row yet must not fail on the constraint.
        taken = slug and conn.execute(
            "SELECT 1 FROM article WHERE slug = ?", (slug,)
        ).fetchone()
        slug = slug if (slug and not taken) else unique_slug(conn, article.title)
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
            "INSERT INTO section (article_id, idx, title) VALUES (?,?,?)",
            [(article_id, s.idx, s.title) for s in article.sections],
        )
        conn.executemany(
            """
            INSERT INTO block
                (article_id, section_idx, idx, block_id, kind, text, footnote_ref, media)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (article_id, b.section_idx, b.idx, b.id, str(b.kind), b.text,
                 b.footnote_ref, dump_media(b.media))
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


def edit_blocks(article_id: int, edits: dict[str, dict], conn: sqlite3.Connection | None = None) -> int:
    """Rewrite the text or the kind of blocks that already exist.

    Ids do not move, so the timing map and the audio stay valid — the audio is
    simply out of date for the blocks that changed, and rebuilding re-renders
    only those, because the cache is keyed by the text.
    """
    conn = conn or connect()
    kinds = {str(k) for k in BlockKind}
    changed = 0
    with transaction(conn):
        for block_id, edit in edits.items():
            text = (edit.get("text") or "").strip()
            kind = edit.get("kind")
            if not text:
                continue
            if kind not in kinds:
                kind = None
            cursor = conn.execute(
                "UPDATE block SET text = ?, kind = COALESCE(?, kind)"
                " WHERE article_id = ? AND block_id = ? AND (text <> ? OR kind <> COALESCE(?, kind))",
                (text, kind, article_id, block_id, text, kind),
            )
            changed += cursor.rowcount
        if changed:
            words = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(TRIM(text)) - LENGTH(REPLACE(TRIM(text), ' ', '')) + 1), 0)"
                " AS n FROM block WHERE article_id = ?",
                (article_id,),
            ).fetchone()["n"]
            conn.execute("UPDATE article SET word_count = ? WHERE id = ?", (words, article_id))
    return changed


def set_block_media(
    article_id: int, block_id: str, media: dict | None, conn: sqlite3.Connection | None = None
) -> None:
    """Rewrite one block's payload, leaving its text and its id alone.

    How a fetched picture reaches the row it belongs to. Nothing else moves,
    so the audio and the timing map stay valid: only where the picture is
    read from has changed.
    """
    conn = conn or connect()
    with transaction(conn):
        conn.execute(
            "UPDATE block SET media = ? WHERE article_id = ? AND block_id = ?",
            (dump_media(media), article_id, block_id),
        )


def replace_blocks(article_id: int, article: Article, conn: sqlite3.Connection | None = None) -> None:
    """Swap in a new set of blocks for an article that is already stored.

    Inserting or removing a block moves every id after it, so the audio that
    exists no longer lines up. The timings go with the old rows and the audio
    counters are cleared; the caller queues a build.
    """
    conn = conn or connect()
    article.renumber()
    with transaction(conn):
        conn.execute("DELETE FROM block WHERE article_id = ?", (article_id,))
        # The sections go too. They were left alone, so emptying one by hand
        # deleted its blocks and left its heading standing over nothing —
        # "Explore more" and "FT alerts" survived every edit that removed the
        # links under them, because `load_article` seeds sections from this
        # table and does not care whether any block points at them.
        conn.execute("DELETE FROM section WHERE article_id = ?", (article_id,))
        conn.executemany(
            "INSERT INTO section (article_id, idx, title) VALUES (?,?,?)",
            [(article_id, s.idx, s.title) for s in article.sections],
        )
        conn.executemany(
            """
            INSERT INTO block
                (article_id, section_idx, idx, block_id, kind, text, footnote_ref, media)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (article_id, b.section_idx, b.idx, b.id, str(b.kind), b.text,
                 b.footnote_ref, dump_media(b.media))
                for _s, b in article.blocks()
            ],
        )
        # Status describes the audio, and there is none that matches this text
        # any more. Leaving it `ready` claimed audio the article did not have.
        conn.execute(
            "UPDATE article SET word_count = ?, audio_ms = 0, audio_bytes = 0, status = 'new'"
            " WHERE id = ?",
            (article.word_count, article_id),
        )


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
        s["idx"]: Section(title=s["title"], idx=s["idx"])
        for s in conn.execute(
            "SELECT idx, title FROM section WHERE article_id = ? ORDER BY idx", (article_id,)
        )
    }
    for b in conn.execute(
        """
        SELECT section_idx, idx, kind, text, footnote_ref, media
          FROM block WHERE article_id = ? ORDER BY section_idx, idx
        """,
        (article_id,),
    ):
        section = sections.setdefault(b["section_idx"], Section(title="", idx=b["section_idx"]))
        section.blocks.append(
            Block(
                kind=BlockKind(b["kind"]),
                text=b["text"],
                footnote_ref=b["footnote_ref"],
                media=load_media(b["media"]),
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
               SET status = 'ready', audio_ms = ?, audio_bytes = ?, engine = ?, voice = ?,
                   built_at = CAST(strftime('%s','now') AS INTEGER)
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


def forget_audio(article_id: int, conn: sqlite3.Connection | None = None) -> None:
    """Drop everything that describes audio, keeping the text.

    For when the files are gone but the database still points at them — media
    deleted by hand, or a volume that did not come back. The timings are what
    the player seeks by, so stale ones are worse than none.
    """
    conn = conn or connect()
    with transaction(conn):
        conn.execute(
            "UPDATE article SET status = 'new', audio_ms = 0, audio_bytes = 0 WHERE id = ?",
            (article_id,),
        )
        conn.execute(
            "UPDATE section SET file = NULL, duration_ms = 0 WHERE article_id = ?", (article_id,)
        )
        conn.execute(
            "UPDATE block SET start_ms = NULL, dur_ms = NULL, speech_ms = NULL WHERE article_id = ?",
            (article_id,),
        )


def delete_article(article_id: int, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    conn.execute("DELETE FROM article WHERE id = ?", (article_id,))


def set_author(article_id: int, author: str, conn: sqlite3.Connection | None = None) -> None:
    """Who wrote it, when the parser did not find out or got it wrong."""
    conn = conn or connect()
    conn.execute("UPDATE article SET author = ? WHERE id = ?", (author.strip(), article_id))


def set_flag(article_id: int, field: str, value: bool, conn: sqlite3.Connection | None = None) -> None:
    if field not in ("starred", "archived"):
        raise ValueError(f"not a flag: {field}")
    conn = conn or connect()
    conn.execute(f"UPDATE article SET {field} = ? WHERE id = ?", (int(value), article_id))


# --------------------------------------------------------------------------
# library and history
# --------------------------------------------------------------------------


#: A status you can filter for that no article row ever holds. `article.status`
#: describes the audio and nothing else, so "listened to the end" lives on the
#: position row instead and is answered by `_article_filter`.
COMPLETED = "completed"


def _article_filter(
    *,
    tag: str | None = None,
    status: str | None = None,
    starred: bool = False,
    archived: bool = False,
    query: str | None = None,
) -> tuple[str, str, list[Any]]:
    """The JOIN, the WHERE and their parameters, for the list and its count.

    Both were written out separately and could disagree, which divides a pager
    by a total that does not match the rows under it. One producer now, in the
    order SQL reads them: the join's parameters first, then the where's.
    """
    joins = ["LEFT JOIN position p ON p.article_id = a.id"]
    join_params: list[Any] = []
    if tag:
        # Ahead of the position join, because its parameter is bound first.
        joins.insert(0, "JOIN article_tag at ON at.article_id = a.id AND at.tag = ?")
        join_params.append(tag)

    where = ["a.archived = ?"]
    params: list[Any] = [int(archived)]
    if status == COMPLETED:
        # Playback reached the end. Only an article with audio can get there,
        # so the ready check is not redundant: it keeps a stale position row
        # on an article whose audio was dropped out of the answer.
        where.append("a.status = 'ready' AND p.finished = 1")
    elif status:
        where.append("a.status = ?")
        params.append(status)
    if starred:
        where.append("a.starred = 1")
    if query:
        where.append("(a.title LIKE ? OR a.subtitle LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    return " ".join(joins), " AND ".join(where), join_params + params


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
    """One page of the library, most recently added first.

    Added, not published: a newsletter from last March read today belongs at
    the top with everything else that arrived today, and an article with no
    publication date used to fall back to its added date anyway, so the two
    orders were already mixed.
    """
    conn = conn or connect()
    join, where, params = _article_filter(
        tag=tag, status=status, starred=starred, archived=archived, query=query
    )
    return conn.execute(
        f"""
        SELECT a.*, p.ms AS position_ms, p.section_idx AS position_section, p.finished
          FROM article a
          {join}
         WHERE {where}
         ORDER BY a.added_at DESC, a.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()


def count_articles(
    conn: sqlite3.Connection | None = None,
    *,
    tag: str | None = None,
    status: str | None = None,
    starred: bool = False,
    archived: bool = False,
    query: str | None = None,
) -> int:
    """How many rows ``list_articles`` would return without a limit."""
    conn = conn or connect()
    join, where, params = _article_filter(
        tag=tag, status=status, starred=starred, archived=archived, query=query
    )
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM article a {join} WHERE {where}", params
    ).fetchone()
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


#: Fields on the article itself. They are not blocks, so no amount of
#: block_fts will find them, and "who wrote this" is a fair thing to search.
ARTICLE_FIELDS = ("title", "subtitle", "author", "source")


def fts_query(text: str) -> str:
    """What someone typed, as an expression FTS5 will accept.

    Every term is quoted as a phrase. A hyphen, an ampersand or a trailing OR
    is a syntax error to FTS5 and a perfectly ordinary thing to type: before
    this, searching for "Drug-Trial" or "roll-up" — words in the titles of
    articles in the library — answered with a 500.
    """
    out = []
    for term in re.findall(r'"[^"]*"|\S+', text or ""):
        cleaned = re.sub(r'[^\w\s\'-]', " ", term.replace('"', " "), flags=re.UNICODE).strip()
        if cleaned:
            out.append('"' + cleaned + '"')
    return " ".join(out)


def _mark(text: str, term: str) -> str:
    """The field with the match wrapped, escaped for the page."""
    safe = html.escape(text or "")
    if not term:
        return safe
    needle = html.escape(term)
    at = safe.lower().find(needle.lower())
    if at < 0:
        return safe
    return f"{safe[:at]}<mark>{safe[at:at + len(needle)]}</mark>{safe[at + len(needle):]}"


def search_articles(query: str, conn: sqlite3.Connection | None = None, limit: int = 10) -> list[dict]:
    """Articles whose title, byline, publication or tags match.

    A plain substring match, not FTS: these are names, and typing half of one
    should find it. Cheap at a few hundred articles; measure past that.
    """
    conn = conn or connect()
    term = (query or "").strip()
    if not term:
        return []

    # Escaped the same way articles_matching's own LIKE is: unescaped, a
    # literal % or _ in the term -- "50% off" is a title in this library's
    # own corpus -- widens the match to rows that never mentioned it.
    like = f"%{_like_literal(term)}%"
    fields = " OR ".join(f"a.{name} LIKE ? ESCAPE '\\'" for name in ARTICLE_FIELDS)
    rows = conn.execute(
        f"""
        SELECT a.id AS article_id, a.slug, a.title, a.series, a.status,
               a.subtitle, a.author, a.source, a.audio_ms
          FROM article a
         WHERE a.archived = 0
           AND ({fields}
                OR EXISTS (SELECT 1 FROM article_tag t
                            WHERE t.article_id = a.id AND t.tag LIKE ? ESCAPE '\\'))
         ORDER BY a.added_at DESC
         LIMIT ?
        """,
        (*([like] * len(ARTICLE_FIELDS)), like, limit),
    ).fetchall()

    hits = []
    needle = term.lower()
    # One query for every hit's tags, not one per hit. `tags_for_many` was
    # written for the library page and this asked the same question a row at
    # a time.
    tags = tags_for_many([row["article_id"] for row in rows], conn)
    for row in rows:
        # Only the fields that actually matched. The title is already the
        # heading of the row, so repeating it says nothing.
        fields = [row["subtitle"], row["author"], row["source"], *tags[row["article_id"]]]
        parts = [_mark(v, term) for v in fields if v and needle in v.lower()]
        if needle in (row["title"] or "").lower():
            parts.insert(0, _mark(row["title"], term))
        hits.append(
            {
                "article_id": row["article_id"],
                "slug": row["slug"],
                "title": row["title"],
                "series": row["series"],
                "status": row["status"],
                "block_id": None,
                "section_idx": None,
                "start_ms": None,
                "kind": "article",
                "snippet": " · ".join(parts),
                "rank": -1.0,
            }
        )
    return hits


def search(query: str, conn: sqlite3.Connection | None = None, limit: int = 40) -> list[dict]:
    """Everything, article by article and then block by block.

    Two searches, because there are two kinds of answer. An article matches on
    its title, byline, publication or tags, and the hit opens it at the top. A
    block matches on its text — including summaries, quotes and footnotes,
    which are blocks like any other — and the hit opens at that block, and at
    its position in the audio.
    """
    conn = conn or connect()
    if not (query or "").strip():
        return []

    hits = search_articles(query, conn)

    expression = fts_query(query)
    if not expression:
        return hits

    rows = conn.execute(
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
        (expression, limit),
    ).fetchall()
    # An article listed by name still shows its matching passages below: the
    # two answer different questions, and the block hits carry a position.
    return hits + [dict(r) for r in rows]


# --------------------------------------------------------------------------
# playback position
# --------------------------------------------------------------------------


#: How close to the end of an article counts as the end. The player's clock
#: runs on the decoded audio and the total comes from the build manifest, and
#: Opus files sit on a ~1.02 s page grid — so a played-out article saves a
#: position a few seconds past its own duration.
END_MS = 5000


def end_slack(total_ms: int) -> int:
    """How far from the end still counts as the end, for an article this long.

    A tenth of it, at most five seconds. Flat five seconds is most of a
    twenty-second note, and starting one would have marked it played.
    """
    return min(END_MS, max(0, total_ms // 10))


def save_position(
    article_id: int,
    section_idx: int,
    ms: int,
    finished: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Remember where playback is, and notice when that is the end.

    Only the player knows the last section ended rather than the user having
    skipped there, so it carries the flag. But a lost flag left a played-out
    article in "Continue listening" for ever, so a position at the end counts
    too. Scrubbing back clears it on the next save.
    """
    conn = conn or connect()
    # Read and write in one transaction: a build finishing between the two
    # -- writing a fresh audio_ms -- could otherwise compute `finished`
    # against a total that was already stale by the time it was read.
    with transaction(conn):
        row = conn.execute("SELECT audio_ms FROM article WHERE id = ?", (article_id,)).fetchone()
        total = row["audio_ms"] if row else 0
        if total and ms >= total - end_slack(total):
            finished = True
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


def clear_position(article_id: int, conn: sqlite3.Connection | None = None) -> None:
    """Forget where this article was left.

    The row is deleted rather than set back to zero. `continue_listening` asks
    for `ms > 5000`, so a zeroed row would drop off the list too — but it would
    still read as finished = 0 and hide the completed badge, and the reader
    would go on resuming from a position nobody wants any more.
    """
    conn = conn or connect()
    conn.execute("DELETE FROM position WHERE article_id = ?", (article_id,))


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
         WHERE p.finished = 0
           AND p.ms > 5000
           -- Belt as well as braces: a row written before save_position read
           -- the total still drops off once there is nothing left to hear.
           AND (a.audio_ms = 0 OR p.ms < a.audio_ms - MIN(?, a.audio_ms / 10))
           AND a.archived = 0
           AND a.status = 'ready'
         ORDER BY p.updated_at DESC
         LIMIT ?
        """,
        (END_MS, limit),
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
        # One pending job of a kind per article; re-queueing replaces the old
        # one. Scoped by kind, or asking for a summary would cancel a build
        # that is waiting to run.
        conn.execute(
            "DELETE FROM job WHERE article_id = ? AND kind = ? AND state IN ('queued', 'failed')",
            (article_id, kind),
        )
        cursor = conn.execute(
            "INSERT INTO job (article_id, kind, options, created_at) VALUES (?,?,?,?)",
            (article_id, kind, json.dumps(options or {}), now()),
        )
        # `status` describes the audio. Asking for a summary does not change
        # what the audio is, so it does not touch it.
        if kind == "build":
            conn.execute("UPDATE article SET status = 'queued' WHERE id = ?", (article_id,))
    return int(cursor.lastrowid)


def claim_job(
    conn: sqlite3.Connection | None = None,
    kinds: tuple[str, ...] | None = None,
    exclude: set[int] | None = None,
) -> sqlite3.Row | None:
    """Atomically take the oldest queued job of these kinds.

    Never one for an article that already has a job running. Summarising
    rewrites the very blocks a build would be rendering, so the two must not
    meet on the same article — but on different articles they use different
    machines entirely, one the network and one the CPU, and there is no reason
    for either to wait.

    `exclude` passes over jobs this caller has already put back. Without it a
    build process bound to one engine would claim, release and re-claim the
    same job for the other engine for ever.

    Only a build sets the article to `building`. A summary pass does not
    change the state of the audio, so it does not pretend to.
    """
    conn = conn or connect()
    where = "state = 'queued'"
    params: list[Any] = []
    if kinds:
        where += f" AND kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    if exclude:
        where += f" AND id NOT IN ({','.join('?' * len(exclude))})"
        params.extend(sorted(exclude))

    with transaction(conn):
        row = conn.execute(
            f"""
            SELECT * FROM job
             WHERE {where}
               AND article_id NOT IN (SELECT article_id FROM job WHERE state = 'running')
             ORDER BY created_at, id LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE job SET state = 'running', started_at = ?, progress = 0 WHERE id = ?",
            (now(), row["id"]),
        )
        if row["kind"] == "build":
            conn.execute(
                "UPDATE article SET status = 'building' WHERE id = ?", (row["article_id"],)
            )
    return row


def release_job(job_id: int, conn: sqlite3.Connection | None = None) -> None:
    """Put a claimed job back on the queue, exactly as it was found.

    The article's status goes back too: `claim_job` set it to `building` a
    moment ago and nothing has touched the audio.
    """
    conn = conn or connect()
    with transaction(conn):
        conn.execute(
            "UPDATE job SET state = 'queued', started_at = NULL, progress = 0 WHERE id = ?",
            (job_id,),
        )
        conn.execute(
            """
            UPDATE article SET status = 'queued'
             WHERE id = (SELECT article_id FROM job WHERE id = ?)
               AND status = 'building'
            """,
            (job_id,),
        )


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


def has_running_job(article_id: int, conn: sqlite3.Connection | None = None) -> bool:
    """A child process is right now writing this article's rows or files.

    Only `running` matters. A *queued* job's row is deleted along with the
    article by the foreign key and can never be claimed after that, so it was
    never a race. A running one is already a child process outside any
    transaction this connection can see — deleting or re-parsing out from
    under it left files it went on writing, and job rows it went on updating,
    with nothing left pointing at either.
    """
    conn = conn or connect()
    row = conn.execute(
        "SELECT 1 FROM job WHERE article_id = ? AND state = 'running' LIMIT 1", (article_id,)
    ).fetchone()
    return row is not None


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


def last_summary(article_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """How this article's summaries were last made, or an empty dict.

    Read off the last summary job that finished, because that is the only
    place the model is written down. A summary is a block like any other and
    carries no origin: one written by hand and one written by a model are the
    same row. So an article with summaries and nothing here was summarised
    before this was recorded, or the text was written by hand — and in both
    cases the honest answer is to say nothing.
    """
    conn = conn or connect()
    row = conn.execute(
        """
        SELECT options, finished_at FROM job
         WHERE article_id = ? AND kind = 'summarise' AND state = 'done'
         ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        options = json.loads(row["options"] or "{}")
    except ValueError:
        return {}
    if not options.get("model"):
        return {}
    return {
        "model": options["model"],
        "base_url": options.get("base_url", ""),
        "at": row["finished_at"] or "",
    }


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
        # A library made before a column existed has no spelling in it, which
        # is exactly what an empty string means.
        misaki=(row["replacement_misaki"] if "replacement_misaki" in row.keys() else ""),
        espeak=(row["replacement_espeak"] if "replacement_espeak" in row.keys() else ""),
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
    replacement: str = "",
    conn: sqlite3.Connection | None = None,
    *,
    misaki: str = "",
    espeak: str = "",
    ignore_case: bool = False,
    note: str = "",
    sort_order: int = 100,
    builtin: bool = False,
) -> int:
    """Store a rule. All three replacements are optional; one is required."""
    from .pronounce import KINDS, Rule, invalidate

    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("a rule needs something to match")
    rule = Rule(
        kind=kind, pattern=pattern, replacement=replacement or "",
        misaki=misaki or "", espeak=espeak or "",
    )
    if rule.compile() is None:
        raise ValueError("that pattern is not a valid regular expression")
    if not rule.says_anything():
        raise ValueError(
            "a rule needs something to say: a respelling, or IPA for a phonemiser"
        )

    conn = conn or connect()
    cursor = conn.execute(
        """
        INSERT INTO pronunciation
            (kind, pattern, replacement, replacement_misaki,
             replacement_espeak, ignore_case, note, sort_order, builtin, added_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (kind, pattern) DO UPDATE SET
            replacement = excluded.replacement,
            replacement_misaki = excluded.replacement_misaki,
            replacement_espeak = excluded.replacement_espeak,
            ignore_case = excluded.ignore_case,
            note        = excluded.note
        """,
        (kind, pattern, rule.replacement.strip(),
         rule.misaki.strip(), rule.espeak.strip(), int(ignore_case),
         note, sort_order, int(builtin), now()),
    )
    invalidate()
    return int(cursor.lastrowid)


def update_pronunciation(rule_id: int, conn: sqlite3.Connection | None = None, **fields) -> None:
    from .pronounce import invalidate

    allowed = {"pattern", "replacement", "replacement_misaki", "replacement_espeak",
               "note", "enabled", "ignore_case", "sort_order"}
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


def summarisable(conn: sqlite3.Connection | None = None, limit: int = 200) -> list[sqlite3.Row]:
    """Articles with no summary blocks yet, newest first."""
    conn = conn or connect()
    return conn.execute(
        """
        SELECT a.id, a.slug, a.title
          FROM article a
         WHERE a.archived = 0
           AND NOT EXISTS (
               SELECT 1 FROM block b
                WHERE b.article_id = a.id AND b.kind = 'summary'
           )
         ORDER BY a.added_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _like_literal(text: str) -> str:
    """A literal that means itself inside a LIKE pattern.

    Not for correctness -- a wildcard only widens a LIKE, and the regex has
    the last word either way. It is what keeps the narrowing worth doing: an
    unescaped `%` in "100%" matches every block holding "100".
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def articles_matching(rule, conn: sqlite3.Connection | None = None, limit: int = 500) -> list[sqlite3.Row]:
    """Built articles whose text the rule would change.

    Editing one rule invalidates only the articles that use the word, so the
    page can offer to rebuild exactly those. The match itself runs in Python
    because a rule may be a regular expression, which SQLite cannot match.

    A word or a phrase rule is a literal inside guards, though, so the literal
    has to be in the text -- the same bargain `pronounce.apply` makes. `LIKE`
    is a superset of what the pattern would match, and SQLite reads only the
    rows that pass it instead of handing every block of every built article to
    Python. A regex rule has no literal and still reads them all.
    """
    pattern = rule.compile()
    if pattern is None:
        return []

    conn = conn or connect()
    # LIKE is case-insensitive over ASCII whatever the rule says about case,
    # which is what makes it a superset: the pattern still has the last word.
    narrow, params = "", []
    if rule.kind != "regex" and rule.pattern:
        narrow = "AND b.text LIKE ? ESCAPE '\\'"
        params = ["%" + _like_literal(rule.pattern) + "%"]

    hits: list[int] = []
    seen: set[int] = set()
    for row in conn.execute(
        f"""
        SELECT b.article_id, b.text
          FROM block b JOIN article a ON a.id = b.article_id
         WHERE a.status = 'ready' AND a.archived = 0
           {narrow}
         ORDER BY b.article_id
        """,
        params,
    ):
        article_id = row["article_id"]
        if article_id in seen:
            continue
        if pattern.search(row["text"]):
            seen.add(article_id)
            hits.append(article_id)
            if len(hits) >= limit:
                break

    if not hits:
        return []
    placeholders = ",".join("?" * len(hits))
    return conn.execute(
        f"SELECT id, slug, title FROM article WHERE id IN ({placeholders}) ORDER BY added_at DESC",
        hits,
    ).fetchall()


#: What a rule is, on the way out and back in. `builtin` is deliberately not
#: carried: it marks what shipped with the app, not what you meant.
EXPORT_FIELDS = (
    "kind", "pattern", "replacement", "replacement_misaki",
    "replacement_espeak", "ignore_case", "enabled", "note", "sort_order",
)


def export_pronunciations(conn: sqlite3.Connection | None = None) -> dict:
    """Every rule, as plain data you can keep, diff or hand to someone else."""
    conn = conn or connect()
    rules = []
    for row in conn.execute("SELECT * FROM pronunciation ORDER BY sort_order, id"):
        rule = {field: row[field] for field in EXPORT_FIELDS}
        for flag in ("ignore_case", "enabled"):
            rule[flag] = bool(rule[flag])
        rules.append(rule)
    return {"textcast": "pronunciations", "version": 1, "rules": rules}


def import_pronunciations(
    data: dict | list, conn: sqlite3.Connection | None = None, replace: bool = False
) -> dict:
    """Add or update rules from an export. Returns what happened.

    A rule is identified by its kind and pattern, so importing the same file
    twice changes nothing the second time. Nothing is deleted unless you ask
    for `replace`, because a merge is what you want when carrying a few rules
    between machines.
    """
    from .pronounce import KINDS, invalidate

    rules = data.get("rules", []) if isinstance(data, dict) else data
    if not isinstance(rules, list):
        raise ValueError("that file has no list of rules in it")

    conn = conn or connect()
    added = updated = skipped = 0
    with transaction(conn):
        if replace:
            conn.execute("DELETE FROM pronunciation")
        existing = {
            (r["kind"], r["pattern"])
            for r in conn.execute("SELECT kind, pattern FROM pronunciation")
        }
        for raw in rules:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            kind = str(raw.get("kind", "word"))
            pattern = str(raw.get("pattern", "")).strip()
            if kind not in KINDS or not pattern:
                skipped += 1
                continue
            was = (kind, pattern) in existing
            conn.execute(
                """
                INSERT INTO pronunciation
                    (kind, pattern, replacement, replacement_misaki,
                     replacement_espeak, ignore_case, enabled, note, sort_order,
                     builtin, added_at)
                VALUES (?,?,?,?,?,?,?,?,?,0,?)
                ON CONFLICT (kind, pattern) DO UPDATE SET
                    replacement = excluded.replacement,
                    replacement_misaki = excluded.replacement_misaki,
                    replacement_espeak = excluded.replacement_espeak,
                    ignore_case = excluded.ignore_case,
                    enabled     = excluded.enabled,
                    note        = excluded.note,
                    sort_order  = excluded.sort_order
                """,
                (
                    kind, pattern, str(raw.get("replacement", "")),
                    str(raw.get("replacement_misaki", "")),
                    str(raw.get("replacement_espeak", "")),
                    int(bool(raw.get("ignore_case", False))),
                    int(bool(raw.get("enabled", True))),
                    str(raw.get("note", "")),
                    int(raw.get("sort_order", 100) or 100),
                    now(),
                ),
            )
            if was:
                updated += 1
            else:
                added += 1
                existing.add((kind, pattern))
    invalidate()
    return {"added": added, "updated": updated, "skipped": skipped}


#: Which built-ins have ever been offered, so a rule deleted on purpose stays
#: deleted while a rule added to a later release still arrives.
SEEDED_KEY = "pronunciation_seeded"


def seed_pronunciations(conn: sqlite3.Connection | None = None, force: bool = False) -> int:
    """Install built-in rules the library has never been offered.

    Skipping once anything was stored kept a deleted rule deleted, and it also
    meant a rule added to a later release never reached an existing library.
    Both are wanted, so what has been offered is recorded rather than inferred
    from what is present.

    A library seeded before that record existed takes its current rules as the
    baseline. That costs one pass: a built-in deleted before the record began
    comes back on the first start after upgrading, because nothing can tell it
    apart from one never shown. The baseline is written on the way out, so it
    cannot happen twice.
    """
    from .pronounce import builtin_rules

    conn = conn or connect()
    rules = builtin_rules()

    if force:
        offered: set[tuple[str, str]] = set()
    else:
        stored = get_setting(SEEDED_KEY, "", conn)
        if stored:
            offered = {(kind, pattern) for kind, pattern in json.loads(stored)}
        else:
            offered = {
                (row["kind"], row["pattern"])
                for row in conn.execute("SELECT kind, pattern FROM pronunciation")
            }

    added = 0
    for rule in rules:
        if (rule.kind, rule.pattern) in offered:
            continue
        add_pronunciation(
            rule.kind, rule.pattern, rule.replacement, conn,
            misaki=rule.misaki, espeak=rule.espeak, note=rule.note,
            ignore_case=rule.ignore_case, sort_order=rule.sort_order, builtin=True,
        )
        added += 1

    offered |= {(r.kind, r.pattern) for r in rules}
    set_setting(SEEDED_KEY, json.dumps(sorted(offered)), conn)
    return added
