"""Thin DOM helpers over selectolax's lexbor parser.

lexbor parses the corpus about five times faster than BeautifulSoup on lxml,
in one small wheel with no dependencies of its own. Its CSS attribute matching
also expresses Bloomberg's hashed class names — ``[class*="Footnotes_base"]``
— more directly than a regex over the class list.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser, LexborNode

Node = LexborNode
Tree = LexborHTMLParser

#: Space left before punctuation by separator-joined text extraction.
_TIGHTEN = re.compile(r"\s+([,.!?;:’'”\)\]])")
_COLLAPSE = re.compile(r"[\s ]+")


def parse(html: str) -> Tree:
    return LexborHTMLParser(html)


def clean(text: str) -> str:
    return _TIGHTEN.sub(r"\1", _COLLAPSE.sub(" ", text)).strip()


def text_of(node: Node | None) -> str:
    if node is None:
        return ""
    return clean(node.text(separator=" ", strip=True))


def root(tree: Tree) -> Node:
    return tree.body or tree.root


def select(scope: Node | Tree, selector: str) -> list[Node]:
    return scope.css(selector)


def select_one(scope: Node | Tree, selector: str) -> Node | None:
    return scope.css_first(selector)


def attr(node: Node | None, name: str, default: str = "") -> str:
    if node is None:
        return default
    return node.attributes.get(name) or default


def drop(scope: Node | Tree, selectors: list[str]) -> None:
    """Remove matching nodes. Unknown selectors are skipped, not fatal."""
    for selector in selectors:
        try:
            for node in scope.css(selector):
                node.decompose()
        except Exception:
            continue


def ancestor_tags(node: Node, tags: set[str], stop: Node | None = None) -> bool:
    """True when any ancestor of ``node`` has one of ``tags``.

    ``stop`` is compared with ``!=``. lexbor hands out a fresh wrapper object
    per lookup, so the container a caller holds is never ``is`` the one this
    walk arrives at, and an identity test would walk past it to the root.
    """
    current = node.parent
    while current is not None and current != stop:
        if current.tag in tags:
            return True
        current = current.parent
    return False


def children(node: Node, tag: str) -> list[Node]:
    """Direct children with the given tag, so nested lists stay with their own list."""
    return [child for child in node.iter(include_text=False) if child.tag == tag]


def first_text(scope: Node | Tree, selectors: list[str]) -> str:
    for selector in selectors:
        node = select_one(scope, selector)
        if node is not None:
            text = text_of(node)
            if text:
                return text
    return ""


def meta(tree: Tree, **lookups: str) -> str:
    """Read a meta tag's content, trying each ``attribute=value`` in turn."""
    for name, value in lookups.items():
        node = tree.css_first(f'meta[{name}="{value}"]')
        content = attr(node, "content").strip()
        if content:
            return content
    return ""


def link_density(node: Node) -> float:
    """Share of a node's text that sits inside links.

    A navigation rail is nearly all links; prose is nearly none. This is the
    single most useful signal for telling one from the other.
    """
    total = len(node.text(separator=" ", strip=True))
    if not total:
        return 1.0
    linked = sum(len(a.text(separator=" ", strip=True)) for a in node.css("a"))
    return min(1.0, linked / total)
