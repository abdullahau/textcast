"""Financial Times."""

from __future__ import annotations

from bs4 import BeautifulSoup

from ..document import Article
from .base import blocks_from_dom, drop, finish, first_text, text_of


class FTAdapter:
    name = "ft"

    def matches(self, url: str, soup: BeautifulSoup) -> bool:
        if "ft.com" in url:
            return True
        return soup.select_one(".o-topper__standfirst, .article__content-body") is not None

    def parse(self, soup: BeautifulSoup, url: str = "") -> Article:
        h1 = soup.find("h1")
        title = text_of(h1) if h1 else "Untitled"
        subtitle = first_text(soup, [".o-topper__standfirst", "[class*=standfirst]"])

        container = (
            soup.select_one(".article__content-body")
            or soup.select_one("article")
            or soup.find("main")
            or soup.body
            or soup
        )
        drop(container, [
            "figure", "aside", "nav", "script", "style", "noscript",
            ".o-ads", ".o-teaser", "[class*=onward]", "[class*=follow]",
            "[class*=share]", "[class*=Share]",
            "[class*=related]", "[data-trackable=teaser]",
        ])

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                sections=blocks_from_dom(container),
                source="Financial Times",
                url=url,
                author=first_text(soup, [".n-content-tag--author", "[class*=byline]"]),
            )
        )
