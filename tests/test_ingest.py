from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from textcast.document import Article, BlockKind
from textcast.ingest import adapter_names, parse_html, pick_adapter
from textcast.ingest.base import is_junk_block
from textcast.ingest.dom import parse as parse_tree
from textcast.ingest.dom import same
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


def test_a_substack_footnote_is_inlined_where_it_is_cited():
    """Substack collects its footnotes at the foot of the post, in their own
    `.footnote` divs, rather than in a list Bloomberg-style — but a listener
    still needs the note read where the claim is made, not appended after."""
    html = """
    <html><head>
      <link rel="canonical" href="https://example.substack.com/p/a-post">
      <meta property="og:title" content="A Post">
    </head><body>
      <h1 class="post-title">A Post</h1>
      <div class="available-content"><div class="body markup">
        <p>A claim worth citing<span>
          <a data-component-name="FootnoteAnchorToDOM" id="footnote-anchor-1"
             href="#footnote-1" class="footnote-anchor">1</a>
        </span> and the rest of the sentence.</p>
        <div data-component-name="FootnoteToDOM" class="footnote">
          <a id="footnote-1" href="#footnote-anchor-1" class="footnote-number">1</a>
          <div class="footnote-content"><p>Where the claim comes from.</p></div>
        </div>
      </div></div>
    </body></html>
    """
    article = parse_html(html)
    assert article.adapter == "substack"
    text = " ".join(b.text for _s, b in article.blocks())
    assert "[Footnote 1: Where the claim comes from.]" in text
    # The div at the foot of the post must not also survive as its own block.
    assert "Where the claim comes from" not in text.replace(
        "[Footnote 1: Where the claim comes from.]", ""
    )


def test_blogspot_matches_ahead_of_the_newsletter_false_positive():
    """Blogger's own caption markup is `table.tr-caption-container`, which
    matches the newsletter adapter's `table[class*="container"]` check on
    every single post. This has to win the registry order, not just parse
    better once chosen."""
    html = """
    <html><head><meta name="generator" content="blogger">
      <meta property="og:title" content="A Post">
    </head><body>
      <h1 class="title">A Blog</h1>
      <div class="post-body entry-content">
        <p>A paragraph long enough to count as real prose for this test.</p>
        <table class="tr-caption-container"><tr><td>
          <img src="https://example.com/pic.jpg" width="400">
        </td></tr><tr><td class="tr-caption">A caption.</td></tr></table>
      </div>
    </body></html>
    """
    assert pick_adapter("", parse_tree(html)).name == "blogspot"
    article = parse_html(html)
    assert article.adapter == "blogspot"
    figures = [b for _s, b in article.blocks() if b.kind is BlockKind.FIGURE]
    assert len(figures) == 1
    assert figures[0].media["caption"] == "A caption."
    assert not any(b.kind is BlockKind.TABLE for _s, b in article.blocks())


def test_blogspot_promotes_a_fully_bold_paragraph_to_a_heading():
    """A subheading not in `BLOCK_SELECTOR` (`<div>`, `<span>`) is not merely
    unstructured — the shared walk never visits it, so the words are gone
    outright unless this promotes it to a real heading first."""
    html = """
    <html><head><meta name="generator" content="blogger"></head><body>
      <div class="post-body entry-content">
        <div class="separator"><b>A Bold Subhead</b></div>
        <p>The paragraph that follows the subhead, long enough to be kept.</p>
        <p><b>&nbsp;</b>A paragraph that only starts with bold text, which is
           prose and must stay a paragraph, not become a heading of its own.</p>
      </div>
    </body></html>
    """
    article = parse_html(html)
    assert any(s.title == "A Bold Subhead" for s in article.sections)
    texts = [b.text for _s, b in article.blocks()]
    assert any(t.startswith("A paragraph that only starts with bold") for t in texts)


def test_blogspot_promotes_a_bare_div_to_a_paragraph():
    """Blogger's editor writes a plain line of prose straight into a `<div>`
    as often as a `<p>` — commonly the line right after a picture. `<div>` is
    not in `BLOCK_SELECTOR`, so it is not merely unstructured, it is never
    visited and the sentence vanishes outright, unless promoted first. A
    `<div>` that itself wraps another block (the layout-wrapper case) must be
    left alone."""
    html = """
    <html><head><meta name="generator" content="blogger"></head><body>
      <div class="post-body entry-content">
        <div class="separator"><a href="x.jpg"><img src="x.jpg" width="400"></a></div>
        <div style="text-align: justify;">A line of prose Blogger put in a div
           instead of a p, long enough to look like a real paragraph.</div>
        <div class="wrapper"><p>A paragraph inside a layout div, which must
           stay exactly where it is and not be duplicated.</p></div>
      </div>
    </body></html>
    """
    article = parse_html(html)
    texts = [b.text for _s, b in article.blocks()]
    assert any(t.startswith("A line of prose Blogger put in a div") for t in texts)
    assert sum(t.startswith("A paragraph inside a layout div") for t in texts) == 1


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
    # Against the registry, not a copy of it: a new publication is one file
    # and one line there, and this list was the third place to remember.
    assert {load(p).adapter for p in PAGES} <= set(adapter_names())


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
def test_blogspot_adapter_is_chosen_and_scopes_to_the_post():
    """A Blogger permalink page's sidebar — "Popular Posts", a year-by-year
    "Blog Archive" — must not read as sections of the article, and the page's
    own `<h1>` (the *blog's* name) must not become the post's title."""
    page = next((p for p in PAGES if "Scaling and Profitability" in p.name), None)
    if page is None:
        pytest.skip("Blogspot page not in corpus")

    html = page.read_text(encoding="utf-8", errors="replace")
    assert pick_adapter("", parse_tree(html)).name == "blogspot"

    article = load(page)
    assert article.title == "The Scaling and Profitability Trade off: Venture Capital's Weakest Link!"
    assert article.source == "Musings on Markets"
    assert article.author == "Aswath Damodaran"
    assert article.published_at and article.published_at.startswith("2026-09-02")
    assert not any(s.title == "Popular Posts" for s in article.sections)
    assert not any(s.title == "Blog Archive" for s in article.sections)

    # A subheading written as a fully-bold paragraph is promoted, not lost.
    assert any(s.title == "Scaling versus Business Building" for s in article.sections)
    # A picture captioned with a `table.tr-caption-container`, Blogger's own
    # markup, survives as a figure rather than being read as a data table.
    assert any(b.kind is BlockKind.FIGURE for _s, b in article.blocks())
    assert not any(b.kind is BlockKind.TABLE for _s, b in article.blocks())


@pytest.mark.skipif(not PAGES, reason="corpus not present")
def test_every_page_in_the_corpus_has_a_byline():
    """Except where the publication does not print one.

    The Economist does not byline its leaders — that is its editorial policy,
    not a parse that missed something, and there is nothing in the page to
    find. `author` is editable on the article for anything pasted.
    """
    for page in PAGES:
        article = load(page)
        if article.adapter == "economist":
            assert not article.author, "the leaders are unsigned; a name here came from somewhere"
            continue
        assert article.author, f"{page.name} parsed without an author"


def test_node_identity_does_not_serialize_the_subtree():
    """`dom.same` must stay a `mem_id` compare, not fall back to `==`.

    `==` on two lexbor nodes serializes both subtrees and compares the
    markup. It gives the right answer, so nothing fails when someone writes
    it — the page just takes eighteen seconds to parse instead of a
    thirtieth of one, because `_within` asks the question tens of thousands
    of times. Timing it is the only way the difference shows up, and the
    gap is four orders of magnitude, so the threshold does not need to be
    tight.
    """
    import time

    tree = parse_tree("<div><p>x</p></div>" + "<section><p>y</p></section>" * 1500)
    body = tree.css_first("body")
    other = tree.css_first("body")

    start = time.perf_counter()
    for _ in range(2000):
        assert same(body, other)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f"2000 identity tests took {elapsed:.2f}s; same() is serializing"


def test_same_tells_two_different_nodes_apart():
    """Cheap is no use if it is also wrong."""
    tree = parse_tree("<div id='a'><p>x</p></div><div id='b'><p>x</p></div>")
    first, second = tree.css("div")

    assert same(first, first)
    assert not same(first, second), "identical markup is not the same node"
    assert not same(first, None)
    assert same(None, None)
