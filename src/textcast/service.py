"""Ingestion, shared by the CLI and the web app.

Everything that turns raw input into a stored, queued article lives here so the
two front ends cannot drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import db
from .document import Article
from .ingest import parse_html
from .ingest.newsletter import article_from_eml
from .settings import Settings, get_settings

log = logging.getLogger("textcast.service")

USER_AGENT = "Mozilla/5.0 (compatible; textcast/0.2; +https://github.com/abdullahau/textcast)"

#: Below this, an extraction from a *web page* is almost certainly a login
#: wall rather than an article. Text you typed or a file you chose is taken at
#: face value, however short — a two-line note is a legitimate thing to add.
MIN_WORDS = 60

#: Extensions ``store_source`` writes, newest parse first. Re-parse looks for
#: the article's original in this order.
SOURCE_SUFFIXES = (".html", ".eml", ".txt", ".md", ".pdf", ".docx")


class IngestError(RuntimeError):
    pass


@dataclass
class Ingested:
    article_id: int
    slug: str
    title: str
    word_count: int
    series: str | None
    job_id: int | None
    tags: list[str] = field(default_factory=list)
    duplicate: bool = False


def fetch(url: str, timeout: float = 30.0) -> str:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise IngestError(f"could not fetch {url}: {exc}") from exc
    return response.text


def article_from_source(
    *,
    html: str | None = None,
    url: str | None = None,
    eml: bytes | None = None,
    text: str | None = None,
    title: str | None = None,
    upload: tuple[bytes, str] | None = None,
    adapter: str | None = None,
    source: str = "",
) -> Article:
    """Parse one of the accepted input forms into an Article."""
    from .ingest.documents import UnsupportedDocument, article_from_file, article_from_text

    if upload is not None:
        data, filename = upload
        try:
            return article_from_file(data, filename, title=title or "")
        except UnsupportedDocument as exc:
            raise IngestError(str(exc)) from exc

    if eml is not None:
        return article_from_eml(eml, url=url or "")

    if text is not None and text.strip():
        # Pasted text has no page to name its publication, so it is asked for.
        return article_from_text(text, title=title or "", source=source or "Pasted text")

    if html is None:
        if not url:
            raise IngestError("give a url, some text, a file, or an eml message")
        html = fetch(url)
    return parse_html(html, url=url or "", prefer=adapter)


def store_source(slug: str, raw: bytes, suffix: str, settings: Settings | None = None) -> Path:
    """Keep the original input, so a parser fix can be replayed without re-fetching."""
    settings = settings or get_settings()
    settings.source_dir.mkdir(parents=True, exist_ok=True)
    path = settings.source_dir / f"{slug}{suffix}"
    path.write_bytes(raw)
    return path


def ingest(
    *,
    html: str | None = None,
    url: str | None = None,
    eml: bytes | None = None,
    text: str | None = None,
    title: str | None = None,
    upload: tuple[bytes, str] | None = None,
    adapter: str | None = None,
    source: str = "",
    build: bool = True,
    options: dict | None = None,
    tags: list[str] | None = None,
    settings: Settings | None = None,
) -> Ingested:
    """Parse, store, and (by default) queue a build.

    A re-ingest of identical content returns the existing article rather than
    creating a second copy — the same issue often arrives by both email and
    share sheet.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    article = article_from_source(
        html=html, url=url, eml=eml, text=text, title=title, upload=upload,
        adapter=adapter, source=source,
    )
    from_web = text is None and upload is None
    if from_web and article.word_count < MIN_WORDS:
        raise IngestError(
            f"only {article.word_count} words extracted — the page is probably a "
            "login wall. Save it from a logged-in browser and upload the HTML."
        )
    if not article.word_count:
        raise IngestError("there was no text to read in that")

    return store(
        article,
        original=_original(html=html, eml=eml, text=text, upload=upload),
        build=build,
        options=options,
        tags=tags,
        settings=settings,
    )


def store(
    article: Article,
    *,
    original: tuple[bytes | None, str] = (None, ""),
    build: bool = True,
    options: dict | None = None,
    tags: list[str] | None = None,
    settings: Settings | None = None,
) -> Ingested:
    """Write a parsed article to the library and queue its build.

    Split out of ``ingest`` so ``reparse`` can parse first and only then
    replace what is stored.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)

    try:
        article_id = db.save_article(article, conn)
    except db.DuplicateArticle as dup:
        log.info("already stored: %s", dup.slug)
        row = db.get_article(dup.article_id, conn)
        return Ingested(
            article_id=dup.article_id,
            slug=dup.slug,
            title=row["title"],
            word_count=row["word_count"],
            series=row["series"],
            job_id=None,
            duplicate=True,
        )

    row = db.get_article(article_id, conn)
    raw, suffix = original
    if raw:
        store_source(row["slug"], raw, suffix, settings)

    applied = db.set_tags(article_id, _tags_for(article, tags), conn)
    if options:
        db.set_build_options(article_id, options, conn)

    job_id = None
    if build:
        # Summarising comes first and queues the build itself, because a new
        # block moves every id after it.
        kind = "summarise" if (options or {}).get("summarize") else "build"
        job_id = db.enqueue(article_id, kind=kind, options=options, conn=conn)

    return Ingested(
        article_id=article_id,
        slug=row["slug"],
        title=article.title,
        word_count=article.word_count,
        series=article.series,
        job_id=job_id,
        tags=applied,
    )


def _original(*, html=None, eml=None, text=None, upload=None) -> tuple[bytes | None, str]:
    """Keep whatever was handed in, so Re-parse can replay it later."""
    if upload is not None:
        return upload[0], Path(upload[1]).suffix.lower() or ".bin"
    if eml is not None:
        return eml, ".eml"
    if text is not None and text.strip():
        return text.encode("utf-8"), ".txt"
    if html:
        return html.encode("utf-8", errors="replace"), ".html"
    return None, ""


def _tags_for(article: Article, extra: list[str] | None) -> list[str]:
    """Whatever the user typed, plus the newsletter the parser recognised."""
    names = list(extra or [])
    if article.series and article.series not in names:
        names.append(article.series)
    return names


def rebuild(article_id: int, options: dict | None = None, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    if db.get_article(article_id, conn) is None:
        raise IngestError(f"no article {article_id}")
    return db.enqueue(article_id, options=options, conn=conn)


def summarize(article_id: int, settings: Settings | None = None, replace: bool = False) -> int:
    """Queue a summary pass. It writes blocks and stops; building is separate."""
    from .summarize import config

    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    if db.get_article(article_id, conn) is None:
        raise IngestError(f"no article {article_id}")
    if not config(conn).ready:
        raise IngestError("summaries need a model and an API key on the Summaries page")
    options = {"replace": True} if replace else None
    return db.enqueue(article_id, kind="summarise", options=options, conn=conn)


def cached_renders(article_id: int, conn, settings: Settings) -> list[Path]:
    """The cache files this article's blocks would read.

    The key is a hash of the spoken text, the engine, the voice and the pace,
    so a file can belong to more than one article. Rare, but it means these
    are only safe to delete when the block itself is going.
    """
    from .audio import _cache_key
    from .document import Block, BlockKind
    from .prefs import voice_defaults

    chosen = voice_defaults(conn, settings)
    options = db.get_build_options(article_id, conn)
    voice = options.get("voice") or chosen.voice or "af_heart"
    quote_voice = options.get("quote_voice") or chosen.quote_voice
    speed = float(options.get("speed") or chosen.speed or 1.0)

    paths: list[Path] = []
    for row in conn.execute(
        "SELECT kind, text FROM block WHERE article_id = ?", (article_id,)
    ):
        block = Block(kind=BlockKind(row["kind"]), text=row["text"])
        quoted = block.kind is BlockKind.QUOTE and quote_voice
        spoken = block.spoken(quote_markers=not quoted)
        key = _cache_key(spoken, "kokoro", quote_voice if quoted else voice, speed)
        paths.append(settings.cache_dir / f"{key}.f32")
    return paths


def edit_blocks(
    article_id: int,
    edits: dict[str, dict],
    removed: set[str] | None = None,
    settings: Settings | None = None,
) -> dict:
    """Apply hand edits, and remove blocks outright.

    Editing text leaves the ids alone, so the audio and its timing map stay
    valid and only what changed needs re-reading. Removing a block moves every
    id after it, which the timing map is keyed by — so the audio goes, and the
    rebuild is encode-only, since the cache is keyed by the text and every
    surviving block is still in it.
    """
    from .document import BlockKind

    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        raise IngestError(f"no article {article_id}")

    removed = removed or set()
    if not removed:
        return {"changed": db.edit_blocks(article_id, edits, conn), "removed": 0}

    article = db.load_article(article_id, conn)
    kinds = {str(k) for k in BlockKind}
    kept = 0
    for section in article.sections:
        surviving = []
        for block in section.blocks:
            if block.id in removed:
                continue
            edit = edits.get(block.id) or {}
            text = (edit.get("text") or "").strip()
            if text:
                block.text = text
            if edit.get("kind") in kinds:
                block.kind = BlockKind(edit["kind"])
            surviving.append(block)
            kept += 1
        section.blocks = surviving

    if not kept:
        raise IngestError("that would leave the article with nothing in it")

    article.renumber()
    db.replace_blocks(article_id, article, conn)

    # The ids moved, so the files no longer describe this article. The cache
    # stays: it is keyed by the text, so rebuilding costs an encode, not a
    # trip back to the model.
    media = settings.media_dir / row["slug"]
    for child in media.glob("*"):
        child.unlink(missing_ok=True)
    if media.exists():
        media.rmdir()

    return {"changed": kept, "removed": len(removed)}


def delete_audio(article_id: int, settings: Settings | None = None) -> int:
    """Throw the audio away and keep the article. Returns files removed.

    The files, the block cache behind them, and every timing in the database
    that pointed at them. Without this the only way to undo a build was to
    delete the article and add it again.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        raise IngestError(f"no article {article_id}")

    removed = 0
    media = settings.media_dir / row["slug"]
    for child in media.glob("*"):
        child.unlink(missing_ok=True)
        removed += 1
    if media.exists():
        media.rmdir()

    for path in cached_renders(article_id, conn, settings):
        if path.exists():
            path.unlink(missing_ok=True)
            removed += 1

    db.forget_audio(article_id, conn)
    return removed


def delete_summaries(article_id: int, settings: Settings | None = None) -> int:
    """Remove the summary blocks and keep everything else. Returns how many.

    Removing a block moves every id after it, so the audio no longer lines up:
    it goes too, the same as it would if you had never summarised.
    """
    from .document import BlockKind

    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    article = db.load_article(article_id, conn)
    if article is None:
        raise IngestError(f"no article {article_id}")

    dropped = 0
    for section in article.sections:
        keep = [b for b in section.blocks if b.kind is not BlockKind.SUMMARY]
        dropped += len(section.blocks) - len(keep)
        section.blocks = keep
    if not dropped:
        return 0

    delete_audio(article_id, settings)
    db.replace_blocks(article_id, article, conn)
    return dropped


def delete(article_id: int, settings: Settings | None = None) -> bool:
    """Remove an article and everything kept for it alone.

    The row takes its sections, blocks, tags, position and jobs with it by
    foreign key. The audio and the stored original are files, so they are
    removed here — the original used to be left behind, orphaned in
    `sources/` with nothing in the app able to reach it again.

    Not `db.delete_article`, which `reparse` calls: that one must keep the
    source, because it is about to parse it again.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        return False

    media = settings.media_dir / row["slug"]
    for child in media.glob("*"):
        child.unlink(missing_ok=True)
    if media.exists():
        media.rmdir()

    for suffix in SOURCE_SUFFIXES:
        (settings.source_dir / f"{row['slug']}{suffix}").unlink(missing_ok=True)

    db.delete_article(article_id, conn)
    return True


def reparse(article_id: int, adapter: str | None = None, settings: Settings | None = None) -> Ingested:
    """Re-run the parser over the stored source after a parser fix.

    The new article is parsed *before* the old one is deleted. An earlier
    version deleted first, so a parse error left neither copy behind.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        raise IngestError(f"no article {article_id}")

    for suffix in SOURCE_SUFFIXES:
        path = settings.source_dir / f"{row['slug']}{suffix}"
        if path.exists():
            break
    else:
        raise IngestError(f"no stored source for {row['slug']}")

    raw = path.read_bytes()
    article = article_from_source(
        url=row["url"], adapter=adapter, **_reparse_kwargs(suffix, raw, row["title"])
    )
    if not article.word_count:
        raise IngestError("re-parsing found no text, so the article is unchanged")

    tags = db.tags_for(article_id, conn)
    options = db.get_build_options(article_id, conn)
    db.delete_article(article_id, conn)
    return store(
        article,
        original=(raw, suffix),
        tags=tags,
        options=options,
        settings=settings,
    )


def _reparse_kwargs(suffix: str, raw: bytes, title: str) -> dict:
    """Hand the stored bytes back to the parser they came from."""
    if suffix == ".eml":
        return {"eml": raw}
    if suffix == ".txt":
        return {"text": raw.decode("utf-8", errors="replace"), "title": title}
    if suffix in (".pdf", ".docx", ".md"):
        return {"upload": (raw, f"x{suffix}"), "title": title}
    return {"html": raw.decode("utf-8", errors="replace")}


def rebuild_many(article_ids: list[int], settings: Settings | None = None) -> int:
    """Queue a rebuild for several articles at once. Returns how many were queued.

    Editing one pronunciation rule can invalidate every article that uses the
    word. Rebuilding them one at a time by hand was the only way before this.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    queued = 0
    for article_id in article_ids:
        if db.get_article(article_id, conn) is None:
            continue
        db.enqueue(article_id, conn=conn)
        queued += 1
    return queued
