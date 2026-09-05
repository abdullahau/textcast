"""Pictures, tables and live charts, turned into blocks.

An audio reader still has to answer "look at this". A Money Stuff issue quotes
a chart, an Alphaville post builds its argument on a ready reckoner, and a
Substack post is often half pictures. Read aloud with the visuals dropped,
those articles say "as you can see" about nothing.

So a visual is an ordinary block. It has an id, a place in the read-along and
a row in `block` like every paragraph, and `block.media` carries the one thing
text cannot: the picture's address, the table's cells, the frame's link. The
player can then stop on it and the reader shows the thing itself, at the point
the prose cites it.

Every publication needs the same three answers — what is a visual, what is
furniture dressed as one, and where is its caption — so they are asked once
here. A publication supplies only what is peculiar to it, as a `VisualRules`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urljoin

from ..document import Block, BlockKind
from .dom import Node, ancestor_tags, attr, clean, text_of

#: Containers that hold a picture. A bare `img` is last on purpose: matched
#: first it would take the picture out of its own figure and lose the caption.
FIGURE_SELECTORS: tuple[str, ...] = (
    "figure",
    '[data-component="article-image"]',
    '[data-component="chart"]',
    '[class*="captioned-image"]',
    '[class*="ArticleImage"]',
    '[class*="ArticleChart"]',
    '[class*="n-content-image"]',
    '[class*="content-graphic"]',
    "img",
)

TABLE_SELECTORS: tuple[str, ...] = ("table",)

EMBED_SELECTORS: tuple[str, ...] = ("iframe",)

CAPTION_SELECTORS: tuple[str, ...] = (
    "figcaption",
    "caption",
    '[class*="caption"]',
    '[class*="Caption"]',
    '[class*="credit"]',
)

#: A live chart in a frame, and where to get the same chart as a picture.
#:
#: Keeping the frame was a mistake. It is a third party the reader never
#: agreed to, it needs the provider's own script to draw anything — which the
#: sandbox refuses, so the two Flourish charts in an FT piece rendered as an
#: empty box behind a button — and an article held offline has no chart at
#: all. A picture has none of those problems, and every provider worth keeping
#: publishes one.
#:
#: An allowlist of providers whose still image has actually been fetched and
#: looked at. Flourish's is the full chart at 1020px, title, legend, axis and
#: source: readable, not a thumbnail. A frame from anywhere else is dropped,
#: because a figure pointing at a still that turns out to 404 is worse than no
#: figure at all.
CHART_STILLS: tuple[tuple[str, str], ...] = (
    (
        r"(?:flo\.uri\.sh|public\.flourish\.studio)/visualisation/(\d+)",
        "https://public.flourish.studio/visualisation/{0}/thumbnail",
    ),
)

#: An address that is a beacon, an advert or a consent frame, never a graphic.
JUNK_SRC = re.compile(
    r"px\.gif|/px\.|spoor-api|/pixel|/beacon|/track|doubleclick|googletagmanager"
    r"|google-analytics|scorecardresearch|/ads?/|adservice|/avatar|/logo"
    r"|_tcfapi|gpp|consent|/blank\.|1x1\.",
    re.I,
)

#: A class or id that marks site furniture drawn as a picture: a teaser card,
#: a promo, an author's headshot, a subscribe widget's artwork.
JUNK_CLASS = re.compile(
    r"teaser|promo|advert|sponsor|avatar|headshot|byline|author|logo|icon"
    r"|onward|recirc|related|subscri|paywall|newsletter|share|social|nav"
    r"|footer|masthead|placeholder|thumbnail",
    re.I,
)

#: What a publication puts in a frame's `title` when it has nothing to say.
#: The FT's is "Interactive or visual content" on every graphic it ships, and
#: the still already carries the chart's real title inside the picture.
GENERIC_TITLE = re.compile(
    r"^\s*(interactive( or visual)?( content| chart| graphic)?|visual content"
    r"|chart|graphic|embedded content|iframe|untitled)\s*$",
    re.I,
)

#: A caption that is only a credit line says nothing about what is shown.
#: The word boundary belongs to the words only: `©\b` never matches, because
#: neither the symbol nor the space after it is a word character.
BARE_CREDIT = re.compile(
    r"^\s*(?:©|\(c\)|(?:photo|image|source|credit|getty|reuters|ap|bloomberg)\b)", re.I
)

#: Below this, a picture is an icon, a rule or a spacer rather than a graphic.
MIN_WIDTH = 200

#: A figure carries a caption, not an argument. Past this much text a
#: container that matched a figure selector is a layout wrapper with the
#: article inside it — `article-grid--no-full-width-graphics` matched
#: `[class*="graphic"]` once and swallowed a whole Alphaville post.
MAX_FIGURE_TEXT = 400


@dataclass(frozen=True)
class VisualRules:
    """What one publication's visuals look like.

    Every field defaults to the shared answer, so a publication that behaves
    normally declares nothing. `drop` is the escape hatch: the furniture this
    publication draws that the shared filters cannot recognise.
    """

    figures: tuple[str, ...] = FIGURE_SELECTORS
    tables: tuple[str, ...] = TABLE_SELECTORS
    embeds: tuple[str, ...] = EMBED_SELECTORS
    captions: tuple[str, ...] = CAPTION_SELECTORS
    chart_stills: tuple[tuple[str, str], ...] = CHART_STILLS
    #: Removed before anything else is looked at.
    drop: tuple[str, ...] = ()
    #: Kept even when the shared filters would call it furniture. A
    #: publication that names its charts "promo-chart" needs this.
    keep: tuple[str, ...] = ()
    min_width: int = MIN_WIDTH
    #: Off for a publication whose pictures are all decoration.
    enabled: bool = True

    def with_extra(self, *, drop: tuple[str, ...] = (), keep: tuple[str, ...] = ()) -> VisualRules:
        return replace(self, drop=self.drop + drop, keep=self.keep + keep)

    @property
    def selector(self) -> str:
        """One CSS selector for everything a walk should stop at.

        Joined rather than searched for separately so lexbor returns them in
        document order alongside the paragraphs — a chart cited in the middle
        of an argument belongs in the middle of the argument.
        """
        return ", ".join(self.figures + self.tables + self.embeds)


#: The rules a publication gets when it asks for nothing in particular.
DEFAULT_RULES = VisualRules()

#: Rules that keep nothing, for a walk that must stay text-only.
NO_VISUALS = VisualRules(enabled=False)


# --------------------------------------------------------------------------
# Recognising one


def _classes(node: Node) -> str:
    return f"{attr(node, 'class')} {attr(node, 'id')} {attr(node, 'data-component')}"


def _int(value: str) -> int:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else 0


def _hidden(node: Node) -> bool:
    style = attr(node, "style").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return node.attributes.get("hidden") is not None or attr(node, "aria-hidden") == "true"


def _marked_keep(node: Node, rules: VisualRules, stop: Node | None) -> bool:
    """True when this node, or something it sits in, is on the keep list."""
    return any(_ancestor_matches(node, selector, stop) for selector in rules.keep)


def drop_furniture(container: Node, rules: VisualRules) -> None:
    """Remove the furniture, but never a picture the keep list claims.

    `keep` is asked per node, late, and only reaches six ancestors. `drop` was
    a plain `dom.drop` over the whole container: CSS, no depth limit, and it
    decomposes the node — so by the time `keep` was consulted the picture was
    already off the tree and could not be rescued. Drop silently won, which is
    backwards. Every adapter writes a short keep list and a long drop one
    precisely because keep is meant to be the authority.

    Substack is what showed it. `.pencraft img` was written for an author's
    face and a button glyph; Substack now wraps the whole post body in a
    `pencraft` layout div, ten levels above every picture in the article, so
    that one selector took all of them and every post came out as prose.
    """
    for selector in rules.drop:
        try:
            found = container.css(selector)
        except Exception:
            continue
        for node in found:
            if _marked_keep(node, rules, container):
                continue
            node.decompose()


def _matches_self(node: Node, selector: str) -> bool:
    """Does this node itself match, ignoring its descendants?

    Neither `css_matches` nor `any_css_matches` answers it: both are true when
    a *descendant* matches, so every wrapper on the page "matched" `.o-table`
    and rescued the promo banners the junk filter had just refused. `css`
    searches the node and its subtree in document order, so a node that
    matches is its own first hit.
    """
    try:
        return node.css_first(selector) == node
    except Exception:
        return False


def _ancestor_matches(node: Node, selector: str, stop: Node | None) -> bool:
    current: Node | None = node
    depth = 0
    while current is not None and current != stop and depth < 6:
        if _matches_self(current, selector):
            return True
        current = current.parent
        depth += 1
    return False


def _furniture(node: Node, stop: Node | None) -> bool:
    """True when the node, or a container it sits in, is site furniture.

    Walks up rather than testing the node alone: a teaser's picture carries no
    class of its own, and the card around it carries all of them.
    """
    current: Node | None = node
    depth = 0
    while current is not None and current != stop and depth < 6:
        if current.tag in ("aside", "nav", "footer", "header"):
            return True
        if JUNK_CLASS.search(_classes(current)):
            return True
        if _hidden(current):
            return True
        current = current.parent
        depth += 1
    return False


# --------------------------------------------------------------------------
# Pictures


_SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src")


#: Candidates are comma-separated, and a candidate's URL may itself hold
#: commas — Substack serves every picture through Cloudinary, whose path is
#: `w_1456,c_limit,f_webp`. A plain `split(",")` cut those in three. The comma
#: that separates two candidates is the one with whitespace after it; a comma
#: inside a URL never has any.
_CANDIDATES = re.compile(r",\s+")


def _largest(srcset: str) -> str:
    """The widest candidate in a srcset.

    Worth the parse twice over. It is the best copy on a live page, and on a
    page saved to disk it is the only *absolute* address: the browser rewrites
    `src` to point at the `_files` directory beside the HTML.
    """
    best, best_width = "", -1
    for candidate in _CANDIDATES.split(srcset):
        parts = candidate.split()
        if not parts:
            continue
        width = _int(parts[1]) if len(parts) > 1 else 0
        if width >= best_width:
            best, best_width = parts[0], width
    return best


def _image_src(img: Node, base: str) -> str:
    src = _largest(attr(img, "srcset") or attr(img, "data-srcset"))
    if not src:
        # Substack, and anyone else using `<picture>`, puts every candidate on
        # a `<source>` and leaves the `<img>` with a bare fallback.
        parent = img.parent
        if parent is not None and parent.tag == "picture":
            for source in parent.css("source"):
                src = _largest(attr(source, "srcset"))
                if src:
                    break
    if not src:
        for name in _SRC_ATTRS:
            value = attr(img, name).strip()
            if value and not value.startswith("data:"):
                src = value
                break
    if not src or src.startswith("data:"):
        return ""
    src = urljoin(base, src) if base else src
    # The reader puts this in an `src` attribute. A page it parsed is not a
    # page it trusts, so only the two schemes that fetch a picture get there.
    if ":" in src.split("/")[0] and not src.lower().startswith(("http://", "https://")):
        return ""
    return src


def _caption_match(node: Node, rules: VisualRules, *, bare: bool) -> Node | None:
    """The first `rules.captions` match that is a genuine descendant of ``node``.

    ``found != node`` (not ``is``) because lexbor hands out a fresh wrapper
    per lookup, so a container whose own class matches a caption selector
    (e.g. `class="captioned-image-wrapper"`, which also matches
    `[class*="caption"]`) would otherwise self-match and be read as its own
    caption. ``bare`` picks which of `_caption` and `_credit` is asking: a
    bare credit ("© Reuters") is not a caption and vice versa.
    """
    for selector in rules.captions:
        try:
            found = node.css_first(selector)
        except Exception:
            continue
        if found is None or found == node:
            continue
        text = text_of(found)
        if text and bool(BARE_CREDIT.match(text)) == bare:
            return found
    return None


def _caption(node: Node, rules: VisualRules) -> str:
    """The words a publication prints under the picture.

    A bare credit — "© Reuters" — is dropped: it names who took it, not what
    it shows, and read aloud it is noise where a caption would have been
    information.
    """
    found = _caption_match(node, rules, bare=False)
    return text_of(found) if found is not None else ""


def _figure_block(node: Node, rules: VisualRules, base: str) -> Block | None:
    img = node if node.tag == "img" else node.css_first("img")
    if img is None:
        return None
    if _int(attr(img, "width")) == 1 or _int(attr(img, "height")) == 1:
        return None
    src = _image_src(img, base)
    if not src or JUNK_SRC.search(src):
        return None
    width = _int(attr(img, "width"))
    if width and width < rules.min_width:
        return None

    alt = clean(attr(img, "alt"))
    caption = _caption(node, rules) or alt
    media = {"src": src, "alt": alt}
    # The declared size, so the reader can reserve the right box before the
    # bytes arrive. Only the ratio survives — the stored copy is whichever
    # candidate the srcset offered — and the ratio is what stops the page
    # jumping when the picture lands.
    height = _int(attr(img, "height"))
    if width and height:
        media["w"], media["h"] = width, height
    if caption:
        media["caption"] = caption
    if node.tag != "img":
        credit = _credit(node, rules)
        if credit:
            media["credit"] = credit
    return Block(kind=BlockKind.FIGURE, text=_label("Figure", caption), media=media)


def _credit(node: Node, rules: VisualRules) -> str:
    found = _caption_match(node, rules, bare=True)
    return text_of(found) if found is not None else ""


def _label(word: str, caption: str) -> str:
    """The block's text: the cue a listener hears, and what search indexes.

    Not what the page prints. A reader can see it is a table; only a listener
    needs telling. The page shows `media["caption"]`, which is the caption
    alone and is absent when the publication printed none.

    Always non-empty, because a block with no text is a block search cannot
    find, the editor cannot show and the synthesiser has nothing to say for.
    """
    caption = caption.strip()
    if not caption:
        return f"{word}."
    if caption.lower().startswith(word.lower()):
        return caption
    return f"{word}: {caption}"


# --------------------------------------------------------------------------
# Tables


#: A cell may claim any span it likes; the FT's table footer claims 1000.
#: Past this the number is a way of saying "the whole row", not a width.
MAX_COLSPAN = 12


def _cells(row: Node) -> list[str]:
    out: list[str] = []
    for cell in row.css("th, td"):
        text = text_of(cell)
        span = min(MAX_COLSPAN, max(1, _int(attr(cell, "colspan")) or 1))
        out.extend([text] * span)
    return out


def _body_rows(node: Node) -> list[list[str]]:
    """Every row but the footer's, which is a credit rather than data."""
    rows = []
    for tr in node.css("tr"):
        if ancestor_tags(tr, {"tfoot"}, stop=node):
            continue
        cells = _cells(tr)
        if any(cells):
            rows.append(cells)
    return rows


def _table_block(node: Node, rules: VisualRules) -> Block | None:
    """A table as its cells, plus a caption taken from wherever one is.

    A layout table is not a table: newsletters are built out of them, and one
    with a single cell or no header is furniture. Two columns and two rows is
    the smallest thing worth showing.
    """
    rows = _body_rows(node)
    if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
        return None
    if any(inner != node for inner in node.css("table")):
        return None  # a table wrapping another is layout, not data

    caption = text_of(node.css_first("caption"))
    header = bool(node.css_first("thead th, thead td, tr th"))

    # An FT table titles itself in its header row rather than in the empty
    # `<caption>` beside it, and leaves the rest of that row blank so the
    # title spans the width. A mostly empty header row is a title, not
    # headings. The row stays where it is: it is still what the page shows.
    in_table = False
    if not caption and header and rows[0][0]:
        filled = [cell for cell in rows[0] if cell]
        if len(filled) * 2 <= len(rows[0]):
            caption, in_table = rows[0][0], True

    foot = text_of(node.css_first("tfoot"))
    media: dict = {"rows": rows, "header": header}
    # A title lifted out of the header row is already on the page, in that
    # row. Printed again under the table it reads as a second title.
    if caption and not in_table:
        media["caption"] = caption
    if foot:
        media["foot"] = foot
    return Block(kind=BlockKind.TABLE, text=_label("Table", caption), media=media)


# --------------------------------------------------------------------------
# Live charts


def _embed_block(node: Node, rules: VisualRules, base: str) -> Block | None:
    """A charting frame, as the picture of that chart the provider publishes.

    Not a frame. See `CHART_STILLS`: the frame drew nothing, needed a third
    party, and vanished offline.
    """
    src = attr(node, "src") or attr(node, "data-src")
    if not src:
        return None
    src = urljoin(base, src) if base else src
    still = _still_of(src, rules)
    if not still:
        return None
    caption = clean(attr(node, "title")) or _caption(node.parent or node, rules)
    if GENERIC_TITLE.match(caption):
        caption = ""
    media = {"src": still, "alt": caption, "frame": src}
    if caption:
        media["caption"] = caption
    return Block(kind=BlockKind.FIGURE, text=_label("Chart", caption), media=media)


def _still_of(src: str, rules: VisualRules) -> str:
    """The same chart as a picture, or nothing if this provider has none."""
    for pattern, template in rules.chart_stills:
        match = re.search(pattern, src)
        if match:
            return template.format(*match.groups())
    return ""


def _wraps_prose(node: Node, rules: VisualRules) -> bool:
    """True when the candidate is a layout container, not a figure.

    Two signs, and either is enough: it holds a heading or a quote, which no
    caption does, or it holds more words than a caption ever has. A table is
    exempt — its text is its data.

    The caption's own words are not counted towards that limit. A data-heavy
    Substack post can caption a chart with a full explanatory paragraph —
    "hook-and-squeeze"'s second image ran to 407 characters on its own — and
    counting it left a real figure indistinguishable from the wrapper this
    check exists to catch, so `visual_block` fell through to the bare `img`
    inside it, which cannot see a `figcaption` sitting beside it as its
    parent's sibling.
    """
    if node.tag == "table":
        return False
    if any(found != node for found in node.css("h1, h2, h3, blockquote")):
        return False if node.tag == "figure" else True
    text_len = len(node.text(separator=" ", strip=True) or "")
    found = _caption_match(node, rules, bare=False)
    if found is not None:
        text_len -= len(text_of(found))
    return text_len > MAX_FIGURE_TEXT


# --------------------------------------------------------------------------
# The one entry point


def visual_block(
    node: Node,
    rules: VisualRules = DEFAULT_RULES,
    *,
    base_url: str = "",
    stop: Node | None = None,
) -> Block | None:
    """Turn one node into a visual block, or say it is not one.

    Returns None for furniture, for a picture too small to be a graphic, for a
    layout table and for a frame that is not a chart. The caller emits nothing
    in that case, which is what the parsers did for every visual before this.
    """
    if not rules.enabled:
        return None
    kept = _marked_keep(node, rules, stop)
    if not kept and _furniture(node, stop):
        return None

    if _wraps_prose(node, rules):
        return None
    if node.tag == "table":
        return _table_block(node, rules)
    if node.tag == "iframe":
        return _embed_block(node, rules, base_url)
    return _figure_block(node, rules, base_url)


__all__ = [
    "CAPTION_SELECTORS",
    "CHART_STILLS",
    "DEFAULT_RULES",
    "EMBED_SELECTORS",
    "FIGURE_SELECTORS",
    "NO_VISUALS",
    "TABLE_SELECTORS",
    "VisualRules",
    "drop_furniture",
    "visual_block",
]
