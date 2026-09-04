"""Ingestion: everything that turns raw input into a stored, queued article.

It named the CLI as its second caller. There is no CLI -- `cli.py` was deleted
because the app is the interface and a second one drifts -- so the web app is
the only front end, and the worker reaches ingest through the mail poll.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

import requests

from . import db, netguard, pictures
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


class ArticleBusy(IngestError):
    """A build or a summary is writing this article right now.

    Its own class because the three routes that can raise it each already
    mapped `IngestError` to whatever *their* own failure meant -- 400 for
    bad input, 404 for no such article -- and a running job is neither. It
    subclasses `IngestError` so every caller that already handles one goes
    on working; only the routes that want to say 409 have to know.
    """


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
    #: Summaries carried across a re-parse, and ones with nowhere to land.
    summaries_kept: int = 0
    summaries_lost: int = 0
    #: A re-parse that produced exactly what was already stored, and so
    #: replaced nothing and left the audio alone.
    unchanged: bool = False


def fetch(url: str, timeout: float = 30.0) -> str:
    try:
        response = netguard.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except (requests.RequestException, netguard.UnsafeURL) as exc:
        raise IngestError(f"could not fetch {url}: {exc}") from exc
    # `requests` falls back to ISO-8859-1 for any `text/html` that names no
    # charset -- the old HTTP/1.1 default -- and the page is almost always
    # UTF-8. Semafor sends exactly that header, so every curly quote arrived
    # as the three characters its UTF-8 bytes spell in Latin-1: an opening
    # quote came through as "a-circumflex, euro, oe". The body is asked
    # instead, and only where the server declined to say.
    if "charset=" not in response.headers.get("Content-Type", "").lower():
        response.encoding = response.apparent_encoding or "utf-8"
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

    # Fetched here rather than left to `article_from_source`, which fetches
    # into a local that never comes back: `_original` was then asked for the
    # page and had nothing, so a URL ingest stored no source at all and
    # Re-parse had nothing to replay -- for the one input that cannot simply
    # be handed in again once the address has moved or gone behind a wall.
    wants_page = upload is None and eml is None and not (text and text.strip())
    if wants_page and html is None and url:
        html = fetch(url)

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
    slug: str | None = None,
    settings: Settings | None = None,
) -> Ingested:
    """Write a parsed article to the library and queue its build.

    Split out of ``ingest`` so ``reparse`` can parse first and only then
    replace what is stored. ``slug`` is what a re-parse passes back, so a
    changed title does not move the article's files out from under it.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)

    try:
        article_id = db.save_article(article, conn, slug=slug)
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

    # Before the build is queued, so the reader has the pictures the moment
    # the article appears. A failure here is not a failure to ingest: the
    # block keeps the address it was parsed with and the reader hotlinks it.
    try:
        pictures.fetch_for(article_id, settings, conn)
    except Exception:
        log.exception("could not store the pictures for %s", row["slug"])

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
    cfg = config(conn)
    if not cfg.ready:
        raise IngestError("summaries need a model and an API key on the Summaries page")
    # The model goes on the job, which is the only durable record of how an
    # article's summaries were made. A summary block does not say where it came
    # from, and a block written by hand is the same row as a generated one.
    options: dict = {"model": cfg.model, "base_url": cfg.base_url}
    if replace:
        options["replace"] = True
    return db.enqueue(article_id, kind="summarise", options=options, conn=conn)


# The cache functions live in `cache.py`: the worker's parent calls the sweep
# and must not import this module to do it. Re-exported, because the reader
# and the tests both reach for them here.
from .cache import (  # noqa: E402,F401
    cache_keys,
    cached_renders,
    library_keys,
    sweep_cache,
)


def _drop_media(slug: str, settings: Settings, *, pictures_too: bool = False) -> int:
    """Empty and remove one article's media directory. Returns files removed.

    Three callers had a copy of this loop. Only the files go: the block cache
    is a separate decision, and the two callers that keep it are the point.

    `images/` sits in the same directory and is not audio. The audio can be
    built again from the blocks; a picture cannot, because the page it came
    from may be gone. So the default steps over the directory — `unlink` would
    raise on it anyway — and only `delete`, which is taking the article too,
    asks for the lot.
    """
    media = settings.media_dir / slug
    if pictures_too:
        removed = sum(1 for path in media.rglob("*") if path.is_file())
        shutil.rmtree(media, ignore_errors=True)
        return removed

    removed = 0
    for child in media.glob("*"):
        if child.is_file():
            child.unlink(missing_ok=True)
            removed += 1
    # `rmdir` refuses a directory with anything left in it, which is the
    # behaviour wanted: the pictures do not go when the audio does.
    if media.is_dir() and not any(media.iterdir()):
        media.rmdir()
    return removed


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
    from .document import VISUAL_KINDS, BlockKind

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
                # Retyped out of a visual, the payload describes nothing on
                # the page any more: a paragraph has no picture to draw.
                if block.kind not in VISUAL_KINDS:
                    block.media = None
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
    _drop_media(row["slug"], settings)
    # A removed figure takes its picture with it. Nothing else collects them:
    # the name is a hash, so no later parse ever overwrites an old one.
    pictures.sweep(row["slug"], article, settings)

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
    if db.has_running_job(article_id, conn):
        raise ArticleBusy(f"a build or summary is running for {row['slug']}; wait for it to finish")

    removed = _drop_media(row["slug"], settings)

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

    The block cache stays, exactly as it does for a hand edit. This called
    `delete_audio`, which also deletes every render only this article wants —
    so dropping a summary sent every *other* block back to the model on the
    next build, minutes of synthesis to undo a paragraph the article is
    keeping. The cache is keyed by the spoken text, so the surviving blocks
    are all still in it and the rebuild is an encode.
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

    # `replace_blocks` clears the timings, the counters and the status; the
    # files on disk are the only part it cannot reach.
    _drop_media(db.get_article(article_id, conn)["slug"], settings)
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
    if db.has_running_job(article_id, conn):
        raise ArticleBusy(f"a build or summary is running for {row['slug']}; wait for it to finish")

    # The whole directory this time, pictures included: nothing is left that
    # could want them.
    _drop_media(row["slug"], settings, pictures_too=True)

    for suffix in SOURCE_SUFFIXES:
        (settings.source_dir / f"{row['slug']}{suffix}").unlink(missing_ok=True)

    db.delete_article(article_id, conn)
    # After the row is gone, not before: the sweep asks what the library still
    # wants, and this article must already have stopped wanting anything. Its
    # renders used to stay on disk for ever, unreachable and uncounted.
    sweep_cache(settings, conn)
    return True


def _summaries_by_section(article: Article) -> dict[str, list[str]]:
    """The summaries an article is carrying, filed under their section title."""
    from .document import BlockKind

    out: dict[str, list[str]] = {}
    for section in article.sections:
        kept = [b.text for b in section.blocks if b.kind is BlockKind.SUMMARY]
        if kept:
            out.setdefault(section.title, []).extend(kept)
    return out


def _restore_summaries(article: Article, carried: dict[str, list[str]]) -> tuple[int, int]:
    """Put the summaries back at the head of the sections they belonged to.

    A summary is a block, and the source an article was parsed from never
    had one — so a re-parse used to delete every summary in the library
    without saying so. Thirty-five of them, across seven articles, each one
    a call to a model.

    The section title is the only handle there is. A section that has been
    renamed or split by the very parser fix being replayed loses its summary,
    and the count says how many.
    """
    from .document import Block, BlockKind

    kept = 0
    remaining = {title: list(texts) for title, texts in carried.items()}
    for section in article.sections:
        texts = remaining.get(section.title)
        if not texts:
            continue
        # At the head, where `summarize` puts them, and in the order they
        # were stored.
        for offset, text in enumerate(texts):
            section.blocks.insert(offset, Block(kind=BlockKind.SUMMARY, text=text))
        kept += len(texts)
        remaining.pop(section.title)
    article.renumber()
    return kept, sum(len(texts) for texts in remaining.values())


def _content(media: dict | None) -> dict | None:
    """A visual's payload without the bookkeeping the store adds to it."""
    if not media:
        return None
    return {k: v for k, v in media.items() if k != "file"}


def _same_article(stored: Article | None, fresh: Article) -> bool:
    """Would replacing one with the other change anything the reader shows?

    Section titles and every block, compared as they would be stored. If they
    match there is nothing to replace, and replacing anyway would take an
    article out of `ready` and orphan audio that is still correct.
    """
    if stored is None:
        return False
    if [s.title for s in stored.sections] != [s.title for s in fresh.sections]:
        return False
    def rows(article: Article):
        # `media["file"]` is written after the article is stored, by the
        # picture fetch, so the stored copy always carries one and a fresh
        # parse never does. Comparing it would mean no article with a picture
        # in it could ever be found unchanged.
        return [
            (b.kind, b.text, _content(b.media), b.footnote_ref) for _s, b in article.blocks()
        ]
    if rows(stored) != rows(fresh):
        return False
    return (stored.title, stored.subtitle, stored.author, stored.source, stored.published_at) == (
        fresh.title, fresh.subtitle, fresh.author, fresh.source, fresh.published_at
    )


def reparse(
    article_id: int,
    adapter: str | None = None,
    settings: Settings | None = None,
    build: bool = False,
) -> Ingested:
    """Re-run the parser over the stored source after a parser fix.

    The new article is parsed *before* the old one is deleted. An earlier
    version deleted first, so a parse error left neither copy behind.

    Summaries are carried across. They are blocks, and no source ever held
    one, so re-parsing used to throw away every summary the library had.

    Two things it does *not* do. It does not queue a build: replaying a parser
    fix over the whole library queued one per article, which is the CPU for
    the rest of the day and nobody asked for it. And where the new parse is
    the old one, it replaces nothing at all — the ids would not have moved, so
    the audio is still correct and taking the article out of `ready` would
    have been a lie about it.
    """
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        raise IngestError(f"no article {article_id}")
    if db.has_running_job(article_id, conn):
        raise ArticleBusy(f"a build or summary is running for {row['slug']}; wait for it to finish")

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

    stored_article = db.load_article(article_id, conn)
    kept, lost = _restore_summaries(
        article, _summaries_by_section(stored_article) if stored_article else {}
    )
    if lost:
        log.warning("%s: %d summary block(s) had no section to return to", row["slug"], lost)

    if _same_article(stored_article, article):
        log.info("%s re-parsed to exactly what was stored; left alone", row["slug"])
        return Ingested(
            article_id=article_id,
            slug=row["slug"],
            title=row["title"],
            word_count=row["word_count"],
            series=row["series"],
            job_id=None,
            tags=tags,
            summaries_kept=kept,
            unchanged=True,
        )

    # The audio belongs to the block ids that just moved, so it is wrong now.
    # It used to be orphaned instead — a directory under the old slug that
    # nothing could reach. Keeping the slug means these files stay reachable,
    # which is only an improvement if they also go.
    dropped = _drop_media(row["slug"], settings)
    for path in cached_renders(article_id, conn, settings):
        path.unlink(missing_ok=True)
        dropped += 1
    if dropped:
        log.info("%s: dropped %d stale audio file(s) before re-parsing", row["slug"], dropped)

    db.delete_article(article_id, conn)
    result = store(
        article,
        original=(raw, suffix),
        tags=tags,
        options=options,
        build=build,
        # The address it already has. A re-parse that finds a better title
        # would otherwise move it, and `media/<old-slug>/`, its pictures and
        # `sources/<old-slug>.*` would be left with nothing able to reach
        # them — nothing sweeps by slug. What the slug reads is of no interest
        # to anyone; what it points at is.
        slug=row["slug"],
        settings=settings,
    )
    return replace(result, summaries_kept=kept, summaries_lost=lost)


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
