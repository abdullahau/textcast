"""The Economist.

Two things the catch-all got wrong. The dek is an `<h2>` directly after the
`<h1>`, so the density walk read it as the first section's heading rather than
as the article's standfirst. And every leader ends with the paper's own
recirculation — "Explore more", "From the September 5th 2026 edition", "More
from Leaders" — which arrives as headings with other articles' headlines under
them, and read as four extra sections of one line each.
"""

from __future__ import annotations

from ..document import Article
from .base import blocks_from_dom, finish, text_of
from .dom import Tree, drop, meta, root, select_one
from .visuals import VisualRules

#: Everything from here down is other articles. The walk stops rather than
#: pruning afterwards, because what follows is a run of real headings with
#: real text under them — there is nothing about them that looks like junk
#: except where they sit.
STOP_AT = "explore more"

NOISE = [
    "script", "style", "noscript", "nav", "aside", "footer", "header",
    '[class*="newsletter"]', '[class*="Newsletter"]',
    '[class*="subscri"]', '[class*="Subscri"]',
    '[class*="teaser"]', '[class*="Teaser"]', '[class*="promo"]',
    '[data-test-id*="recirc"]', '[data-test-id*="Recirc"]',
]

#: The paper illustrates almost every leader, and the picture is the one thing
#: on the page that is not either prose or a link to another article.
VISUALS = VisualRules(drop=('[class*="teaser"] img', '[class*="promo"] img'))


class EconomistAdapter:
    name = "economist"

    def matches(self, url: str, tree: Tree) -> bool:
        if "economist.com" in url:
            return True
        return meta(tree, property="og:site_name") == "The Economist"

    def parse(self, tree: Tree, url: str = "") -> Article:
        title = text_of(select_one(tree, "h1")) or meta(tree, property="og:title") or "Untitled"
        subtitle = meta(tree, property="og:description")

        container = select_one(tree, "article") or select_one(tree, "main") or root(tree)
        drop(container, NOISE)

        # The paper sets small capitals with `<small>`, and it does it *inside*
        # words: `Chat<small>GPT’</small>s`, `<small>AI</small>`, and a drop
        # cap as `<span data-caps="initial">N</span><small>VIDIA was</small>`.
        # Text extraction joins elements with a space, so those came out as
        # "Chat GPT’ s" and "N VIDIA" — and were read aloud that way. Unwrap
        # them and the letters rejoin the word they belong to.
        container.unwrap_tags(["small"])
        for node in container.css("span[data-caps]"):
            node.unwrap()
        # Unwrapping leaves two text nodes side by side, and the separator goes
        # between text *nodes*, not between elements — so "N" and "VIDIA" were
        # still two words until they were merged into one node.
        container.merge_text_nodes()

        # The dek is a heading in the markup and a standfirst on the page. Left
        # in, it becomes the first section's title and the article's own title
        # is never used for one.
        if subtitle:
            for node in container.css("h2, h3"):
                if text_of(node) == subtitle:
                    node.decompose()
                    break

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                sections=blocks_from_dom(
                    container,
                    heading_tags=("h2", "h3"),
                    stop_at=STOP_AT,
                    visuals=VISUALS,
                    base_url=url,
                ),
                source="The Economist",
                url=url,
                author=meta(tree, name="author", property="article:author"),
                published_at=meta(tree, property="article:published_time") or None,
            )
        )
