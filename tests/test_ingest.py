from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from textcast.document import Article, BlockKind
from textcast.ingest import parse_html, pick_adapter
from textcast.ingest.base import is_junk_block
from textcast.ingest.dom import parse as parse_tree
from textcast.ingest.newsletter import article_from_eml, is_cutoff, parse_eml

CORPUS = Path(__file__).with_name("corpus")
PAGES = sorted(CORPUS.glob("*.html"))


def load(path: Path) -> Article:
    return parse_html(path.read_text(encoding="utf-8", errors="replace"))


@pytest.mark.skipif(not PAGES, reason="corpus not present")
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem[:30])
def test_every_page_parses_to_real_prose(page: Path):
    article = load(page)
    assert article.title and article.title != "Untitled"
    assert article.sections, "no sections extracted"
    assert article.word_count > 400, f"only {article.word_count} words"
    for _section, block in article.blocks():
        assert block.text.strip()
        assert not is_junk_block(block.text)


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_block_ids_are_unique_and_stable():
    article = load(PAGES[0])
    ids = [b.id for _s, b in article.blocks()]
    assert len(ids) == len(set(ids))
    assert ids == [b.id for _s, b in Article.from_dict(article.to_dict()).blocks()]


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_bloomberg_adapter_is_chosen_and_finds_money_stuff():
    page = next(p for p in PAGES if "Bloomberg" in p.name)
    html = page.read_text(encoding="utf-8", errors="replace")
    assert pick_adapter("", parse_tree(html)).name == "bloomberg"
    assert load(page).series == "Money Stuff"


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_ft_adapter_is_chosen():
    page = next((p for p in PAGES if "roll-up" in p.name), None)
    if page is None:
        pytest.skip("FT page not in corpus")
    article = load(page)
    assert article.source == "Financial Times"
    # Share rows and the "Follow the topics" rail must not survive.
    assert all("opens in a new window" not in b.text for _s, b in article.blocks())
    assert all(s.title != "Follow the topics in this article" for s in article.sections)


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_footnotes_are_inlined_not_appended():
    page = next((p for p in PAGES if "Drug-Trial" in p.name), None)
    if page is None:
        pytest.skip("page not in corpus")
    text = " ".join(b.text for _s, b in load(page).blocks())
    assert "[Footnote 1:" in text


def test_quote_blocks_get_spoken_markers():
    from textcast.document import Block

    quote = Block(kind=BlockKind.QUOTE, text="Markets are efficient.")
    assert quote.spoken() == "Start quote. Markets are efficient. End quote."
    # With a dedicated quote voice, the cue is redundant.
    assert quote.spoken(quote_markers=False) == "Markets are efficient."


def test_junk_block_matching():
    assert is_junk_block("In this Article")
    assert is_junk_block("Some headline on x (opens in a new window)")
    assert is_junk_block("4 min read")
    assert not is_junk_block("The bank said it would open a new window into its balance sheet.")


def _message(html: str, subject: str = "Money Stuff: Test Issue") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Matt Levine <noreply@mail.bloombergbusiness.com>"
    msg["List-Id"] = "money-stuff.mail.bloombergbusiness.com"
    msg["Date"] = "Tue, 2 Jul 2025 12:00:00 +0000"
    msg.set_content("plain text fallback")
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


_PARA_A = "First real paragraph of the issue, long enough to survive pruning. " * 3
_PARA_B = "Second real paragraph, also comfortably long enough to be kept. " * 3

NEWSLETTER_HTML = f"""
<html><body><table class="body"><tr><td>
<p>View this email in your browser</p>
<h1>Test Issue</h1>
<p>{_PARA_A}</p>
<p>{_PARA_B}</p>
<blockquote>A quoted passage that runs on for a while so it is kept.</blockquote>
<p>You received this message because you subscribed to this list.</p>
<p>Unsubscribe here</p>
</td></tr></table></body></html>
"""


def test_eml_headers_give_series_and_date():
    html, meta = parse_eml(_message(NEWSLETTER_HTML))
    assert "Test Issue" in html
    assert meta.subject == "Money Stuff: Test Issue"
    assert meta.series == "Money Stuff"
    assert meta.date and meta.date.startswith("2025-07-02")


def test_eml_strips_chrome_and_keeps_prose():
    article = article_from_eml(_message(NEWSLETTER_HTML))
    texts = [b.text for _s, b in article.blocks()]
    joined = " ".join(texts).lower()

    assert "view this email in your browser" not in joined
    assert "unsubscribe" not in joined
    assert "you received this message" not in joined
    assert any("First real paragraph" in t for t in texts)
    assert any(b.kind is BlockKind.QUOTE for _s, b in article.blocks())
    assert article.series == "Money Stuff"


def test_cutoff_detection():
    assert is_cutoff("You received this message because you signed up")
    assert is_cutoff("Copyright © 2026 Bloomberg")
    assert not is_cutoff("The company received a message from its auditor")


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_money_stuff_issues_are_grouped_into_a_series():
    """Every /newsletters/ issue is detected; the 2019 column is not one."""
    by_name = {p.stem: load(p).series for p in PAGES}
    issues = [name for name, series in by_name.items() if series == "Money Stuff"]
    assert len(issues) >= 5

    column = next((n for n in by_name if "Deals on the Train" in n), None)
    if column:
        assert by_name[column] is None, "an /opinion/articles/ column is not a newsletter issue"


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_adapter_is_recorded_on_the_article():
    assert {load(p).adapter for p in PAGES} <= {"bloomberg", "ft", "newsletter", "generic"}


def test_generic_extractor_finds_the_body_and_drops_the_rails():
    prose = "The market moved sharply today, and traders had opinions about why. " * 4
    html = f"""
    <html><body>
      <nav><a href="/a">Home</a><a href="/b">Markets</a><a href="/c">Opinion</a></nav>
      <div class="sidebar"><a href="/1">Teaser one</a><a href="/2">Teaser two</a></div>
      <div class="article-content"><p>{prose}</p><p>{prose}</p><p>{prose}</p></div>
      <footer><a href="/x">Privacy</a></footer>
    </body></html>
    """
    article = parse_html(html, prefer="generic")
    texts = [b.text for _s, b in article.blocks()]
    assert len(texts) == 3
    assert all("moved sharply" in t for t in texts)


def test_a_short_pasted_note_is_accepted(conn):
    """The paywall guard is for web pages, not for text you typed yourself."""
    from textcast.service import IngestError, ingest

    result = ingest(
        text="A short note.\n\nOnly a couple of lines, but worth keeping.",
        title="Short note",
        build=False,
        tags=["Notes"],
    )
    assert result.word_count < 60
    assert result.tags == ["Notes"]

    # Whitespace-only reads as "you gave me nothing", which is the truth.
    with pytest.raises(IngestError, match="give a url, some text"):
        ingest(text="   ", build=False)


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_a_bloomberg_page_names_who_wrote_it():
    """Every Money Stuff issue says Matt Levine in its head, three times over,
    and the adapter read none of them."""
    page = next((p for p in PAGES if "Bloomberg" in p.name), None)
    assert page is not None, "the corpus has no Bloomberg page"

    article = load(page)

    assert article.source == "Bloomberg"
    assert article.author, "no byline extracted"
    assert article.author == "Matt Levine"


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_every_page_in_the_corpus_has_a_byline():
    for page in PAGES:
        assert load(page).author, f"{page.name} parsed without an author"
