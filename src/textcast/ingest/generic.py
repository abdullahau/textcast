"""Fallback extractor for everything without a dedicated adapter.

Uses readability to isolate the article body, then hands the cleaned DOM to the
same walker every other adapter uses.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..document import Article
from .base import blocks_from_dom, drop, finish, first_text, make_soup, text_of

_NOISE = [
    "script", "style", "noscript", "nav", "aside", "footer", "header",
    "form", "iframe", "figure", "figcaption",
    "[role=navigation]", "[role=banner]", "[role=complementary]",
    ".advertisement", ".ad", ".newsletter-signup", ".related-posts",
    ".share", ".social", ".comments", ".cookie", ".paywall",
]


class GenericAdapter:
    name = "generic"

    def matches(self, url: str, soup: BeautifulSoup) -> bool:
        return True  # last in the registry, so this is the catch-all

    def parse(self, soup: BeautifulSoup, url: str = "") -> Article:
        title = self._title(soup)
        subtitle = first_text(soup, ["meta[property='og:description']", ".standfirst", ".subtitle", ".dek"])
        if not subtitle:
            meta = soup.find("meta", attrs={"name": "description"})
            subtitle = (meta.get("content", "") if meta else "").strip()

        container = self._body(soup)
        drop(container, _NOISE)
        sections = blocks_from_dom(container)

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                sections=sections,
                source=first_text(soup, ["meta[property='og:site_name']"]) or _host(url),
                url=url,
                author=self._author(soup),
            )
        )

    def _title(self, soup: BeautifulSoup) -> str:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            return og["content"].strip()
        h1 = soup.find("h1")
        if h1:
            return text_of(h1)
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return "Untitled"

    def _author(self, soup: BeautifulSoup) -> str:
        for attrs in ({"name": "author"}, {"property": "article:author"}):
            meta = soup.find("meta", attrs=attrs)
            if meta and meta.get("content"):
                return meta["content"].strip()
        return ""

    def _body(self, soup: BeautifulSoup) -> BeautifulSoup:
        """Isolate the article body, preferring readability when it is installed."""
        try:
            from readability import Document

            extracted = Document(str(soup)).summary(html_partial=True)
            candidate = make_soup(extracted)
            if len(candidate.get_text(strip=True)) > 400:
                return candidate
        except Exception:
            pass

        for selector in ("article", "main", "[role=main]", ".post-content", ".entry-content", "#content"):
            node = soup.select_one(selector)
            if node and len(node.get_text(strip=True)) > 400:
                return node
        return soup.body or soup


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.removeprefix("www.") if url else ""
