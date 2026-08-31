"""Find the article body in an arbitrary page.

A compact readability: score every candidate container by how much prose it
holds, discount it by how much of that text is inside links, and take the best.
That is the part of the classic algorithm that does the real work, and it drops
``readability-lxml`` — and with it lxml — from the dependency tree.
"""

from __future__ import annotations

import re

from .dom import Node, Tree, link_density, root, select

#: Containers worth scoring. Anything else is either inline or a leaf.
CANDIDATES = "article, main, section, div, td"

#: Class and id names that give a container away, in both directions.
_GOOD = re.compile(r"article|body|content|entry|main|page|post|story|text|prose", re.I)
_BAD = re.compile(
    r"ad-|advert|banner|breadcrumb|combx|comment|community|cookie|disqus|extra|"
    r"foot|header|legend|menu|meta|modal|nav|pag(er|ination)|popup|promo|related|"
    r"remark|rss|share|shopping|sidebar|social|sponsor|subscribe|tags|tool|widget",
    re.I,
)

STRIP = [
    "script", "style", "noscript", "template", "svg", "form", "iframe",
    "nav", "aside", "footer", "header", "figure", "figcaption", "button",
    "[role=navigation]", "[role=banner]", "[role=complementary]",
    "[role=search]", "[aria-hidden=true]", "[hidden]",
]


def _name_bonus(node: Node) -> float:
    label = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
    if not label.strip():
        return 1.0
    if _BAD.search(label):
        return 0.25
    if _GOOD.search(label):
        return 1.5
    return 1.0


def score(node: Node) -> float:
    """How much this container looks like the body of an article.

    Paragraph text counts; text loose in the container does not, which is what
    separates a real article body from a wrapper full of teasers.
    """
    paragraphs = node.css("p")
    if len(paragraphs) < 2:
        return 0.0

    prose = 0
    for para in paragraphs:
        text = para.text(separator=" ", strip=True)
        if len(text) < 40:
            continue
        # Commas track sentence complexity, and so track real writing.
        prose += len(text) + text.count(",") * 12

    if prose < 250:
        return 0.0
    return prose * (1.0 - link_density(node)) * _name_bonus(node)


def article_body(tree: Tree, strip: list[str] | None = None) -> Node:
    """Return the node most likely to be the article body."""
    from .dom import drop

    drop(tree, strip if strip is not None else STRIP)
    base = root(tree)

    best: Node = base
    best_score = 0.0
    for node in select(base, CANDIDATES):
        value = score(node)
        if value > best_score:
            best, best_score = node, value

    return best if best_score > 0 else base
