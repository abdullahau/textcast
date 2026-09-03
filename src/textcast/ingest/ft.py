"""Financial Times."""

from __future__ import annotations

from ..document import Article
from .base import blocks_from_dom, finish, text_of
from .dom import Tree, drop, first_text, root, select_one
from .visuals import VisualRules

#: `figure` used to be here, which is how the Alphaville lead image and every
#: FT chart drawn as one were lost. The furniture it was standing in for is
#: named directly now: a teaser card, the onward rail, an advert slot.
NOISE = [
    "aside", "nav", "script", "style", "noscript",
    ".o-ads", ".o-teaser", '[class*="onward"]', '[class*="follow"]',
    '[class*="share"]', '[class*="Share"]', '[class*="related"]',
    '[data-trackable="teaser"]',
]

#: The FT draws a data table with Origami's `o-table`, prints its title in a
#: single filled header cell rather than in the `<caption>` beside it, and
#: publishes its own charts from `ig.ft.com` in a frame. The lead image sits
#: in `n-content-image`, which the shared figure selectors already reach.
VISUALS = VisualRules(
    keep=('[class*="n-content-image"]', ".o-table", '[class*="o-table"]'),
    drop=(".o-teaser__image", '[data-trackable="teaser"] img', ".n-content-related-box"),
)


def _author(tree: Tree) -> str:
    """The byline block also carries the date and a print link; take the name."""
    name = first_text(tree, ['[class*="byline"] a', ".n-content-tag--author"])
    return name.split(" Published ")[0].strip()


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
                sections=blocks_from_dom(container, visuals=VISUALS, base_url=url),
                source="Financial Times",
                url=url,
                author=_author(tree),
            )
        )
