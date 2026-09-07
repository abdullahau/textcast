"""Blogger, at ``*.blogspot.com`` and any custom domain the platform serves.

A permalink page carries the post inside ``.post-body.entry-content`` and
nothing else, but nothing before this adapter scoped a walk to it, so a real
page came out as the post's prose followed by "Popular Posts" and a
year-by-year "Blog Archive" as extra sections — read as though they were part
of the article, and with no way to remove them, because they are not noise a
prune list can name; they are real headings with real links under them, the
same shape as the Economist's own recirculation. And unscoped, the walk read
``<h1 class="title">`` — the *blog's* name, not the post's — as the article's
own heading.

Routing here at all needed its own fix. Blogger's caption markup is
``table.tr-caption-container``, and the newsletter adapter's own detection —
``table[class*="container"]`` — matches that on every single post, ahead of
the generic fallback in the registry. This adapter has to sit ahead of
`NewsletterAdapter` for that reason, not only to do better once it is chosen.

Two more things the shared walk cannot see on its own. A caption lives in that
same ``tr-caption-container`` table — a picture in one row, the words in the
next — which is exactly what a data table looks like to the visual walk, so it
reads two cells and drops the whole thing rather than call it a picture.
Rebuilding it as a ``<figure>``/``<figcaption>`` first hands it to the ordinary
picture path instead. And a subheading is not a heading: Blogger's own style
(and every post on this blog) marks one by bolding the whole line —
``<p><b>Scaling versus Business Building</b></p>``, sometimes a ``<div>`` or a
``<span>`` instead of a ``<p>`` — which is not in `BLOCK_SELECTOR` at all, so
the words are not merely unstructured, they are never visited and silently
gone. Promoting one to an ``<h3>`` before the walk is what keeps it.

And a paragraph is not always a ``<p>`` either. Blogger's editor writes a
plain line of prose straight into a ``<div>`` as often as into a ``<p>`` —
especially the line right after a picture, which is where "The graph below
captures the scaling choices..." sat, immediately following the
``<div class="separator">`` that held the image. `BLOCK_SELECTOR` does not
name ``div`` — most of it, on any other publication, is layout — so the
sentence was never visited either, the same silent loss as the heading. Any
``<div>`` with no block-level element inside it (no nested ``div``, ``p``,
list, table, quote or heading) is prose or nothing, never a layout wrapper on
this platform, and is promoted to a ``<p>`` for the same reason.
"""

from __future__ import annotations

import html as htmlmod
from urllib.parse import urlparse

from ..document import Article
from .base import blocks_from_dom, finish, text_of
from .dom import Node, Tree, attr, meta, parse, select_one
from .visuals import DEFAULT_RULES

#: Above this many characters a bold line is a paragraph in bold, not a title.
MAX_HEADING_LEN = 100


class BlogspotAdapter:
    name = "blogspot"

    def matches(self, url: str, tree: Tree) -> bool:
        if meta(tree, name="generator").strip().lower() == "blogger":
            return True
        return ".blogspot." in url

    def parse(self, tree: Tree, url: str = "") -> Article:
        container = (
            select_one(tree, ".post-body.entry-content")
            or select_one(tree, ".post-body")
            or select_one(tree, "body")
        )

        _convert_caption_tables(container)
        _promote_bold_headings(container)
        _promote_bare_divs_to_paragraphs(container)

        title = (
            text_of(select_one(tree, ".post-title.entry-title"))
            or meta(tree, property="og:title")
            or "Untitled"
        )
        # The blog's own name, e.g. "Musings on Markets" — never the post's
        # own description, which `og:description` carries as the blog's fixed
        # tagline and prints identically under every post.
        source = (
            text_of(select_one(tree, "h1.title"))
            or meta(tree, property="og:site_name")
            or _host(url)
        )
        author = text_of(select_one(tree, ".post-author .fn"))
        published = attr(select_one(tree, "abbr.published"), "title") or None

        return finish(
            Article(
                title=title,
                subtitle="",
                sections=blocks_from_dom(container, visuals=DEFAULT_RULES, base_url=url),
                source=source,
                url=url,
                author=author or None,
                published_at=published,
            )
        )


def _convert_caption_tables(container: Node) -> None:
    for table in container.css("table.tr-caption-container"):
        img = table.css_first("img")
        if img is None:
            continue
        caption = text_of(table.css_first("td.tr-caption"))
        cap_html = f"<figcaption>{htmlmod.escape(caption)}</figcaption>" if caption else ""
        fragment = parse(f"<div><figure>{img.html}{cap_html}</figure></div>")
        table.replace_with(fragment.css_first("figure"))


def _promote_bold_headings(container: Node) -> None:
    for node in container.css("p, div, span"):
        kids = list(node.iter(include_text=False))
        if len(kids) != 1 or kids[0].tag not in ("b", "strong"):
            continue
        text = text_of(node)
        if not text or text != text_of(kids[0]) or len(text) > MAX_HEADING_LEN:
            continue
        if text.endswith((".", "!", "?")):
            continue
        fragment = parse(f"<div><h3>{htmlmod.escape(text)}</h3></div>")
        node.replace_with(fragment.css_first("h3"))


#: A `<div>` holding one of these is a layout wrapper, not a line of prose —
#: promoting it to a `<p>` would either bury a real block inside another one
#: or hand `blocks_from_dom` a heading it would never see as one.
_BLOCK_LEVEL = "div, p, table, ol, ul, blockquote, h1, h2, h3, h4, h5, h6, figure"


def _promote_bare_divs_to_paragraphs(container: Node) -> None:
    for node in container.css("div"):
        if node == container or any(found != node for found in node.css(_BLOCK_LEVEL)):
            continue
        fragment = parse(f"<div><p>{node.inner_html}</p></div>")
        node.replace_with(fragment.css_first("p"))


def _host(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") if url else ""
