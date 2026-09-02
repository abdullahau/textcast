"""The speech normaliser.

Cases are drawn from the kind of writing this app actually reads: money with
scale suffixes, basis points, quarters, initialisms.
"""

from __future__ import annotations

import pytest

from textcast.document import Block, BlockKind
from textcast.normalize import normalize


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        # Money with a scale suffix, which is where engines fail hardest.
        ("a $72mm round", "a 72 million dollars round"),
        ("the fund is $5bn", "the fund is 5 billion dollars"),
        ("raised £5bn", "raised 5 billion pounds"),
        ("€300k of it", "300 thousand euros of it"),
        ("US$1.2tn of assets", "1.2 trillion dollars of assets"),
        ("HK$40mm", "40 million Hong Kong dollars"),
        # An already-spelled scale must reorder, not strand the currency.
        ("about $19 million of cash", "about 19 million dollars of cash"),
        # Singular where it matters.
        ("worth $1 today", "worth 1 dollar today"),
        # Commas are removed so the number is read as one figure.
        ("$1,250 each", "1250 dollars each"),
        # Finance shorthand.
        ("up 150bps", "up 150 basis points"),
        ("trading at 12x", "trading at 12 times"),
        ("fell 2.5%", "fell 2.5 percent"),
        ("Q3 was fine", "quarter 3 was fine"),
        ("FY2024 guidance", "fiscal year 2024 guidance"),
        ("revenue 2019-21 grew", "revenue 2019 to 2021 grew"),
        ("72mm shares outstanding", "72 million shares outstanding"),
        # Abbreviations people write but do not say.
        ("gains vs. losses", "gains versus losses"),
        ("approx. half", "approximately half"),
        ("grew YoY", "grew year over year"),
    ],
)
def test_rewrites(conn, written: str, spoken: str):
    """The structural rewrites, plus the abbreviations that come from rules.

    ``conn`` is not decoration: "vs.", "approx." and "YoY" are seeded
    pronunciation rules, so they are read from the database. Without the
    fixture this passed only when some earlier test had left a seeded one
    behind, and failed whenever the order changed.
    """
    assert normalize(written) == spoken


def test_initialisms_are_spelled_and_acronyms_respelled():
    """Word-level behaviour comes from the pronunciation rules.

    Most initialisms have no rule at all any more: misaki spells an acronym
    out by itself, and better — measured, "CEO" alone is sˌiˌiˈO against the
    rule's sˈi ˈi ˈO, every letter its own stressed word.
    """
    from textcast import pronounce

    rules = pronounce.builtin_rules()
    say = lambda t: normalize(t, rules=rules)  # noqa: E731

    assert say("The SEC told the CEO") == "The SEC told the CEO"
    assert say("the S&P 500") == "the S&P 500"
    assert say("the M&A team") == "the M&A team"

    # Acronyms said as words get a respelling, not a spelled-out form.
    assert say("GAAP rules") == "gap rules"
    assert say("EBITDA margin") == "ee bitda margin"


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        # All six are real blocks from the library.
        ("sold the stock short *before* they agreed", "sold the stock short before they agreed"),
        ("how quickly Strategy can *lower* the rate", "how quickly Strategy can lower the rate"),
        ("one quite speculative but *possible* reading", "one quite speculative but possible reading"),
        ("ETFs do not sell *appreciated* stocks", "ETFs do not sell appreciated stocks"),
        ("confidence in *you*.]", "confidence in you.]"),
        # And the rest of the family.
        ("this is **bold**", "this is bold"),
        ("this is ~~gone~~", "this is gone"),
        ("this is __also bold__", "this is also bold"),
        # A marker must not hide a rewrite behind it.
        ("worth *$5bn* today", "worth 5 billion dollars today"),
    ],
)
def test_emphasis_markers_are_not_spoken(written: str, spoken: str):
    """The engine read a stray asterisk aloud as the word "asterisk".

    Matt Levine writes *before* for italics and the newsletter carries the
    characters straight through, so nothing had stripped them: the Markdown
    reader does it at parse time, and these arrive as HTML.
    """
    assert normalize(written, rules=[]) == spoken


@pytest.mark.parametrize(
    "text",
    ["See the footnote marker * at the end.", "A lone * and nothing else.", "2 * 3 = 6"],
)
def test_a_lone_asterisk_is_left_alone(text: str):
    """It is a footnote marker or a bullet, not emphasis. Only pairs are stripped."""
    assert normalize(text, rules=[]) == text


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("a call on Monday at 8:00am", "a call on Monday at 8 a.m."),
        ("at 8pm.", "at 8 p.m."),
        ("meeting at 8:00 in the morning", "meeting at 8 in the morning"),
        # Already right, and left alone: "ten forty seven".
        ("filed at 10:47 on Friday", "filed at 10:47 on Friday"),
        ("the call is at 4 p.m. sharp", "the call is at 4 p.m. sharp"),
    ],
)
def test_a_time_on_the_hour_loses_its_zero_minutes(written: str, spoken: str):
    """8:00am was "eight zero zero a m": espeak reads the zeroes, and without
    the space "8am" is "eight" and then "am" the verb."""
    assert normalize(written, rules=[]) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("his 401(k) plan", "his four oh one k plan"),
        ("INmune Bio said", "InMune Bio said"),
        # The possessive too, which a word rule would miss: its trailing
        # guard refuses to match before an apostrophe.
        ("INMune's stock rose", "InMune's stock rose"),
        ("INmune, a biotech,", "InMune, a biotech,"),
    ],
)
def test_written_forms_the_phonemiser_reads_wrongly(written: str, spoken: str):
    """Measured against Kokoro, as the built-ins require.

    "401(k)" came out "four hundred one k". "INmune" came out "I EN-mune",
    while the company's own possessive already came out right — so the
    respelling only has to make every form take that path.
    """
    from textcast import pronounce

    assert normalize(written, rules=pronounce.builtin_rules()) == spoken


def test_smart_punctuation_is_flattened():
    assert "—" not in normalize("a thought — an aside — the end")
    assert normalize("“quoted”") == '"quoted"'
    assert normalize("it’s") == "it's"


def test_a_footnote_gets_a_pause_before_it():
    out = normalize("a claim [Footnote 3: the caveat] and on we go")
    assert out.startswith("a claim ...")
    assert "Footnote 3." in out


def test_normalisation_is_idempotent():
    once = normalize("Thrive led a $72mm round at 12x, per the SEC.")
    assert normalize(once) == once


def test_empty_input_is_safe():
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_display_text_is_never_touched():
    """The page keeps the author's punctuation; only the engine sees the rewrite."""
    block = Block(kind=BlockKind.PARA, text="A $72mm round — per the SEC.")
    assert block.text == "A $72mm round — per the SEC."
    assert block.spoken() == "A 72 million dollars round, per the SEC."


def test_quotes_are_normalised_and_still_get_their_cue():
    block = Block(kind=BlockKind.QUOTE, text="We raised $5bn.")
    assert block.spoken() == "Start quote. We raised 5 billion dollars. End quote."
    assert block.spoken(quote_markers=False) == "We raised 5 billion dollars."


def test_curly_apostrophes_are_flattened_before_the_rules_run():
    """Web prose is full of them, and a rule written for who'll would never
    have matched who’ll."""
    from textcast.normalize import normalize
    from textcast.pronounce import builtin_rules

    rules = builtin_rules()
    assert normalize("The people who’ll pay.", rules=rules) == "The people hool pay."
    assert normalize("Who’ll pay?", rules=rules) == "hool pay?"
    assert normalize("It’s a “quote” — really.", rules=rules) == 'It\'s a "quote", really.'
