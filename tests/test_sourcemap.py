"""Word-level source mapping: `sourcemap.py`, `normalize_tracked`, `apply_tracked`.

Two things matter here, and the tests are organised around them. First,
`normalize_tracked` must never disagree with `normalize` about what the
engine hears — that invariant is what lets phase two zip an aligner's word
list against the source map's word list positionally. Second, each origin
span must point at the *right* displayed text, not just at something.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from textcast.document import Block, BlockKind
from textcast.ingest import parse_html
from textcast.normalize import normalize, normalize_tracked
from textcast.sourcemap import TrackedText, tracked_replace, tracked_strip, tracked_sub, words

CORPUS = Path(__file__).with_name("corpus")
PAGES = sorted(CORPUS.glob("*.html"))


def origins(text: str, **kwargs) -> list[tuple[str, str | None]]:
    """Each spoken word next to the displayed substring its origin names."""
    tracked = normalize_tracked(text, **kwargs)
    return [
        (word, text[orig[0] : orig[1]] if orig else None) for word, orig in words(tracked)
    ]


# --------------------------------------------------------------------------
# normalize_tracked must never disagree with normalize about the text
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The $4.4tn company raised its earnings guidance for Q3.",
        "$4.4tn evaporated overnight after the FY24 report.",
        "It cost £5bn and rose 12.5% in H1 2019-21, at 8am on 2 Jul.",
        "*Emphasis* survives, and so does a [Footnote 1: a citation] aside.",
        "",
        "   ",
        "M&A bankers watched the Q&A closely.",
        "LIBOR fell to 4.3% after the FOMC statement at 10:47.",
    ],
)
@pytest.mark.parametrize("g2p", ["misaki", "espeak"])
def test_tracked_text_matches_the_untracked_text(text, g2p):
    assert normalize_tracked(text, g2p=g2p).text == normalize(text, g2p=g2p)


@pytest.mark.skipif(not PAGES, reason="corpus not present")
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem[:30])
def test_tracked_text_matches_the_untracked_text_over_the_corpus(page):
    article = parse_html(page.read_text(encoding="utf-8", errors="replace"))
    for _section, block in article.blocks():
        for g2p in ("misaki", "espeak"):
            spoken = block.spoken(g2p=g2p)
            tracked = block.spoken_tracked(g2p=g2p)
            assert tracked.text == spoken, (block.id, g2p, tracked.text, spoken)


# --------------------------------------------------------------------------
# each origin points at the right displayed text
# --------------------------------------------------------------------------


def test_money_expansion_merges_onto_the_one_displayed_token():
    # Every spoken word money.py expands "$4.4tn" into points back at
    # exactly "$4.4tn" -- the collapse this module exists for, with no
    # special case written for money anywhere in sourcemap.py itself.
    result = origins("the $4.4tn company")
    money_words = [orig for word, orig in result if word not in ("the", "company")]
    assert money_words == ["$4.4tn"] * len(money_words)
    assert len(money_words) >= 4  # "4 point four trillion dollar"


def test_decimal_point_spelled_out_keeps_the_original_number_as_origin():
    result = origins("It rose 14.6 percent overnight.")
    number_words = [orig for word, orig in result if word in ("point",) or word.isdigit()]
    assert all(orig == "14.6" for orig in number_words)


def test_year_range_words_point_at_the_written_span():
    result = origins("profits over 2019-21 grew")
    year_words = [orig for word, orig in result if orig and "2019" in orig]
    assert year_words and all(o == "2019-21" for o in year_words)


def test_plain_prose_keeps_a_precise_one_to_one_mapping():
    text = "Markets steadied on Thursday after the announcement."
    result = origins(text)
    assert result == [
        ("Markets", "Markets"),
        ("steadied", "steadied"),
        ("on", "on"),
        ("Thursday", "Thursday"),
        ("after", "after"),
        ("the", "the"),
        ("announcement.", "announcement."),
    ]


def test_emphasis_markers_are_dropped_but_the_word_still_points_at_the_source():
    text = "It was *huge* news."
    result = origins(text)
    assert ("huge", "*huge*") in result


def test_footnote_insertion_is_dropped_or_points_at_the_bracket():
    text = "A claim [Footnote 1: a citation] follows."
    tracked = normalize_tracked(text)
    start, end = text.index("["), text.index("]") + 1
    for word, orig in words(tracked):
        if orig is not None and word not in ("A", "claim", "follows."):
            # Every word the footnote rewrite produced must point somewhere
            # inside the original bracketed footnote -- never at unrelated
            # text before or after it, and never at the whole block (the
            # very bug this module first shipped with).
            assert start <= orig[0] and orig[1] <= end, (word, orig)


def test_quote_wrapping_gives_the_markers_no_origin():
    block = Block(kind=BlockKind.QUOTE, text="This changes everything.")
    tracked = block.spoken_tracked()
    pairs = words(tracked)
    marker_words = {"Start", "quote."}
    for word, orig in pairs:
        if word in marker_words:
            assert orig is None, (word, orig)
    real_words = [w for w, o in pairs if o is not None]
    assert real_words == ["This", "changes", "everything."]


# --------------------------------------------------------------------------
# the plumbing itself: TrackedText, tracked_sub, tracked_replace, tracked_strip
# --------------------------------------------------------------------------


def test_identity_maps_every_character_to_itself():
    tt = TrackedText.identity("hello world")
    assert tt.origin_of(0, 5) == (0, 5)
    assert tt.origin_of(6, 11) == (6, 11)


def test_tracked_sub_with_a_shorter_replacement_keeps_the_full_match_as_origin():
    original = "call it start-up culture"
    tt = TrackedText.identity(original)
    tt = tracked_sub(re.compile(r"start-up"), "startup", tt)
    assert tt.text == "call it startup culture"
    start = tt.text.index("startup")
    orig = tt.origin_of(start, start + len("startup"))
    assert original[orig[0] : orig[1]] == "start-up"


def test_tracked_replace_on_literal_text():
    tt = TrackedText.identity("a—b")
    tt = tracked_replace("—", ", ", tt)
    assert tt.text == "a, b"
    orig = tt.origin_of(1, 3)
    assert "—" in "a—b"[orig[0] : orig[1]]


def test_tracked_strip_removes_leading_and_trailing_whitespace_only():
    tt = TrackedText.identity("  hi there  ")
    stripped = tracked_strip(tt)
    assert stripped.text == "hi there"
    assert stripped.origin_of(0, 2) == (2, 4)


def test_tracked_strip_of_an_all_whitespace_string_is_empty():
    assert tracked_strip(TrackedText.identity("   ")).text == ""


def test_wrap_adds_unoriginated_text_at_either_end():
    tt = TrackedText.identity("core").wrap(prefix="[", suffix="]")
    assert tt.text == "[core]"
    assert tt.origin_of(0, 1) is None
    assert tt.origin_of(5, 6) is None
    assert tt.origin_of(1, 5) == (0, 4)


def test_a_collapsed_span_does_not_narrow_when_a_sub_range_is_queried():
    # The bug this module first shipped with: a replacement's whole output
    # took the *whole original string's* origin because a later query into
    # a sub-range of an untouched span was not offset -- it returned the
    # entire span's bounds. Locked down explicitly, not just implied by the
    # corpus-wide text-equality tests, since that bug did not change the
    # text at all, only the origins.
    tt = TrackedText.identity("The $4.4tn company")

    def money(_m):
        return "4 point four trillion dollars"

    tt = tracked_sub(re.compile(r"\$4\.4tn"), money, tt)
    assert tt.text == "The 4 point four trillion dollars company"
    word_span = tt.origin_of(tt.text.index("point"), tt.text.index("point") + len("point"))
    assert word_span == (4, 10)  # "$4.4tn", not the whole string
