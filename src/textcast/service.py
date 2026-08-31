"""Ingestion, shared by the CLI and the web app.

Everything that turns raw input into a stored, queued article lives here so the
two front ends cannot drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from . import db
from .document import Article
from .ingest import parse_html
from .ingest.newsletter import article_from_eml
from .settings import Settings, get_settings

log = logging.getLogger("textcast.service")

USER_AGENT = "Mozilla/5.0 (compatible; textcast/0.2; +https://github.com/abdullahau/textcast)"

#: Below this a "parse" is almost certainly a paywall page, not an article.
MIN_WORDS = 60


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
    adapter: str | None = None,
) -> Article:
    """Parse one of the accepted input forms into an Article."""
    if eml is not None:
        return article_from_eml(eml, url=url or "")
    if html is None:
        if not url:
            raise IngestError("give html, a url, or an eml message")
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
    adapter: str | None = None,
    build: bool = True,
    options: dict | None = None,
    settings: Settings | None = None,
) -> Ingested:
    """Parse, store, and (by default) queue a build.

    A re-ingest of identical content returns the existing article rather than
    creating a second copy — the same issue often arrives by both email and
    share sheet.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)

    article = article_from_source(html=html, url=url, eml=eml, adapter=adapter)
    if article.word_count < MIN_WORDS:
        raise IngestError(
            f"only {article.word_count} words extracted — the page is probably a "
            "login wall. Save it from a logged-in browser and upload the HTML."
        )

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
    raw = eml if eml is not None else (html or "").encode("utf-8", errors="replace")
    if raw:
        store_source(row["slug"], raw, ".eml" if eml is not None else ".html", settings)

    job_id = None
    if build and _should_build(article, conn):
        job_id = db.enqueue(article_id, options=options, conn=conn)

    return Ingested(
        article_id=article_id,
        slug=row["slug"],
        title=article.title,
        word_count=article.word_count,
        series=article.series,
        job_id=job_id,
    )


def _should_build(article: Article, conn) -> bool:
    """Respect a series that has auto-build switched off."""
    if not article.series:
        return True
    row = db.get_series(article.series, conn)
    return True if row is None else bool(row["auto_build"])


def rebuild(article_id: int, options: dict | None = None, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    if db.get_article(article_id, conn) is None:
        raise IngestError(f"no article {article_id}")
    return db.enqueue(article_id, options=options, conn=conn)


def reparse(article_id: int, adapter: str | None = None, settings: Settings | None = None) -> Ingested:
    """Re-run the parser over the stored source after a parser fix."""
    settings = settings or get_settings()
    conn = db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        raise IngestError(f"no article {article_id}")

    for suffix in (".html", ".eml"):
        path = settings.source_dir / f"{row['slug']}{suffix}"
        if path.exists():
            break
    else:
        raise IngestError(f"no stored source for {row['slug']}")

    db.delete_article(article_id, conn)
    raw = path.read_bytes()
    if suffix == ".eml":
        return ingest(eml=raw, url=row["url"], adapter=adapter, settings=settings)
    return ingest(html=raw.decode("utf-8", errors="replace"), url=row["url"], adapter=adapter, settings=settings)
