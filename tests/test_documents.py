"""Readers for text, Markdown, PDF and DOCX."""

from __future__ import annotations

import io

import pytest

from textcast.document import BlockKind
from textcast.ingest.documents import (
    UnsupportedDocument,
    _rewrap,
    article_from_file,
    article_from_text,
    looks_like_markdown,
)

MARKDOWN = """# Quarterly Note

Markets moved on **thin** volume, and the desk had [opinions](http://x.com).

> Liquidity is a coward; it flees at the first sign of trouble.

## Positioning

- Long $5bn of duration
- Short 12x EBITDA names

The trade is simple enough, though the sizing is not.
"""

PLAIN = """A note to myself

I keep meaning to write this down. The point is that the model
reads short forms badly unless you fix them first.

Second thought

It also drops the ends of long paragraphs sometimes.
"""


def kinds(article):
    return [b.kind for _s, b in article.blocks()]


def test_markdown_headings_become_sections():
    article = article_from_text(MARKDOWN, markdown=True)

    assert article.title == "Quarterly Note"
    assert [s.title for s in article.sections] == ["Quarterly Note", "Positioning"]
    assert BlockKind.QUOTE in kinds(article)
    assert kinds(article).count(BlockKind.LIST_ITEM) == 2


def test_inline_markdown_is_stripped_but_link_text_survives():
    texts = [b.text for _s, b in article_from_text(MARKDOWN, markdown=True).blocks()]
    joined = " ".join(texts)

    assert "**" not in joined and "](" not in joined
    assert "thin volume" in joined, "bold markers go, the words stay"
    assert "opinions" in joined, "a link keeps its text"


def test_a_fenced_code_block_is_skipped():
    article = article_from_text("Before.\n\n```\nnoise = 1\n```\n\nAfter.", markdown=True)
    joined = " ".join(b.text for _s, b in article.blocks())
    assert "noise" not in joined
    assert "Before." in joined and "After." in joined


def test_plain_text_headings_need_to_stand_alone():
    """A wrapped first line is not a heading, however short it looks."""
    article = article_from_text(PLAIN, markdown=False)

    assert article.title == "A note to myself"
    assert [s.title for s in article.sections] == ["A note to myself", "Second thought"]
    # The wrapped lines rejoin into one paragraph each.
    assert [len(s.blocks) for s in article.sections] == [1, 1]
    assert "reads short forms badly" in article.sections[0].blocks[0].text


def test_blank_lines_separate_paragraphs():
    article = article_from_text("One.\n\nTwo.\n\nThree.", markdown=False)
    assert len(list(article.blocks())) == 3


def test_markdown_is_detected_when_not_declared():
    assert looks_like_markdown(MARKDOWN)
    assert not looks_like_markdown("Just some prose about markets and money.")


def test_docx_headings_and_title():
    docx = pytest.importorskip("docx", reason="python-docx not installed")

    document = docx.Document()
    document.add_heading("The Roll-Up Trade", 1)
    document.add_paragraph("Venture firms are borrowing from private equity.")
    document.add_heading("Why now", 2)
    document.add_paragraph("Because $72mm rounds are cheap.")

    buffer = io.BytesIO()
    document.save(buffer)

    article = article_from_file(buffer.getvalue(), "note.docx")
    assert article.title == "The Roll-Up Trade", "a heading beats the filename"
    assert [s.title for s in article.sections] == ["The Roll-Up Trade", "Why now"]
    assert article.source == "Word document"


def test_rewrap_rejoins_hard_wrapped_lines():
    wrapped = "The market moved sharply today, and traders\nhad opinions about why it happened.\n\nA new paragraph."
    out = _rewrap(wrapped)
    assert "traders had opinions" in out, "the line break inside a sentence closes up"
    assert "\n\n" in out, "the real paragraph break survives"


def test_rewrap_rejoins_a_hyphenated_split():
    assert "shareholders" in _rewrap("The share-\nholders voted.")


def test_an_unreadable_type_says_so():
    with pytest.raises(UnsupportedDocument, match="cannot read"):
        article_from_file(b"\x00\x01", "sheet.xlsx")
    with pytest.raises(UnsupportedDocument, match="Save it as .docx"):
        article_from_file(b"\x00\x01", "old.doc")


def test_a_text_file_keeps_its_stem_as_a_fallback_title():
    article = article_from_file(b"Only a single line of prose here.", "my-note.txt")
    assert article.title in ("my note", "Only a single line of prose here.")
    assert list(article.blocks())


def test_markdown_headings_are_found_anywhere_in_the_text():
    """Detection was anchored to the ends of the string, so it only ever saw
    a heading in text one line long — and pasted Markdown kept its hashes."""
    from textcast.ingest.documents import article_from_text, looks_like_markdown

    text = "# First section\n\nSome prose, long enough to be a paragraph.\n\n## Second\n\nMore prose."

    assert looks_like_markdown(text)
    assert [s.title for s in article_from_text(text).sections] == ["First section", "Second"]


def test_a_quotation_that_runs_over_lines_is_one_block():
    """Split, each line got its own "Start quote… End quote" spoken round it."""
    from textcast.document import BlockKind
    from textcast.ingest.documents import article_from_text

    article = article_from_text(
        "# One\n\nOrdinary prose here, long enough to be a paragraph.\n\n"
        "> The quotation begins,\n> and carries on.\n\n"
        "More prose.\n\n> A separate quotation.\n"
    )
    blocks = [b for _s, b in article.blocks()]

    kinds = [b.kind for b in blocks]
    assert kinds == [BlockKind.PARA, BlockKind.QUOTE, BlockKind.PARA, BlockKind.QUOTE]
    assert blocks[1].text == "The quotation begins, and carries on."
    assert blocks[1].spoken().count("Start quote") == 1


def test_a_quotation_ending_at_a_new_paragraph_does_not_swallow_it():
    from textcast.document import BlockKind
    from textcast.ingest.documents import article_from_text

    article = article_from_text("# One\n\n> Quoted.\nStraight back to prose without a blank line.\n")
    blocks = [b for _s, b in article.blocks()]

    assert [b.kind for b in blocks] == [BlockKind.QUOTE, BlockKind.PARA]
    assert blocks[1].text == "Straight back to prose without a blank line."
