"""The document model.

One idea carries the whole app: the *block* is the unit. A paragraph, a quote,
a list item, a footnote and a summary are each one block with a stable id.
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


#: Kinds the player can hide and the synthesiser can skip, per user setting.
OPTIONAL_KINDS = {BlockKind.FOOTNOTE, BlockKind.SUMMARY}


@dataclass
class Block:
    kind: BlockKind
    text: str
    #: Footnote number this block carries, when the source cited one inline.
    footnote_ref: str | None = None
    section_idx: int = 0
    idx: int = 0

    @property
    def id(self) -> str:
        return f"b{self.section_idx}-{self.idx}"

    def spoken(self, quote_markers: bool = True) -> str:
        """The text handed to the TTS engine, which is not what is shown.

        Two differences from the displayed text. A block quote reads badly
        without a cue, so we speak one unless the engine is voicing quotes in a
        second voice. And everything runs through the normaliser, so "$72mm"
        is read as money rather than spelled out.
        """
        from .normalize import normalize

        spoken = normalize(self.text)
        if self.kind is BlockKind.QUOTE and quote_markers:
            return f"Start quote. {spoken} End quote."
        return spoken


@dataclass
class Section:
    title: str
    blocks: list[Block] = field(default_factory=list)
    summary: str | None = None
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
                summary=s.get("summary"),
                idx=s.get("idx", i),
                blocks=[
                    Block(
                        kind=BlockKind(b.get("kind", "para")),
                        text=b["text"],
                        footnote_ref=b.get("footnote_ref"),
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


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, limit: int = 60) -> str:
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "untitled"
