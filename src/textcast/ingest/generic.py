"""Fallback extractor for everything without a dedicated adapter."""

from __future__ import annotations

from urllib.parse import urlparse

from ..document import Article
from .base import blocks_from_dom, finish, text_of
from .dom import Tree, meta, select_one
from .extract import article_body


class GenericAdapter:
    name = "generic"

    def matches(self, url: str, tree: Tree) -> bool:
        return True  # last in the registry, so this is the catch-all

    def parse(self, tree: Tree, url: str = "") -> Article:
        title = (
            meta(tree, property="og:title")
            or text_of(select_one(tree, "h1"))
            or text_of(select_one(tree, "title"))
            or "Untitled"
        )
        subtitle = meta(tree, property="og:description", name="description")
        source = meta(tree, property="og:site_name") or _host(url)
        author = meta(tree, name="author", property="article:author")
        published = meta(tree, property="article:published_time", itemprop="datePublished")

        body = article_body(tree)

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                sections=blocks_from_dom(body),
                source=source,
                url=url,
                author=author,
                published_at=published or None,
            )
        )


def _host(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") if url else ""
