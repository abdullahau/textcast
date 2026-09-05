"""`block.rich`: the italics and bold and links `block.text` cannot carry.

Two things to get right, and this file is split the same way. `rich_of` is
what a parsed page turns into, and only fires when there is markup worth the
second field. `sanitize_rich` is what a hand edit turns into, and has to
survive markup nobody asked for — the whole point of it running at all.
"""

from __future__ import annotations

from textcast.ingest.base import rich_of, rich_to_text, sanitize_rich
from textcast.ingest.dom import parse as parse_tree


def _rich(html: str, selector: str = "p") -> str | None:
    tree = parse_tree(f"<p>{html}</p>" if selector == "p" and not html.startswith("<p") else html)
    return rich_of(tree.css_first(selector))


# --------------------------------------------------------------- rich_of


def test_a_plain_paragraph_gets_no_rich_copy():
    assert rich_of(parse_tree("<p>Nothing bold about this.</p>").css_first("p")) is None


def test_bold_and_italic_and_link_and_code_survive():
    rich = _rich('Hello <b>world</b>, <i>truly</i> — <a href="https://x.test/a">see</a> and <code>x</code>.')
    assert rich == (
        'Hello <strong>world</strong>, <em>truly</em> — '
        '<a href="https://x.test/a">see</a> and <code>x</code>.'
    )


def test_a_span_alone_is_not_formatting_worth_a_second_copy():
    """No `em`, `strong`, `a`, `code` or `br` in it anywhere — a `<span>` for
    a tracking class is not something `text` fails to already say."""
    assert _rich('Sponsored by <span class="ad-slot">Acme</span> today.') is None


def test_an_unlisted_tag_unwraps_but_keeps_its_words():
    """A publication's own `<span class="ad-slot">` is not on the allowlist,
    but the words inside it are still what the paragraph says — checked
    alongside a `<b>` so there is a reason for `rich` to exist at all."""
    rich = _rich('Sponsored by <span class="ad-slot" style="color:red">Acme</span> <b>today</b>.')
    assert rich == "Sponsored by Acme <strong>today</strong>."
    assert "span" not in rich and "style" not in rich


def test_a_script_tag_never_survives_as_a_tag():
    rich = _rich("Before <script>alert(1)</script> after.")
    assert "<script" not in (rich or "")


def test_a_link_with_no_href_scheme_check_needed_keeps_its_tag():
    rich = _rich('See <a href="/local/page">this</a>.')
    assert rich == 'See <a href="/local/page">this</a>.'


def test_a_javascript_href_leaves_nothing_worth_a_second_copy():
    """The `<a>` is real, but its `href` is rejected, so what is left is
    exactly what `text` already says — no rich copy for that alone."""
    assert _rich('<a href="javascript:alert(1)">click</a>') is None


def test_a_bare_line_break_is_not_formatting_either():
    """A newline with nothing bold, italic, linked or coded around it reads
    the same from `text` under the reader's `pre-line` CSS — no rich copy."""
    assert _rich("First line<br>second line.") is None


def test_a_line_break_next_to_real_formatting_becomes_a_newline_not_a_tag():
    rich = _rich("First <b>line</b><br>second line.")
    assert rich == "First <strong>line</strong>\nsecond line."


def test_a_blockquote_of_two_paragraphs_gets_a_blank_line_between_them():
    tree = parse_tree("<blockquote><p>One <b>bit</b>.</p><p>Two.</p></blockquote>")
    rich = rich_of(tree.css_first("blockquote"))
    assert rich == "One <strong>bit</strong>.\n\nTwo."


def test_rich_to_text_strips_tags_and_restores_entities():
    assert rich_to_text('<strong>Bo&amp;ld</strong> plain') == "Bo&ld plain"


# ------------------------------------------------------------ sanitize_rich


def test_sanitizing_plain_text_produces_no_rich_copy():
    rich, text = sanitize_rich("Just typed text.")
    assert rich is None
    assert text == "Just typed text."


def test_sanitizing_keeps_the_four_allowed_tags():
    rich, text = sanitize_rich('<b>Bold</b> and <a href="https://x.test">link</a>')
    assert rich == '<strong>Bold</strong> and <a href="https://x.test">link</a>'
    assert text == "Bold and link"


def test_sanitizing_strips_a_script_tag_the_editor_should_never_produce_but_a_form_post_can_forge():
    rich, text = sanitize_rich("<script>alert(document.cookie)</script>Hello")
    assert rich is None
    assert "<script" not in text
    assert "Hello" in text


def test_sanitizing_strips_an_event_handler_attribute():
    rich, text = sanitize_rich('<img src=x onerror="alert(1)">Hello')
    assert rich is None
    assert "onerror" not in text
    assert text == "Hello"


def test_sanitizing_rejects_a_javascript_href():
    rich, text = sanitize_rich('<a href="javascript:alert(1)">click me</a>')
    assert rich is None
    assert text == "click me"


def test_sanitizing_drops_an_onclick_from_an_otherwise_safe_link():
    rich, text = sanitize_rich('<a href="https://x.test" onclick="alert(1)">safe</a>')
    assert rich == '<a href="https://x.test">safe</a>'
    assert "onclick" not in rich
    assert text == "safe"
