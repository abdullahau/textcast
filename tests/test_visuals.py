"""Pictures, tables and live charts as blocks.

The unit tests here are the filters — what a visual is, and what is furniture
wearing a picture. The corpus tests are the ones that matter: they hold a real
Alphaville post whose whole argument rests on a table, and a real Money Stuff
issue whose chart used to be dropped with `figure` in the noise list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textcast.document import BlockKind, to_markdown
from textcast.ingest import parse_html
from textcast.ingest.dom import parse as parse_tree
from textcast.ingest.visuals import DEFAULT_RULES, VisualRules, visual_block

CORPUS = Path(__file__).with_name("corpus")


def only(html: str, selector: str, rules: VisualRules = DEFAULT_RULES, url: str = ""):
    tree = parse_tree(html)
    return visual_block(tree.css_first(selector), rules, base_url=url)


# --------------------------------------------------------------- pictures


def test_a_figure_becomes_a_block_carrying_its_address_and_its_caption():
    block = only(
        '<figure><img src="https://x.test/chart.png" width="900" alt="CPI by year">'
        "<figcaption>Inflation, year on year</figcaption></figure>",
        "figure",
    )

    assert block.kind is BlockKind.FIGURE
    assert block.text == "Figure: Inflation, year on year"
    assert block.media["src"] == "https://x.test/chart.png"


def test_a_caption_that_is_only_a_credit_is_kept_apart_from_the_caption():
    """"© Reuters" names who took it, not what it shows."""
    block = only(
        '<figure><img src="https://x.test/a.png" width="900" alt="Colossus I">'
        "<figcaption>© Reuters</figcaption></figure>",
        "figure",
    )

    assert block.text == "Figure: Colossus I"
    assert block.media["credit"] == "© Reuters"


def test_the_widest_candidate_wins_and_a_cloudinary_path_is_not_split_on_its_commas():
    """Substack serves every picture through Cloudinary: `w_1456,c_limit,f_webp`.

    Splitting the srcset on every comma cut that path in three and produced an
    address that resolved against the article's own host.
    """
    block = only(
        "<figure><img "
        'srcset="https://cdn.test/image/fetch/w_424,c_limit,f_webp/c.png 424w, '
        'https://cdn.test/image/fetch/w_1456,c_limit,f_webp/c.png 1456w" '
        'src="https://cdn.test/fallback.png" width="1456"></figure>',
        "figure",
    )

    assert block.media["src"] == "https://cdn.test/image/fetch/w_1456,c_limit,f_webp/c.png"


def test_a_picture_element_puts_its_candidates_on_a_source_not_on_the_img():
    block = only(
        "<figure><picture>"
        '<source type="image/webp" srcset="https://cdn.test/big.webp 1456w">'
        '<img src="https://cdn.test/small.png" width="1456">'
        "</picture></figure>",
        "figure",
    )

    assert block.media["src"] == "https://cdn.test/big.webp"


def test_a_tracking_pixel_is_not_a_picture():
    assert only('<img src="https://spoor-api.ft.com/px.gif" width="1" height="1">', "img") is None


def test_a_teaser_card_is_furniture_however_large_its_picture():
    html = (
        '<div class="o-teaser"><img class="o-teaser__image" '
        'src="https://x.test/teaser.jpg" width="340" alt="Read next"></div>'
    )

    assert only(html, "img") is None


def test_an_icon_is_too_small_to_be_a_graphic():
    assert only('<img src="https://x.test/i.png" width="24" height="24">', "img") is None


def test_a_relative_address_is_resolved_against_the_article():
    block = only(
        '<figure><img src="/media/chart.png" width="900"></figure>',
        "figure",
        url="https://x.test/opinion/2026/piece",
    )

    assert block.media["src"] == "https://x.test/media/chart.png"


def test_a_javascript_address_never_reaches_the_page():
    """The reader puts this in an `src`. A parsed page is not a trusted page."""
    assert only('<figure><img src="javascript:alert(1)" width="900"></figure>', "figure") is None


def test_a_wrapper_holding_the_whole_article_is_not_a_figure():
    """`article-grid--no-full-width-graphics` matched `[class*="graphic"]` once.

    It produced one figure block holding the lead image and swallowed every
    paragraph after it, because the walk treats a figure's contents as its own.
    """
    body = "".join(f"<p>{'Sentence about the market. ' * 6}</p>" for _ in range(4))
    html = f'<div class="content-graphic"><img src="https://x.test/a.png" width="900">{body}</div>'

    assert only(html, "div") is None


# ----------------------------------------------------------------- tables


TABLE = (
    "<table><thead><tr><th>Operator</th><th>FY25</th></tr></thead>"
    "<tbody><tr><td>Northwind</td><td>4.4%</td></tr>"
    "<tr><td>Calder</td><td>4.2%</td></tr></tbody></table>"
)


def test_a_data_table_becomes_a_block_holding_its_cells():
    block = only(TABLE, "table")

    assert block.kind is BlockKind.TABLE
    assert block.media["rows"][0] == ["Operator", "FY25"]
    assert block.media["header"] is True


def test_a_layout_table_is_not_a_table():
    """Newsletters are built out of these, and one is never data."""
    outer = f'<table><tr><td>{TABLE}</td></tr></table>'

    assert only(outer, "table") is None


def test_a_single_column_table_is_not_a_table():
    assert only("<table><tr><td>One</td></tr><tr><td>Two</td></tr></table>", "table") is None


def test_a_mostly_empty_header_row_is_the_table_title():
    """The FT leaves `<caption>` empty and spans the title across the header."""
    html = (
        "<table><caption></caption><thead><tr>"
        "<th>Ker-CHING</th><th></th><th></th><th></th></tr></thead>"
        "<tbody><tr><td>Two years</td><td>160%</td><td>134%</td><td>113%</td></tr>"
        "<tr><td>Three years</td><td>177%</td><td>150%</td><td>130%</td></tr></tbody></table>"
    )

    assert only(html, "table").text == "Table: Ker-CHING"


def test_a_footer_spanning_a_thousand_columns_does_not_become_a_thousand_cells():
    html = (
        "<table><tbody><tr><td>A</td><td>1</td></tr><tr><td>B</td><td>2</td></tr></tbody>"
        '<tfoot><tr><td colspan="1000">FTAV</td></tr></tfoot></table>'
    )
    block = only(html, "table")

    assert max(len(row) for row in block.media["rows"]) == 2
    assert block.media["foot"] == "FTAV"


# ----------------------------------------------------------- live charts


def test_a_datawrapper_frame_is_a_chart():
    block = only(
        '<iframe title="Contract length by operator" src="https://datawrapper.dwcdn.net/aB3/2/">'
        "</iframe>",
        "iframe",
    )

    assert block.kind is BlockKind.EMBED
    assert block.text == "Chart: Contract length by operator"


def test_a_frame_from_anywhere_else_is_not():
    """An allowlist, because an iframe is far more often an advert."""
    assert only('<iframe src="https://www.googletagmanager.com/ns.html?id=G-1"></iframe>', "iframe") is None


def test_the_page_shows_the_caption_and_the_audio_says_the_label():
    """A reader can see it is a table. Only a listener has to be told."""
    captioned = only(
        '<figure><img src="https://x.test/a.png" width="900">'
        "<figcaption>Inflation, year on year</figcaption></figure>",
        "figure",
    )
    bare = only('<figure><img src="https://x.test/a.png" width="900"></figure>', "figure")

    assert captioned.text == "Figure: Inflation, year on year"
    assert captioned.media["caption"] == "Inflation, year on year"
    # Nothing to print under a picture the publication did not caption.
    assert bare.text == "Figure."
    assert "caption" not in bare.media


def test_a_title_lifted_out_of_the_header_row_is_not_printed_twice():
    html = (
        "<table><thead><tr><th>Ker-CHING</th><th></th><th></th><th></th></tr></thead>"
        "<tbody><tr><td>Two years</td><td>160%</td><td>134%</td><td>113%</td></tr>"
        "<tr><td>Three years</td><td>177%</td><td>150%</td><td>130%</td></tr></tbody></table>"
    )
    block = only(html, "table")

    assert block.text == "Table: Ker-CHING"
    assert "caption" not in block.media, "the header row already shows it"


# --------------------------------------------------- quotes and dimensions


def test_a_quote_keeps_the_paragraphs_it_was_published_with():
    """`text_of` joins every descendant with a space, which read a bolded
    lead-in straight into the sentence after it."""
    from textcast.ingest.base import blocks_from_dom

    html = (
        "<article><blockquote>"
        "<p><strong>Compute Services Agreements with Third Parties</strong></p>"
        "<p>We believe our compute infrastructure provides flexibility.</p>"
        "</blockquote></article>"
    )
    section = blocks_from_dom(parse_tree(html).css_first("article"))[0]
    quote = section.blocks[0]

    assert quote.kind is BlockKind.QUOTE
    assert quote.text == (
        "Compute Services Agreements with Third Parties\n\n"
        "We believe our compute infrastructure provides flexibility."
    )


def test_a_quote_of_one_paragraph_gains_no_break():
    from textcast.ingest.base import blocks_from_dom

    html = "<article><blockquote><p>Markets are efficient.</p></blockquote></article>"
    section = blocks_from_dom(parse_tree(html).css_first("article"))[0]

    assert section.blocks[0].text == "Markets are efficient."


def test_a_paragraph_break_is_spoken_as_a_sentence_break():
    """Collapsed to a space, the lead-in ran into the sentence after it."""
    from textcast.document import Block

    block = Block(kind=BlockKind.PARA, text="Compute Services Agreements\n\nWe believe it.")

    assert block.spoken() == "Compute Services Agreements. We believe it."


def test_a_line_that_already_ends_in_a_full_stop_gains_nothing():
    from textcast.document import Block

    block = Block(kind=BlockKind.PARA, text="It ended here.\n\nAnd began again.")

    assert block.spoken() == "It ended here. And began again."


def test_an_emoji_is_not_read_out_by_name():
    """espeak says "money bag" for the FT's Ker-CHING table heading."""
    from textcast.document import Block

    assert Block(kind=BlockKind.PARA, text="Ker-CHING \U0001F4B0").spoken() == "Ker-CHING"
    # A credit means to say these, and they are named correctly.
    assert "\u00a9" in Block(kind=BlockKind.PARA, text="\u00a9 Reuters").spoken()


def test_a_table_speaks_its_caption_and_never_its_cells():
    from textcast.document import Block

    block = Block(
        kind=BlockKind.TABLE,
        text="Table: Ker-CHING",
        media={"rows": [["Depreciation life", "$7bn"], ["Two years", "160%"]], "header": True,
               "foot": "FTAV"},
    )
    spoken = block.spoken()

    assert spoken == "Table: Ker-CHING"
    for cell in ("Depreciation life", "$7bn", "Two years", "160%", "FTAV"):
        assert cell not in spoken


def test_a_figure_speaks_its_caption_and_never_its_address():
    from textcast.document import Block

    block = Block(
        kind=BlockKind.FIGURE,
        text="Figure: Colossus I",
        media={"src": "https://images.ft.com/v3/image/raw/ftcms.webp", "alt": "Colossus I"},
    )

    assert block.spoken() == "Figure: Colossus I"


def test_a_picture_carries_the_ratio_it_was_published_at():
    """Without it the reader reserves no box and the page jumps when it lands."""
    block = only(
        '<figure><img src="https://x.test/a.png" width="3000" height="1687"></figure>',
        "figure",
    )

    assert (block.media["w"], block.media["h"]) == (3000, 1687)


# ------------------------------------------------------------ the corpus


def load(name: str):
    page = next((p for p in CORPUS.glob("*.html") if name in p.name), None)
    if page is None:
        pytest.skip(f"{name} not in the corpus")
    return parse_html(page.read_text(encoding="utf-8", errors="replace"))


def test_the_alphaville_ready_reckoner_is_read_where_the_prose_cites_it():
    """The whole point of a visual block: it lands in the argument, not after it."""
    article = load("SpaceX considered as a leasing company")
    blocks = [b for _s, b in article.blocks()]
    tables = [b for b in blocks if b.kind is BlockKind.TABLE]

    assert len(tables) == 1
    assert tables[0].text == "Table: Ker-CHING 💰"
    assert tables[0].media["rows"][1][:2] == ["Depreciation life", "$7bn"]

    before = blocks[blocks.index(tables[0]) - 1]
    assert before.text.startswith("You can do a little ready reckoner")


def test_the_alphaville_lead_image_survives_and_the_event_promo_does_not():
    article = load("SpaceX considered as a leasing company")
    figures = [b for _s, b in article.blocks() if b.kind is BlockKind.FIGURE]

    assert [f.text for f in figures] == ["Figure: Colossus I © Reuters"]
    assert "images.ft.com" in figures[0].media["src"]


def test_a_money_stuff_chart_is_no_longer_dropped_with_the_noise():
    """`figure` headed the Bloomberg noise list, which cost every article image."""
    article = load("Strategy Does a Stretch")
    figures = [b for _s, b in article.blocks() if b.kind is BlockKind.FIGURE]

    assert figures, "the article image was dropped"
    assert figures[0].media["src"].startswith("https://assets.bwbx.io/")


def test_a_substack_post_keeps_its_chart_its_table_and_its_frame():
    article = load("The depreciation problem")
    kinds = [b.kind for _s, b in article.blocks() if b.media]

    assert kinds == [BlockKind.FIGURE, BlockKind.TABLE, BlockKind.EMBED]
    assert article.adapter == "substack"


def test_a_substack_subscribe_widget_is_not_a_figure():
    article = load("The depreciation problem")

    assert all("avatar" not in (b.media or {}).get("src", "") for _s, b in article.blocks())
    assert all("Subscribe to" not in b.text for _s, b in article.blocks())


# ------------------------------------------------------------- the export


def test_a_visual_survives_an_export():
    """A Markdown export that loses the table is not the article."""
    article = load("SpaceX considered as a leasing company")
    text = to_markdown(article)

    assert "![Figure: Colossus I © Reuters](https://images.ft.com/" in text
    assert "| Depreciation life | $7bn |" in text


# --------------------------------------------------------- the round trip


def test_a_visual_block_survives_the_database(conn):
    """`media` is a column of its own, so a rebuild reads back what was parsed."""
    from textcast import db
    from textcast.document import Article, Block, Section

    doc = Article(
        title="A charted note",
        sections=[
            Section(
                title="One",
                blocks=[
                    Block(kind=BlockKind.PARA, text="The body of it."),
                    Block(
                        kind=BlockKind.TABLE,
                        text="Table: Ker-CHING",
                        media={"rows": [["A", "B"], ["1", "2"]], "header": True},
                    ),
                    Block(
                        kind=BlockKind.FIGURE,
                        text="Figure: A chart",
                        media={"src": "https://x.test/c.png", "alt": ""},
                    ),
                ],
            )
        ],
    ).renumber()

    article_id = db.save_article(doc, conn)
    back = db.load_article(article_id, conn)
    blocks = [b for _s, b in back.blocks()]

    assert blocks[1].media["rows"] == [["A", "B"], ["1", "2"]]
    assert blocks[2].media["src"] == "https://x.test/c.png"
    assert blocks[0].media is None
