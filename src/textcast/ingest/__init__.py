"""Adapter registry.

Order matters: the first adapter whose ``matches`` returns true wins, and
``GenericAdapter`` is last because it always matches. Adding a publication is
one file plus one line here.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..document import Article
from .base import Adapter, make_soup
from .bloomberg import BloombergAdapter
from .ft import FTAdapter
from .generic import GenericAdapter
from .newsletter import NewsletterAdapter

ADAPTERS: list[Adapter] = [
    BloombergAdapter(),
    FTAdapter(),
    NewsletterAdapter(),
    GenericAdapter(),
]

_BY_NAME = {a.name: a for a in ADAPTERS}


def pick_adapter(url: str, soup: BeautifulSoup) -> Adapter:
    for adapter in ADAPTERS:
        try:
            if adapter.matches(url, soup):
                return adapter
        except Exception:
            continue
    return _BY_NAME["generic"]


def parse_html(
    html: str,
    url: str = "",
    prefer: str | None = None,
    soup: BeautifulSoup | None = None,
) -> Article:
    """Parse a page into an Article, choosing the adapter automatically."""
    soup = soup if soup is not None else make_soup(html)
    adapter = _BY_NAME[prefer] if prefer in _BY_NAME else pick_adapter(url, soup)
    article = adapter.parse(soup, url=url)
    if not article.source:
        article.source = adapter.name
    return article


def adapter_names() -> list[str]:
    return [a.name for a in ADAPTERS]


__all__ = ["ADAPTERS", "Adapter", "Article", "adapter_names", "parse_html", "pick_adapter"]
