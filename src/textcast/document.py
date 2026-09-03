"""The document model.

One idea carries the whole app: the *block* is the unit. A paragraph, a quote,
a list item, a footnote and a generated summary are each one block with a
stable id.
Reading, listening, highlighting, seeking and search all read the same blocks,
so they cannot drift apart.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class BlockKind(StrEnum):
    HEADING = "heading"
    PARA = "para"
    QUOTE = "quote"
    LIST_ITEM = "list_item"
    FOOTNOTE = "footnote"
    SUMMARY = "summary"
    #: A picture, whether it is a photograph, a chart drawn as one, or the
    #: still a charting service publishes of a chart it would rather draw
    #: live. There is no kind for a live one: see `ingest/visuals.py`.
    FIGURE = "figure"
    TABLE = "table"


#: Blocks you look at rather than listen to. They are ordinary blocks — one
#: row, one id, one place in the read-along — so the audio can stop on one and
#: the reader can show the thing itself at exactly the point it is cited.
VISUAL_KINDS = {BlockKind.FIGURE, BlockKind.TABLE}

#: Kinds the player can hide and the synthesiser can skip, per user setting.
OPTIONAL_KINDS = {BlockKind.FOOTNOTE, BlockKind.SUMMARY} | VISUAL_KINDS


@dataclass
class Block:
    kind: BlockKind
    text: str
    #: Footnote number this block carries, when the source cited one inline.
    footnote_ref: str | None = None
    #: What a visual block *shows*, which text cannot carry: the picture's
    #: address, the table's cells, the frame's link. Empty for every other
    #: kind. `text` stays the caption, so search, editing and the spoken cue
    #: all keep working without knowing this field exists.
    media: dict | None = None
    section_idx: int = 0
    idx: int = 0

    @property
    def id(self) -> str:
        return f"b{self.section_idx}-{self.idx}"

    def spoken(
        self,
        quote_markers: bool = True,
        g2p: str = "misaki",
        phonemes: bool = True,
    ) -> str:
        """The text handed to the TTS engine, which is not what is shown.

        Two differences from the displayed text. A block quote reads badly
        without a cue, so we speak one unless the engine is voicing quotes in a
        second voice. And everything runs through the normaliser, so "$72mm"
        is read as money rather than spelled out.

        ``g2p`` and ``phonemes`` describe the engine that will read this, and
        only a phoneme rule is sensitive to them. The result is *not* cached
        anywhere: the block cache is keyed on the engine as well as the text,
        so two engines never share a render.
        """
        from .normalize import normalize

        spoken = normalize(self.text, g2p=g2p, phonemes=phonemes)
        if self.kind is BlockKind.QUOTE and quote_markers:
            return f"Start quote. {spoken} End quote."
        return spoken


@dataclass
class Section:
    title: str
    blocks: list[Block] = field(default_factory=list)
    idx: int = 0


@dataclass
class Article:
    title: str
    sections: list[Section] = field(default_factory=list)
    subtitle: str = ""
    author: str = ""
    source: str = ""
    url: str = ""
    lang: str = "en"
    published_at: str | None = None
    #: Set for newsletter issues so the library can group them into a series.
    series: str | None = None
    #: Which ingest adapter produced this, for debugging a bad parse.
    adapter: str = ""

    def renumber(self) -> Article:
        """Assign section and block indices, making every block id stable."""
        for s_idx, section in enumerate(self.sections):
            section.idx = s_idx
            for b_idx, block in enumerate(section.blocks):
                block.section_idx = s_idx
                block.idx = b_idx
        return self

    def blocks(self, include: set[BlockKind] | None = None):
        for section in self.sections:
            for block in section.blocks:
                if include is None or block.kind in include:
                    yield section, block

    @property
    def word_count(self) -> int:
        return sum(len(b.text.split()) for _s, b in self.blocks())

    @property
    def fingerprint(self) -> str:
        """Stable hash of the content, used to spot a re-ingest of the same issue."""
        h = hashlib.sha256()
        h.update(self.title.encode())
        for _s, block in self.blocks():
            h.update(block.text.encode())
        return h.hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Article:
        sections = [
            Section(
                title=s.get("title", ""),
                idx=s.get("idx", i),
                blocks=[
                    Block(
                        kind=BlockKind(b.get("kind", "para")),
                        text=b["text"],
                        footnote_ref=b.get("footnote_ref"),
                        media=b.get("media"),
                        section_idx=b.get("section_idx", i),
                        idx=b.get("idx", j),
                    )
                    for j, b in enumerate(s.get("blocks", []))
                ],
            )
            for i, s in enumerate(data.get("sections", []))
        ]
        known = {f for f in cls.__dataclass_fields__ if f != "sections"}
        return cls(sections=sections, **{k: v for k, v in data.items() if k in known}).renumber()


def to_markdown(article: Article) -> str:
    """The article as text, for reading outside the app.

    The displayed text, not the spoken form: ``Block.spoken()`` is derived at
    build time and belongs to the engine, not to a reader. A summary is a
    block like any other, so it comes out wherever it sits.
    """
    head = [f"# {article.title}"]
    if article.subtitle:
        head.append(f"*{article.subtitle}*")
    byline = " · ".join(x for x in (article.author, article.source, article.published_at) if x)
    if byline:
        head.append(byline)
    if article.url:
        head.append(f"<{article.url}>")

    body = []
    for section in article.sections:
        if section.title:
            body.append(f"## {section.title}")
        for block in section.blocks:
            if block.kind is BlockKind.HEADING:
                body.append(f"### {block.text}")
            elif block.kind is BlockKind.QUOTE:
                body.append("\n".join(f"> {line}" for line in block.text.splitlines()))
            elif block.kind is BlockKind.LIST_ITEM:
                body.append(f"- {block.text}")
            elif block.kind is BlockKind.SUMMARY:
                body.append(f"**Summary.** {block.text}")
            elif block.kind is BlockKind.FOOTNOTE:
                mark = f"[{block.footnote_ref}] " if block.footnote_ref else ""
                body.append(f"{mark}{block.text}")
            elif block.kind in VISUAL_KINDS:
                body.append(_visual_markdown(block))
            else:
                body.append(block.text)

    return "\n\n".join([*head, *body]) + "\n"


def _visual_markdown(block: Block) -> str:
    """A visual as Markdown: the thing itself where it can be written down.

    An image becomes an image, a table becomes a table, and a live chart
    becomes a link — there is nothing else to write for a frame. The caption
    is the block's text either way, so an export never loses what it said.
    """
    media = block.media or {}
    if block.kind is BlockKind.TABLE:
        rows = media.get("rows") or []
        if not rows:
            return block.text
        head, *rest = rows
        width = max(len(r) for r in rows)
        def line(cells: list[str]) -> str:
            padded = list(cells) + [""] * (width - len(cells))
            return "| " + " | ".join(c.replace("|", "\\|") for c in padded) + " |"
        table = [line(head), "| " + " | ".join(["---"] * width) + " |", *(line(r) for r in rest)]
        return f"**{block.text}**\n\n" + "\n".join(table) if block.text else "\n".join(table)
    src = media.get("src") or ""
    return f"![{block.text}]({src})" if src else block.text


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, limit: int = 60) -> str:
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "untitled"
