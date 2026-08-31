"""Bloomberg, and Money Stuff in particular.

Ported from ``html-to-text.ipynb``. Bloomberg ships hashed CSS module class
names, so every selector here is a prefix match: ``HedAndDek_dek-abc123``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..document import Article, Block, BlockKind
from .base import blocks_from_dom, clean, drop, finish, inline_footnotes, text_of

_DEK = re.compile(r"HedAndDek_dek")
_SUBHEAD = re.compile(r"Subhead_subhead")
_FOOTNOTES = re.compile(r"Footnotes_base")

#: Money Stuff ends with a links roundup and a subscription pitch.
STOP_AT = "if you'd like to get money stuff in handy email form"


class BloombergAdapter:
    name = "bloomberg"

    def matches(self, url: str, soup: BeautifulSoup) -> bool:
        if "bloomberg.com" in url:
            return True
        return soup.find(class_=_DEK) is not None or soup.find("ol", class_=_FOOTNOTES) is not None

    def parse(self, soup: BeautifulSoup, url: str = "") -> Article:
        h1 = soup.find("h1")
        title = text_of(h1) if h1 else "Untitled"

        dek = soup.find(class_=_DEK)
        subtitle = text_of(dek) if dek else ""

        footnotes = self._collect_footnotes(soup)
        inline_footnotes(soup, footnotes, {"data-component": "footnote-link"})

        container = soup.find("main") or soup.body or soup
        drop(container, [
            "figure", "aside", "nav", "script", "style", "noscript",
            "[data-component=in-this-article]", "[class*=InThisArticle]",
            "[class*=Recirc]", "[class*=Paywall]",
        ])
        for node in container.find_all("ol", class_=_FOOTNOTES):
            node.decompose()

        sections = blocks_from_dom(
            container,
            heading_tags=("h2", "h3"),
            stop_at=STOP_AT,
        )

        # The dek is repeated as a paragraph in the body; drop that copy.
        if subtitle:
            for section in sections:
                section.blocks = [
                    b for b in section.blocks if not (b.kind is BlockKind.PARA and b.text == subtitle)
                ]

        article = Article(
            title=title,
            subtitle=subtitle,
            sections=sections,
            source="Bloomberg",
            url=url,
            series="Money Stuff" if "money stuff" in title.lower() or _is_money_stuff(soup) else None,
        )
        return finish(article)

    def _collect_footnotes(self, soup: BeautifulSoup) -> dict[str, str]:
        out: dict[str, str] = {}
        ol = soup.find("ol", class_=_FOOTNOTES)
        if not ol:
            return out
        for i, li in enumerate(ol.find_all("li"), start=1):
            for a in li.find_all("a"):
                if "view in article" in a.get_text(strip=True).lower():
                    a.decompose()
            body = clean(li.get_text(" ", strip=True))
            if body:
                out[str(i)] = body
        return out


def _is_money_stuff(soup: BeautifulSoup) -> bool:
    """Money Stuff issues live under /opinion/newsletters/ and carry the brand.

    The canonical URL alone is not enough: it is dated and slugged from the
    headline, with no mention of the newsletter it belongs to.
    """
    canonical = soup.find("link", rel="canonical")
    href = (canonical.get("href", "") if canonical else "").lower()
    if "/newsletters/" not in href:
        return False
    return "money-stuff" in str(soup).lower() or "money stuff" in soup.get_text(" ")[:20000].lower()


def footnote_blocks(footnotes: dict[str, str]) -> list[Block]:
    """Footnotes as standalone blocks, for the reader's end-of-article list."""
    return [
        Block(kind=BlockKind.FOOTNOTE, text=f"Footnote {num}. {body}", footnote_ref=num)
        for num, body in sorted(footnotes.items(), key=lambda kv: int(kv[0]))
    ]
