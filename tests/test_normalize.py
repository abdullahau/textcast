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
        # Singular where it matters, including a multi-word currency.
        ("worth $1 today", "worth 1 dollar today"),
        ("worth A$1 today", "worth 1 Australian dollar today"),
        ("worth C$1 today", "worth 1 Canadian dollar today"),
        ("worth HK$1 today", "worth 1 Hong Kong dollar today"),
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
        # Units of measure, glued to the number only -- "10 in total" is not
        # ten inches, and requiring no space is what keeps it that way.
        ("a 300MW turbine", "a 300 megawatts turbine"),
        ("a 4GB file", "a 4 gigabytes file"),
        ("a 4MBytes cache", "a 4 megabytes cache"),
        ("weighs 200kg", "weighs 200 kilograms"),
        ("just 1kg of it", "just 1 kilogram of it"),
        ("10cm by 5in", "10 centimeters by 5 inches"),
        ("a 100Mbps line", "a 100 megabits per second line"),
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
        ("the stock is BofA", "the stock is Bank of America"),
        ("carried by Ymobile", "carried by Y Mobile"),
        ("under Rule 144A", "under Rule one forty four A"),
        ("TL;DR it failed", "too long, didn't read it failed"),
        ("TLDR it failed", "too long, didn't read it failed"),
        ("her lawyer said", "her law yer said"),
        ("the lawyers said", "the law yers said"),
        ("Enron collapsed", "En ron collapsed"),
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


def test_mm_stays_the_accounting_scale_not_a_length():
    """This app's own corpus writes "72mm shares" far more than it ever
    writes a length, so the ambiguous ones stay million, not millimetre."""
    assert normalize("a 4mm gap") == "a 4 million gap"


def test_a_bare_g_is_left_alone_for_5g_the_network():
    """"5g" the wireless generation and "5g" the mass are the same string;
    a unit rule that expanded it would rewrite every mobile network in the
    library into a mass measurement."""
    assert normalize("a 5g network") == "a 5g network"


def test_smart_punctuation_is_flattened():
    assert "—" not in normalize("a thought — an aside — the end")
    assert normalize("“quoted”") == '"quoted"'
    assert normalize("it’s") == "it's"


def test_a_footnote_gets_a_pause_before_it():
    out = normalize("a claim [Footnote 3: the caveat] and on we go")
    assert out.startswith("a claim ...")
    assert "Footnote 3." in out


def test_a_footnote_citing_a_bracket_of_its_own_is_not_cut_short():
    """A non-greedy close at the citation's own `]` left the rest of the
    body -- "for detail]" here -- as ordinary text with a stray `]` in it,
    instead of inside the footnote's own aside."""
    out = normalize("a claim [Footnote 3: see note [2] for detail] and on we go")
    assert "] and on we go" not in out, "the footnote's own body was cut short"
    assert "for detail" in out


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("World War II began", "World War 2 began"),
        ("Elizabeth II died", "Elizabeth 2 died"),
        ("Henry VIII had six wives", "Henry 8 had six wives"),
        ("Super Bowl LIX was in 2025", "Super Bowl 59 was in 2025"),
        ("Richard III is a play", "Richard 3 is a play"),
        ("Season IX just dropped", "Season 9 just dropped"),
    ],
)
def test_a_roman_numeral_becomes_a_digit(written: str, spoken: str):
    """Kokoro's own g2p already recognises one of these and says so out
    loud -- "Roman number 2" for "II" -- so the digit has to reach it
    first."""
    assert normalize(written) == spoken


@pytest.mark.parametrize("written", [
    "Chapter I is the introduction",  # bare I: the pronoun, far more often
    "He works at Detroit MI today",  # a state code that round-trips too
    "The event is in Washington DC",
    "Watch LIV Golf this weekend",  # a brand, not the numeral 54
    "The MIX is a popular station",  # a word, not the numeral 1009
    "St Croix VI is beautiful",
    "I think this is fine",
])
def test_a_roman_looking_word_is_left_alone(written: str):
    assert normalize(written) == written


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("In 1931 he was born", "In nineteen thirty-one he was born"),
        ("The year 1900 began well", "The year nineteen hundred began well"),
        ("By 1999 it was over", "By nineteen ninety-nine it was over"),
        ("In 1600 it happened", "In sixteen hundred it happened"),
        ("In 2000 it changed", "In two thousand it changed"),
        ("In 2024 it happened", "In twenty twenty-four it happened"),
    ],
)
def test_a_bare_year_is_read_in_pairs_on_espeak(written: str, spoken: str):
    """misaki already reads "1931" as "nineteen thirty-one" unassisted;
    espeak reads the same digits as "nineteen hundred thirty one" instead,
    which is what this rewrite exists to fix -- for espeak only."""
    assert normalize(written, g2p="espeak") == spoken
    assert normalize(written) == written, "misaki needs no help and gets none"


def test_a_year_range_is_paired_on_espeak_after_the_range_itself_is_spelled_out():
    """YEAR_RANGE turns the hyphen into "to" for every engine; espeak's own
    pairing runs after, and on both halves."""
    assert (
        normalize("From 2019-21 it grew", g2p="espeak")
        == "From twenty nineteen to twenty twenty-one it grew"
    )
    assert normalize("From 2019-21 it grew") == "From 2019 to 2021 it grew"


@pytest.mark.parametrize("written", [
    "It cost $1931 that year",  # money, not a year -- MONEY handles the $
    "It weighed 1931kg",  # a count glued to a unit, not a year
    "The 1930s were hard",  # a decade, not a single year
])
def test_a_year_shaped_number_that_is_not_a_year_is_left_to_its_own_rule(written: str):
    """The guard is the character glued to either side, not the four digits
    alone: a currency symbol or a unit means something else is reading it."""
    assert "thirty-one" not in normalize(written, g2p="espeak")
    assert "nineteen" not in normalize(written, g2p="espeak")


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
