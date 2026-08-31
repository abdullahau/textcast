"""Shared parsing machinery.

Every adapter walks a DOM into blocks with the same function, so quote
handling, list numbering and whitespace repair behave identically no matter
where the article came from.
"""

from __future__ import annotations

import re
from typing import Protocol

from bs4 import BeautifulSoup, Tag

from ..document import Article, Block, BlockKind, Section

#: Space before punctuation, left behind by ``get_text(separator=" ")``.
_TIGHTEN = re.compile(r"\s+([,.!?;:’'”\)\]])")
_COLLAPSE = re.compile(r"\s+")
_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "blockquote", "p", "ol", "ul"]


class Adapter(Protocol):
    name: str

    def matches(self, url: str, soup: BeautifulSoup) -> bool: ...

    def parse(self, soup: BeautifulSoup, url: str = "") -> Article: ...


def clean(text: str) -> str:
    return _TIGHTEN.sub(r"\1", _COLLAPSE.sub(" ", text)).strip()


def text_of(node: Tag) -> str:
    return clean(node.get_text(separator=" ", strip=True))


def blocks_from_dom(
    container: Tag,
    *,
    heading_tags: tuple[str, ...] = ("h2", "h3"),
    skip: str | None = None,
    stop_at: str | None = None,
) -> list[Section]:
    """Walk a content container into sections of blocks.

    ``skip`` and ``stop_at`` are lowercase text prefixes: the first drops a
    paragraph, the second ends parsing (newsletter sign-off boilerplate).
    """
    sections: list[Section] = []
    current = Section(title="")
    seen: set[int] = set()

    for elem in container.find_all(_BLOCK_TAGS, recursive=True):
        # A <p> inside a <blockquote> or <li> is handled by its parent.
        if elem.find_parent(["blockquote", "li"]):
            continue
        if id(elem) in seen:
            continue
        seen.add(id(elem))

        text = text_of(elem)
        if not text:
            continue
        low = text.lower()
        if stop_at and low.startswith(stop_at):
            break
        if skip and low.startswith(skip):
            continue

        if elem.name in heading_tags or elem.name == "h1":
            if current.blocks:
                sections.append(current)
            current = Section(title=text)
            continue

        if elem.name == "blockquote":
            current.blocks.append(Block(kind=BlockKind.QUOTE, text=text))
            continue

        if elem.name in ("ol", "ul"):
            ordered = elem.name == "ol"
            for n, li in enumerate(elem.find_all("li", recursive=False) or elem.find_all("li"), start=1):
                item = text_of(li)
                if item:
                    prefix = f"{n}. " if ordered else ""
                    current.blocks.append(Block(kind=BlockKind.LIST_ITEM, text=f"{prefix}{item}"))
            continue

        current.blocks.append(Block(kind=BlockKind.PARA, text=text))

    if current.blocks:
        sections.append(current)
    return sections


def inline_footnotes(soup: BeautifulSoup, footnotes: dict[str, str], link_selector: dict) -> None:
    """Replace each footnote marker with the footnote itself, in place.

    This is the feature that started the project: a footnote read where it is
    cited, not collected at the end where it has lost its context.
    """
    for link in soup.find_all("a", attrs=link_selector):
        href = link.get("href", "")
        match = re.search(r"footnote-(\d+)", href)
        if not match:
            continue
        num = match.group(1)
        body = footnotes.get(num, "")
        if body:
            link.replace_with(f" [Footnote {num}: {body}] ")


def drop(soup: BeautifulSoup, selectors: list[str]) -> None:
    for selector in selectors:
        for node in soup.select(selector):
            node.decompose()


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = text_of(node)
            if text:
                return text
    return ""


#: Whole sections that are site furniture, matched on the section title.
JUNK_SECTIONS = re.compile(
    r"^(follow the topics|latest on |more from|related|most read|recommended|"
    r"you might also|sign ?up|subscribe|comments|share this|explore the series)",
    re.I,
)

#: Single-line UI text that survives content extraction.
JUNK_BLOCKS = {
    "in this article", "add to myft", "share", "save", "listen", "print",
    "gift this article", "read next", "advertisement", "skip to content",
    "sign up", "follow", "more on this topic", "open in app", "copy link",
    "reuse this content", "explore more", "loading", "see all",
}


#: Share rows and link furniture, which survive as ordinary list items.
JUNK_PATTERNS = re.compile(
    r"\(opens in a new window\)"
    r"|^share on \w+"
    r"|\bon (x|twitter|facebook|linkedin|whatsapp|threads|bluesky|reddit)\b.{0,30}$"
    r"|^(copy|copied) link"
    r"|^\d+ (min|minute)s? read$",
    re.I,
)


def is_junk_block(text: str) -> bool:
    stripped = text.strip()
    return stripped.lower() in JUNK_BLOCKS or bool(JUNK_PATTERNS.search(stripped))


def prune(article: Article) -> Article:
    """Strip site furniture that content extraction leaves behind.

    Runs for every adapter, so a widget one publication adds does not need a
    fix in each parser.
    """
    kept = []
    for section in article.sections:
        if JUNK_SECTIONS.match(section.title or ""):
            continue
        section.blocks = [b for b in section.blocks if not is_junk_block(b.text)]
        if section.blocks:
            kept.append(section)
    article.sections = kept
    return article


def finish(article: Article) -> Article:
    """Prune furniture, drop empty sections, title the first one, renumber."""
    prune(article)
    article.sections = [s for s in article.sections if s.blocks]
    if article.sections and not article.sections[0].title:
        article.sections[0].title = article.title
    return article.renumber()
