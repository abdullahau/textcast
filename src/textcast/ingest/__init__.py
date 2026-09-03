"""Adapter registry.

Order matters: the first adapter whose ``matches`` returns true wins, and
``GenericAdapter`` is last because it always matches. Adding a publication is
one file plus one line here.
"""

from __future__ import annotations

from ..document import Article
from .base import Adapter
from .bloomberg import BloombergAdapter
from .dom import Tree, parse
from .ft import FTAdapter
from .generic import GenericAdapter
from .newsletter import NewsletterAdapter
from .substack import SubstackAdapter

ADAPTERS: list[Adapter] = [
    BloombergAdapter(),
    FTAdapter(),
    # Before the newsletter adapter: a Substack issue arriving by email
    # matches both, and this one knows where the pictures and the byline are.
    SubstackAdapter(),
    NewsletterAdapter(),
    GenericAdapter(),
]

_BY_NAME = {a.name: a for a in ADAPTERS}


def pick_adapter(url: str, tree: Tree) -> Adapter:
    for adapter in ADAPTERS:
        try:
            if adapter.matches(url, tree):
                return adapter
        except Exception:
            continue
    return _BY_NAME["generic"]


def parse_html(html: str, url: str = "", prefer: str | None = None) -> Article:
    """Parse a page into an Article, choosing the adapter automatically.

    The tree is parsed once per attempt because adapters mutate it in place
    (dropping noise, inlining footnotes).
    """
    adapter = _BY_NAME.get(prefer) if prefer else None
    if adapter is None:
        adapter = pick_adapter(url, parse(html))

    article = adapter.parse(parse(html), url=url)
    article.adapter = adapter.name
    if not article.source:
        article.source = adapter.name
    return article


def adapter_names() -> list[str]:
    return [a.name for a in ADAPTERS]


__all__ = ["ADAPTERS", "Adapter", "Article", "adapter_names", "parse_html", "pick_adapter"]
