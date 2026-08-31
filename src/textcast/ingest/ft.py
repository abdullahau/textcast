"""Financial Times."""

from __future__ import annotations

from ..document import Article
from .base import blocks_from_dom, finish, text_of
from .dom import Tree, drop, first_text, root, select_one

NOISE = [
    "figure", "aside", "nav", "script", "style", "noscript",
    ".o-ads", ".o-teaser", '[class*="onward"]', '[class*="follow"]',
    '[class*="share"]', '[class*="Share"]', '[class*="related"]',
    '[data-trackable="teaser"]',
]


class FTAdapter:
    name = "ft"

    def matches(self, url: str, tree: Tree) -> bool:
        if "ft.com" in url:
            return True
        return select_one(tree, ".o-topper__standfirst, .article__content-body") is not None

    def parse(self, tree: Tree, url: str = "") -> Article:
        title = text_of(select_one(tree, "h1")) or "Untitled"
        subtitle = first_text(tree, [".o-topper__standfirst", '[class*="standfirst"]'])

        container = (
            select_one(tree, ".article__content-body")
            or select_one(tree, "article")
            or select_one(tree, "main")
            or root(tree)
        )
        drop(container, NOISE)

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                sections=blocks_from_dom(container),
                source="Financial Times",
                url=url,
                author=first_text(tree, [".n-content-tag--author", '[class*="byline"]']),
            )
        )
