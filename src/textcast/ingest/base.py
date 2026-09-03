"""Shared parsing machinery.

Every adapter walks a DOM into blocks with the same function, so quote
handling, list numbering and whitespace repair behave identically no matter
where the article came from.
"""

from __future__ import annotations

import re
from typing import Protocol

from ..document import Article, Block, BlockKind, Section
from .dom import Node, Tree, ancestor_tags, children, clean, drop, parse, text_of
from .visuals import NO_VISUALS, VisualRules, visual_block

BLOCK_SELECTOR = "h1, h2, h3, h4, blockquote, p, ol, ul"

#: The tags `BLOCK_SELECTOR` matches. Anything else the walk stops at came
#: from the visual half of the selector, so this is how one is told from the
#: other without asking the matcher which half fired.
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "blockquote", "p", "ol", "ul"}

#: Nested inside these, a paragraph belongs to the parent block, not itself.
_ENCLOSING = {"blockquote", "li", "figure"}


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
    visuals: VisualRules = NO_VISUALS,
    base_url: str = "",
) -> list[Section]:
    """Walk a content container into sections of blocks.

    ``skip`` and ``stop_at`` are lowercase text prefixes: the first drops a
    paragraph, the second ends parsing (newsletter sign-off boilerplate).

    ``visuals`` says what pictures, tables and charts this publication draws.
    They are matched in the *same* pass as the prose, because a chart's place
    in the argument is the whole of what it means. Off by default: a walk that
    has not been told what a publication's furniture looks like keeps text
    only, which is what every parser did before visuals existed.
    """
    sections: list[Section] = []
    current = Section(title="")
    headings = set(heading_tags) | {"h1"}

    if visuals.enabled and visuals.drop:
        drop(container, list(visuals.drop))
    selector = f"{BLOCK_SELECTOR}, {visuals.selector}" if visuals.enabled else BLOCK_SELECTOR

    #: Visual containers that already produced a block. Their contents belong
    #: to them, so a caption is not read again as a paragraph and a picture is
    #: not emitted a second time as a bare `img`.
    consumed: list[Node] = []
    seen: set[str] = set()

    for elem in container.css(selector):
        # lexbor's `css` searches the node as well as its subtree, so the
        # container matches its own selectors. Left in, an article whose
        # wrapper is called "...no-full-width-graphics" is one figure.
        if elem == container or _within(elem, consumed):
            continue

        if elem.tag not in _BLOCK_TAGS:
            block = visual_block(elem, visuals, base_url=base_url, stop=container)
            if block is None:
                continue
            key = _visual_key(block)
            if key in seen:
                continue
            seen.add(key)
            consumed.append(elem)
            current.blocks.append(block)
            continue

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
            current.blocks.append(Block(kind=BlockKind.QUOTE, text=quoted(elem) or text))
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


def quoted(node: Node) -> str:
    """A block quote's text, with the breaks the publication put in it.

    `text_of` joins every descendant with a space, which reads a pull quote
    of three paragraphs as one and runs a bolded lead-in straight into the
    sentence after it — the SpaceX prospectus quote came out as "...Compute
    Services Agreements with Third Parties We believe our compute...".

    One block either way: a quote is one thing to highlight and one thing to
    seek to. The paragraphs live inside its text as blank lines, which is
    what `to_markdown` already expected and what the reader now renders.
    """
    parts = [text_of(child) for child in node.css("p, li")]
    parts = [part for part in parts if part]
    return "\n\n".join(parts) if len(parts) > 1 else ""


def _within(node: Node, containers: list[Node]) -> bool:
    """True when ``node`` sits inside something already emitted."""
    if not containers:
        return False
    # lexbor hands out a fresh wrapper per lookup, so two objects for one
    # node are not `is` each other. They compare equal; identity does not.
    current = node.parent
    while current is not None:
        if any(current == done for done in containers):
            return True
        current = current.parent
    return False


def _visual_key(block: Block) -> str:
    """What makes two visuals the same one.

    A publication often prints the lead picture twice — once in the topper and
    once at the head of the body — and a saved page keeps both. The address is
    the thing that says so; a table has none, so its cells stand in.
    """
    media = block.media or {}
    return media.get("src") or f"{block.kind}:{block.text}:{media.get('rows')}"


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
    "print this page", "comments", "leave a comment",
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
