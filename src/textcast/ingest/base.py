"""Shared parsing machinery.

Every adapter walks a DOM into blocks with the same function, so quote
handling, list numbering and whitespace repair behave identically no matter
where the article came from.
"""

from __future__ import annotations

import re
from typing import Protocol

from ..document import Article, Block, BlockKind, Section
from .dom import Node, Tree, ancestor_tags, children, clean, parse, text_of

BLOCK_SELECTOR = "h1, h2, h3, h4, blockquote, p, ol, ul"

#: Nested inside these, a paragraph belongs to the parent block, not itself.
_ENCLOSING = {"blockquote", "li"}


class Adapter(Protocol):
    name: str

    def matches(self, url: str, tree: Tree) -> bool: ...

    def parse(self, tree: Tree, url: str = "") -> Article: ...


def blocks_from_dom(
    container: Node,
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
    headings = set(heading_tags) | {"h1"}

    for elem in container.css(BLOCK_SELECTOR):
        # A <p> inside a <blockquote> or <li> is emitted by its parent.
        if ancestor_tags(elem, _ENCLOSING, stop=container):
            continue
        # A list nested inside another list likewise.
        if elem.tag in ("ol", "ul") and ancestor_tags(elem, {"ol", "ul"}, stop=container):
            continue

        text = text_of(elem)
        if not text:
            continue
        low = text.lower()
        if stop_at and low.startswith(stop_at):
            break
        if skip and low.startswith(skip):
            continue

        if elem.tag in headings:
            if current.blocks:
                sections.append(current)
            current = Section(title=text)
            continue

        if elem.tag == "blockquote":
            current.blocks.append(Block(kind=BlockKind.QUOTE, text=text))
            continue

        if elem.tag in ("ol", "ul"):
            ordered = elem.tag == "ol"
            items = children(elem, "li") or elem.css("li")
            for n, li in enumerate(items, start=1):
                item = text_of(li)
                if item:
                    prefix = f"{n}. " if ordered else ""
                    current.blocks.append(Block(kind=BlockKind.LIST_ITEM, text=f"{prefix}{item}"))
            continue

        current.blocks.append(Block(kind=BlockKind.PARA, text=text))

    if current.blocks:
        sections.append(current)
    return sections


def inline_footnotes(scope: Node | Tree, footnotes: dict[str, str], selector: str) -> None:
    """Replace each footnote marker with the footnote itself, in place.

    This is the feature that started the project: a footnote read where it is
    cited, not collected at the end where it has lost its context.
    """
    for link in scope.css(selector):
        href = link.attributes.get("href") or ""
        match = re.search(r"footnote-(\d+)", href)
        if not match:
            continue
        body = footnotes.get(match.group(1), "")
        if body:
            link.replace_with(f" [Footnote {match.group(1)}: {body}] ")


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


# Re-exported so adapters import one module.
make_tree = parse
__all__ = [
    "Adapter",
    "BLOCK_SELECTOR",
    "JUNK_BLOCKS",
    "JUNK_PATTERNS",
    "JUNK_SECTIONS",
    "blocks_from_dom",
    "clean",
    "finish",
    "inline_footnotes",
    "is_junk_block",
    "make_tree",
    "prune",
    "text_of",
]
