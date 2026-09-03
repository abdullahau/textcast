"""Bloomberg, and Money Stuff in particular.

Ported from ``html-to-text.ipynb``. Bloomberg ships hashed CSS module class
names — ``HedAndDek_dek-abc123`` — so every selector here matches on a prefix.
"""

from __future__ import annotations

import re

from ..document import Article, BlockKind
from .base import blocks_from_dom, finish, inline_footnotes, text_of
from .dom import Tree, attr, clean, drop, first_text, root, select, select_one
from .visuals import VisualRules

DEK = '[class*="HedAndDek_dek"]'
FOOTNOTE_LIST = 'ol[class*="Footnotes_base"]'
FOOTNOTE_LINK = 'a[data-component="footnote-link"]'

#: Money Stuff ends with a links roundup and a subscription pitch.
STOP_AT = "if you'd like to get money stuff in handy email form"

#: `figure` used to head this list, which cost every Money Stuff chart and
#: every article image. What it was standing in for is named below instead.
NOISE = [
    "aside", "nav", "script", "style", "noscript",
    '[data-component="in-this-article"]', '[class*="InThisArticle"]',
    '[class*="Recirc"]', '[class*="Paywall"]', '[class*="Newsletter"]',
]

#: Bloomberg wraps an article image in `ArticleImage` and a chart in Toaster,
#: which it serves in a frame. It also prints the writer's headshot twice, in
#: `BylineAuthorBio` and `AuthorBio`, and both are pictures of a person rather
#: than of anything the article is about.
VISUALS = VisualRules(
    keep=('[data-component="article-image"]', '[class*="ArticleImage"]', '[class*="Toaster"]'),
    drop=(
        '[class*="AuthorBio"] img', '[class*="BylineAuthorBio"] img',
        '[class*="Ad_"]', '[class*="Outbrain"]', '[data-component="ad"]',
    ),
)


#: Bloomberg names the writer three times over in the head, and again in the
#: byline. The meta tags are stable; the byline class carries a build hash, so
#: it is matched on a prefix and kept as the fallback.
AUTHOR_META = [
    'meta[name="parsely-author"]',
    'meta[name="sailthru.author"]',
    'meta[name="author"]',
    'meta[property="article:author"]',
]
AUTHOR_BYLINE = [
    '[class*="BylineAuthorBio_author"]',
    '[class*="bylineAuthor"] a',
    '[class*="Byline"] a',
    '[rel="author"]',
]


def _author(tree: Tree) -> str:
    """Who wrote it. Every Money Stuff issue says Matt Levine right here."""
    for selector in AUTHOR_META:
        content = attr(select_one(tree, selector), "content").strip()
        if content:
            return content
    return first_text(tree, AUTHOR_BYLINE)


class BloombergAdapter:
    name = "bloomberg"

    def matches(self, url: str, tree: Tree) -> bool:
        if "bloomberg.com" in url:
            return True
        return select_one(tree, DEK) is not None or select_one(tree, FOOTNOTE_LIST) is not None

    def parse(self, tree: Tree, url: str = "") -> Article:
        title = text_of(select_one(tree, "h1")) or "Untitled"
        subtitle = text_of(select_one(tree, DEK))
        author = _author(tree)

        # Decide this before dropping noise: the subscribe link that names the
        # series sits in a node the noise selectors remove.
        series = newsletter_series(tree)

        footnotes = self._collect_footnotes(tree)
        inline_footnotes(tree, footnotes, FOOTNOTE_LINK)

        container = select_one(tree, "main") or root(tree)
        drop(container, NOISE)
        drop(container, [FOOTNOTE_LIST])

        sections = blocks_from_dom(
            container, heading_tags=("h2", "h3"), stop_at=STOP_AT, visuals=VISUALS, base_url=url
        )

        # The dek is repeated as a paragraph in the body; drop that copy.
        if subtitle:
            for section in sections:
                section.blocks = [
                    b for b in section.blocks if not (b.kind is BlockKind.PARA and b.text == subtitle)
                ]

        return finish(
            Article(
                title=title,
                subtitle=subtitle,
                author=author,
                sections=sections,
                source="Bloomberg",
                url=url,
                series=series,
            )
        )

    def _collect_footnotes(self, tree: Tree) -> dict[str, str]:
        out: dict[str, str] = {}
        ol = select_one(tree, FOOTNOTE_LIST)
        if ol is None:
            return out
        for i, li in enumerate(select(ol, "li"), start=1):
            for a in select(li, "a"):
                if "view in article" in a.text(strip=True).lower():
                    a.decompose()
            body = clean(li.text(separator=" ", strip=True))
            if body:
                out[str(i)] = body
        return out


#: The newsletter slug as the page's own analytics payload records it.
_PILLAR = re.compile(r'"(?:editorialPillar|newsletterSlug)"\s*:\s*"([a-z0-9-]+)"')
_SIGNUP = re.compile(r"/join/[^/]+/([a-z0-9-]+?)-?signup")


def newsletter_series(tree: Tree) -> str | None:
    """Name the Bloomberg newsletter an issue belongs to, if it is one.

    The canonical URL cannot answer this: it is dated and slugged from the
    headline, with no mention of the newsletter. Three signals do, in order of
    how directly they name the series. Saved pages carry different subsets of
    them, so all three are worth trying.
    """
    if "/newsletters/" not in attr(select_one(tree, 'link[rel="canonical"]'), "href").lower():
        return None

    # 1. A link to the newsletter's own subscribe page.
    for link in select(tree, 'a[href*="/account/newsletters/"]'):
        slug = attr(link, "href").split("/account/newsletters/", 1)[1].split("?")[0].strip("/")
        if slug:
            return _title(slug)

    # 2. The analytics payload, which records the slug verbatim.
    for script in select(tree, "script"):
        match = _PILLAR.search(script.text() or "")
        if match:
            return _title(match.group(1))

    # 3. A sign-up link on the mail domain.
    for link in select(tree, 'a[href*="/join/"]'):
        match = _SIGNUP.search(attr(link, "href"))
        if match:
            return _title(match.group(1))

    return None


def _title(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()
