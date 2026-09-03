"""Substack, on the web and in the post.

A Substack post is often half pictures — a chart, a screenshot of a filing, a
table the writer built by hand — so it is the publication that most needs
visuals to survive the walk. It is also the one that dresses the most
furniture as a picture: a subscribe widget, a share row, a recommendation
card and a paywall all carry artwork.

It sits before the newsletter adapter in the registry, because a Substack
issue arriving by email matches both and this one knows more. Both shapes
parse here: the web page keeps its prose in `.available-content`, the email
in a `.body.markup` inside a layout table.
"""

from __future__ import annotations

from ..document import Article
from .base import blocks_from_dom, finish, text_of
from .dom import Tree, drop, first_text, meta, root, select_one
from .visuals import VisualRules

#: Where the prose is, most specific first.
CONTAINERS = (
    ".available-content",
    ".body.markup",
    "div.post .body",
    "article.post",
    "div.post",
    ".email-body",
)

NOISE = [
    "script", "style", "noscript",
    ".subscription-widget-wrap", ".subscription-widget", ".subscribe-widget",
    ".button-wrapper", ".digest-post-embed", ".embedded-publication",
    ".post-ufi", ".email-ufi", ".ufi", ".share-dialog",
    ".paywall", ".paywall-jump", ".comments-page", ".post-footer",
    ".footer", ".recommendations", ".publication-footer",
    '[class*="poll-embed"]', '[class*="subscribeWidget"]',
]

#: Substack draws a picture inside `.captioned-image-container`, which is the
#: only wrapper here that is *not* furniture — everything else with artwork on
#: it is a widget. So the keep list is short and the drop list is long.
VISUALS = VisualRules(
    keep=(".captioned-image-container", ".image-link", "figure"),
    drop=(
        ".subscription-widget img", ".button-wrapper img", ".digest-post-embed img",
        ".embedded-publication img", ".recommendations img",
        'img[src*="substackcdn.com/image/fetch/w_36"]',   # an author's face
        'img[class*="avatar"]', ".pencraft img",
    ),
)


def _author(tree: Tree) -> str:
    """Substack names the writer in a meta tag and again beside the title."""
    return meta(tree, name="author", property="article:author") or first_text(
        tree, [".post-header .pencraft a", '[class*="byline"] a', 'a[href*="/@"]']
    )


class SubstackAdapter:
    name = "substack"

    def matches(self, url: str, tree: Tree) -> bool:
        if "substack.com" in url:
            return True
        if select_one(tree, ".available-content, .post .body.markup") is not None:
            return True
        # A saved page and an email both serve every picture from the CDN,
        # which is the one mark that survives whatever wrapper Substack ships
        # next. A *link* to substack.com is not a mark: half the newsletters
        # in the library carry one.
        return (
            select_one(tree, 'img[src*="substackcdn.com"]') is not None
            or select_one(tree, 'link[rel="canonical"][href*="substack.com"]') is not None
        )

    def parse(self, tree: Tree, url: str = "") -> Article:
        title = (
            text_of(select_one(tree, "h1.post-title"))
            or meta(tree, property="og:title")
            or text_of(select_one(tree, "h1"))
            or "Untitled"
        )
        subtitle = text_of(select_one(tree, "h3.subtitle")) or meta(tree, name="description")
        author = _author(tree)
        series = meta(tree, property="og:site_name") or ""

        container = next(
            (n for selector in CONTAINERS if (n := select_one(tree, selector)) is not None),
            root(tree),
        )
        drop(container, NOISE)

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                author=author,
                sections=blocks_from_dom(
                    container, heading_tags=("h2", "h3", "h4"), visuals=VISUALS, base_url=url
                ),
                source=series or "Substack",
                url=url,
                series=series or None,
                published_at=meta(tree, property="article:published_time") or None,
            )
        )
