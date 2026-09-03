"""Every picture an article cites, kept beside its audio.

Parsed pictures used to be hotlinked, which cost three things at once: an
article kept offline showed nothing, a paywalled image answered 403 to a
reader who was not signed in, and the publication learned the reader's
address. Storage is the cheap one of the four, so the picture is fetched once
and stored.

It lives in ``media/<slug>/images/`` — beside the audio, not in with it. The
audio can be thrown away and rebuilt; a picture cannot, because the page it
came from may be gone. So `delete_audio` leaves the directory alone and only
`service.delete` takes it, with the article.

The name is a hash of the address it came from, so a re-parse rewrites nothing
and two blocks quoting the same chart share one file.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import db
from .document import VISUAL_KINDS
from .settings import Settings, get_settings

log = logging.getLogger("textcast.pictures")

USER_AGENT = "Mozilla/5.0 (compatible; textcast/0.2; +https://github.com/abdullahau/textcast)"

#: A publication that draws thirty charts in one post is drawing furniture.
MAX_PICTURES = 40
#: One picture. A newspaper's largest raw JPEG is a few megabytes.
MAX_BYTES = 12 * 1024 * 1024
#: Every picture in one article, so a gallery cannot fill the disk.
MAX_TOTAL_BYTES = 80 * 1024 * 1024
#: Fetches at once. The same number the summary pass uses, for the same reason.
MAX_PARALLEL = 4
TIMEOUT = 20.0

#: What each image type is called on disk. Read off the Content-Type, because
#: an FT address ends in `?source=next-article` and has no extension at all.
SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}


def images_dir(slug: str, settings: Settings) -> Path:
    return settings.media_dir / slug / "images"


def stored_name(url: str, suffix: str) -> str:
    return _key(url) + suffix


def _key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def already_stored(directory: Path, url: str) -> str | None:
    """The file this address was stored as last time, if it is still there.

    A re-parse rebuilds every block from scratch, so no block remembers its
    file. Without this, re-parsing nine articles would download every picture
    in the library again to write the bytes that are already on disk.
    """
    if not directory.is_dir():
        return None
    for path in directory.glob(_key(url) + ".*"):
        if path.is_file() and path.stat().st_size:
            return path.name
    return None


def _suffix_for(url: str, content_type: str) -> str:
    known = SUFFIXES.get(content_type.split(";")[0].strip().lower())
    if known:
        return known
    tail = Path(urlparse(url).path).suffix.lower()
    return tail if tail in set(SUFFIXES.values()) else ".img"


def _download(url: str) -> tuple[bytes, str] | None:
    try:
        response = requests.get(
            url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if not content_type.lower().startswith("image/"):
            log.info("not a picture, skipped: %s (%s)", url, content_type or "no type")
            return None
        body = response.content
    except requests.RequestException as exc:
        log.warning("could not fetch %s: %s", url, exc)
        return None
    if not body or len(body) > MAX_BYTES:
        log.warning("refused %s: %d bytes", url, len(body))
        return None
    return body, _suffix_for(url, content_type)


def fetch_for(
    article_id: int,
    settings: Settings | None = None,
    conn=None,
) -> int:
    """Copy this article's pictures into its media directory. Returns how many.

    Failure is never fatal. A picture that will not download keeps its remote
    address in `block.media["src"]`, and the reader falls back to it — a
    hotlinked picture is worse than a stored one and better than none.
    """
    settings = settings or get_settings()
    conn = conn or db.connect(settings.db_path)
    row = db.get_article(article_id, conn)
    if row is None:
        return 0
    article = db.load_article(article_id, conn)
    if article is None:
        return 0

    wanted: dict[str, list] = {}
    for _section, block in article.blocks():
        if block.kind not in VISUAL_KINDS or not block.media:
            continue
        src = block.media.get("src") or ""
        if src.startswith(("http://", "https://")) and not block.media.get("file"):
            wanted.setdefault(src, []).append(block)
    if not wanted:
        return 0

    urls = list(wanted)[:MAX_PICTURES]
    if len(wanted) > MAX_PICTURES:
        log.info("%s cites %d pictures; taking the first %d", row["slug"], len(wanted), MAX_PICTURES)

    directory = images_dir(row["slug"], settings)
    directory.mkdir(parents=True, exist_ok=True)

    def record(url: str, name: str) -> None:
        for block in wanted[url]:
            media = dict(block.media or {})
            media["file"] = name
            db.set_block_media(article_id, block.id, media, conn)

    on_disk = {url: already_stored(directory, url) for url in urls}
    for url, name in on_disk.items():
        if name:
            record(url, name)

    missing = [url for url, name in on_disk.items() if not name]
    stored, budget = 0, MAX_TOTAL_BYTES
    if missing:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            for url, result in zip(missing, pool.map(_download, missing), strict=True):
                if result is None:
                    continue
                body, suffix = result
                if len(body) > budget:
                    log.warning("%s is over its picture budget; stopping", row["slug"])
                    break
                budget -= len(body)
                name = stored_name(url, suffix)
                (directory / name).write_bytes(body)
                record(url, name)
                stored += 1

    if stored:
        log.info("stored %d picture(s) for %s", stored, row["slug"])
    # The blocks have moved on: read them back and drop whatever nothing
    # points at any more. A re-parse that lost a picture leaves one here.
    sweep(row["slug"], db.load_article(article_id, conn), settings)
    return stored


def sweep(slug: str, article, settings: Settings) -> int:
    """Delete stored pictures no block wants any more. Returns how many.

    A re-parse can drop a picture the old parse kept, and an edited article
    can lose one with the block that cited it. Nothing else collects them:
    the name is a hash, so a changed address writes a new file beside the old.
    """
    directory = images_dir(slug, settings)
    if not directory.is_dir():
        return 0
    if article is None:
        return 0
    keep = {
        block.media["file"]
        for _section, block in article.blocks()
        if block.media and block.media.get("file")
    }
    removed = 0
    for path in directory.iterdir():
        if path.is_file() and path.name not in keep:
            path.unlink(missing_ok=True)
            removed += 1
    if not any(directory.iterdir()):
        directory.rmdir()
    return removed
