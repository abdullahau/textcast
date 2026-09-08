"""word_wrap: turning a block's text/rich HTML into <span data-w="N"> spans.

The word list is taken as given -- exactly what `WordTiming.text` would
produce -- because this module's job is locating and wrapping, not deciding
what merges into one word. That decision is `audio.compose_word_timings`'s,
tested there and in test_sourcemap.py.
"""

from __future__ import annotations

from textcast.web.wordwrap import _wrap_rich, find_word_ranges, word_wrap


def test_find_word_ranges_locates_each_word_in_order():
    text = "Markets steadied on Thursday."
    ranges = find_word_ranges(text, ["Markets", "steadied", "Thursday."])
    assert ranges == [(0, 7), (8, 16), (20, 29)]


def test_find_word_ranges_handles_a_repeated_word_by_searching_forward():
    text = "the cat sat on the mat"
    ranges = find_word_ranges(text, ["the", "cat", "the", "mat"])
    starts = [r[0] for r in ranges]
    assert starts == [0, 4, 15, 19]


def test_find_word_ranges_returns_none_when_a_word_is_missing():
    # An edit since the last build -- the stored word no longer appears.
    assert find_word_ranges("Markets steadied.", ["Markets", "plummeted"]) is None


def test_find_word_ranges_returns_none_for_an_empty_word():
    assert find_word_ranges("Markets steadied.", ["Markets", ""]) is None


def test_word_wrap_of_plain_text():
    out = word_wrap("Markets steadied on Thursday.", None, ["Markets", "steadied"])
    assert out == (
        '<span class="w" data-w="0">Markets</span> '
        '<span class="w" data-w="1">steadied</span> on Thursday.'
    )


def test_word_wrap_escapes_plain_text_outside_and_inside_spans():
    out = word_wrap("Tom & Jerry said <hi>", None, ["Tom", "<hi>"])
    assert "&amp;" in out
    assert "&lt;hi&gt;" in out
    assert "<hi>" not in out.replace("&lt;hi&gt;", "")


def test_word_wrap_falls_back_when_there_are_no_words():
    assert word_wrap("Plain text.", None, []) == "Plain text."
    assert word_wrap("Plain text.", "<b>Plain</b> text.", []) == "<b>Plain</b> text."


def test_word_wrap_falls_back_when_a_word_no_longer_matches():
    out = word_wrap("Markets steadied.", None, ["Markets", "plummeted"])
    assert out == "Markets steadied."


def test_wrap_rich_leaves_tags_untouched_and_wraps_only_text():
    rich = "It was <b>huge</b> news."
    out = _wrap_rich(rich, find_word_ranges("It was huge news.", ["huge"]))
    assert out == 'It was <b><span class="w" data-w="0">huge</span></b> news.'


def test_wrap_rich_handles_a_word_split_across_two_text_runs_by_a_tag():
    # The word itself is not split by a tag in practice (rich preserves
    # words), but the group can still open in one run and close in the next
    # when the plain text either side of a tag is unrelated -- checked here
    # with a merged range that spans past a tag boundary.
    rich = "The <i>Footnote 1</i>: a citation follows."
    plain = "The Footnote 1: a citation follows."
    ranges = find_word_ranges(plain, ["Footnote 1: a citation"])
    out = _wrap_rich(rich, ranges)
    assert "<span" in out and "</span>" in out
    assert "Footnote 1" in out and "a citation" in out


def test_wrap_rich_decodes_entities_to_track_plain_text_offsets():
    rich = "Tom &amp; Jerry"
    plain = "Tom & Jerry"
    ranges = find_word_ranges(plain, ["&"])
    out = _wrap_rich(rich, ranges)
    assert '<span class="w" data-w="0">&amp;</span>' in out


def test_word_wrap_uses_rich_when_present_and_there_are_words():
    out = word_wrap("It was huge news.", "It was <b>huge</b> news.", ["huge"])
    assert out == 'It was <b><span class="w" data-w="0">huge</span></b> news.'


def test_a_word_is_found_across_a_different_run_of_whitespace():
    """A stored word is a slice of `block.text` and is looked for in the text
    of `block.rich`. The two carry the same words laid out by a different
    pass, and whitespace is where they disagree -- measured over one library,
    16 of the 234 blocks that have `rich` at all."""
    assert find_word_ranges("Commitments \n\nWe have", ["Commitments\n\n", "We"]) == [
        (0, 11), (14, 16)
    ]
    assert find_word_ranges("Further reading: \n— Just", ["reading: —", "Just"]) == [
        (8, 19), (20, 24)
    ]


def test_trailing_whitespace_is_not_part_of_the_highlight():
    """A word whose slice ran to the end of a paragraph would otherwise light
    the blank line after it."""
    (start, end), = find_word_ranges("Commitments\n\nWe have", ["Commitments\n\n"])
    assert (start, end) == (0, 11)


def test_a_word_that_is_really_missing_still_gives_up():
    """Loose whitespace must not become loose everything."""
    assert find_word_ranges("Markets steadied", ["Markets", "collapsed"]) is None
