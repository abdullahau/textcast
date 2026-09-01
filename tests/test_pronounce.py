"""User-editable pronunciation rules."""

from __future__ import annotations

import pytest

from textcast import db
from textcast.normalize import normalize
from textcast.pronounce import Rule, apply, builtin_rules, month_pattern, preview


def say(text: str) -> str:
    return normalize(text, rules=builtin_rules())


# --------------------------------------------------------------------------
# rule kinds
# --------------------------------------------------------------------------


def test_a_word_rule_does_not_fire_inside_a_longer_word():
    rules = [Rule(kind="word", pattern="US", replacement="U S")]
    assert apply("the US economy", rules) == "the U S economy"
    assert apply("we must USE it", rules) == "we must USE it"


def test_a_phrase_rule_matches_anywhere():
    """Needed for anything with a full stop, which breaks word boundaries."""
    rules = [Rule(kind="phrase", pattern="vs.", replacement="versus")]
    assert apply("red vs. blue", rules) == "red versus blue"


def test_a_regex_rule_can_use_lookarounds():
    rules = [Rule(kind="regex", pattern=r"(?<=\d )k\b", replacement="thousand")]
    assert apply("50 k people", rules) == "50 thousand people"
    assert apply("k is a letter", rules) == "k is a letter"


def test_ignore_case_is_opt_in():
    plain = [Rule(kind="word", pattern="Jul", replacement="July")]
    loose = [Rule(kind="word", pattern="Jul", replacement="July", ignore_case=True)]
    assert apply("jul", plain) == "jul"
    assert apply("jul", loose) == "July"


def test_a_broken_pattern_is_skipped_not_fatal():
    """A bad rule must never be what stops a build."""
    rules = [
        Rule(kind="regex", pattern="([unclosed", replacement="x"),
        Rule(kind="word", pattern="fine", replacement="good"),
    ]
    assert apply("this is fine", rules) == "this is good"


def test_phoneme_rules_wrap_the_match_for_misaki():
    rules = [Rule(kind="word", pattern="LIBOR", replacement="lˈIbɔɹ", is_phonemes=True)]
    assert apply("the LIBOR rate", rules) == "the [LIBOR](/lˈIbɔɹ/) rate"


def test_phoneme_replacement_tolerates_wrapping_slashes():
    rules = [Rule(kind="word", pattern="X", replacement="/eks/", is_phonemes=True)]
    assert apply("X", rules) == "[X](/eks/)"


# --------------------------------------------------------------------------
# months — the scoping the whole feature turns on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Published Jul 2 2025.", "Published July 2 2025."),
        ("Due Jul. 2 2025.", "Due July 2 2025."),
        ("He left on 2 Jul.", "He left on 2 July."),
        ("It ran 2 Jul to 4 Aug.", "It ran 2 July to 4 August."),
        ("Filed Dec 31.", "Filed December 31."),
    ],
)
def test_a_month_next_to_a_number_is_a_date(written, spoken):
    assert say(written) == spoken


@pytest.mark.parametrize(
    "text",
    [
        "Marge in March saw Aug.",       # no number, so not a date
        "Julian and Augustus argued.",   # a month hiding inside a name
        "Jan is my colleague.",
        "We may go in March.",
    ],
)
def test_a_month_away_from_a_number_is_left_alone(text):
    assert say(text) == text


def test_month_pattern_covers_both_orders():
    pattern = month_pattern("Jul")
    assert "(?=\\s*\\d)" in pattern, "a number after"
    assert "(?<=\\d\\s)" in pattern, "a number before"


# --------------------------------------------------------------------------
# the shipped set
# --------------------------------------------------------------------------


def test_builtin_patterns_are_unique_per_kind():
    """A duplicate would silently overwrite the earlier rule when seeded.

    REIT was in both the respelling list and the spell-out list, and the
    spell-out won, so REIT came out as "R E I T".
    """
    keys = [(r.kind, r.pattern) for r in builtin_rules()]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("GAAP", "gap"),
        ("EBITDA", "ee bitda"),
        ("REIT", "reet"),
        ("WACC", "whack"),
        ("CAGR", "kaygur"),
        ("MOIC", "moyck"),
        ("CUSIP", "queue sip"),
    ],
)
def test_acronyms_said_as_words_are_respelled(written, spoken):
    assert say(written) == spoken


def test_almost_everything_ships_as_letters_not_ipa():
    """IPA is hard to write, so it is the exception, not the default."""
    rules = builtin_rules()
    ipa = [r for r in rules if r.is_phonemes]
    assert len(ipa) <= 2, "respell where a respelling works"
    assert {r.pattern for r in ipa} == {"LIBOR"}


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------


def test_preview_reports_rules_in_the_order_they_actually_apply():
    """A later rule must not be reported when an earlier one consumed the text.

    "vs." and "vs" both matched the original string, so both looked live.
    """
    rules = [
        Rule(kind="phrase", pattern="vs.", replacement="versus"),
        Rule(kind="word", pattern="vs", replacement="versus"),
    ]
    fired = preview("red vs. blue", rules)
    assert [r.pattern for r, _ in fired] == ["vs."]


def test_preview_returns_what_each_rule_matched():
    rules = [Rule(kind="word", pattern="SEC", replacement="S E C")]
    fired = preview("the SEC and the SEC again", rules)
    assert fired[0][1] == ["SEC", "SEC"]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def test_seeding_happens_once(conn):
    """Re-seeding would resurrect rules the user deliberately deleted."""
    first = len(db.list_pronunciations(conn))
    assert first > 50

    db.delete_pronunciation(db.list_pronunciations(conn)[0].id, conn)
    assert db.seed_pronunciations(conn) == 0
    assert len(db.list_pronunciations(conn)) == first - 1


def test_adding_a_rule_takes_effect_immediately(conn):
    assert normalize("Hodlers gonna FOOBAR") == "Hodlers gonna FOOBAR"
    db.add_pronunciation("word", "FOOBAR", "foo bar", conn)
    assert normalize("Hodlers gonna FOOBAR") == "Hodlers gonna foo bar"


def test_disabling_a_rule_takes_effect_immediately(conn):
    rule = next(r for r in db.list_pronunciations(conn) if r.pattern == "GAAP")
    assert normalize("GAAP") == "gap"

    db.update_pronunciation(rule.id, conn, enabled=0)
    assert normalize("GAAP") == "GAAP"

    db.update_pronunciation(rule.id, conn, enabled=1)
    assert normalize("GAAP") == "gap"


def test_an_invalid_pattern_is_refused_at_the_door(conn):
    with pytest.raises(ValueError, match="not a valid regular expression"):
        db.add_pronunciation("regex", "([unclosed", "x", conn)
    with pytest.raises(ValueError, match="needs something to match"):
        db.add_pronunciation("word", "   ", "x", conn)
    with pytest.raises(ValueError, match="kind must be"):
        db.add_pronunciation("nonsense", "a", "b", conn)


def test_re_adding_a_pattern_updates_it(conn):
    db.add_pronunciation("word", "ZZZ", "zed zed zed", conn)
    db.add_pronunciation("word", "ZZZ", "triple zed", conn)
    matches = [r for r in db.list_pronunciations(conn) if r.pattern == "ZZZ"]
    assert len(matches) == 1
    assert matches[0].replacement == "triple zed"


def test_a_hyphen_that_reads_as_a_pause_is_joined_up():
    """Kokoro breaks on the hyphen: measured 182 ms against 113 ms joined."""
    rules = builtin_rules()

    assert apply("Funding start-ups, and one start-up in particular.", rules) == (
        "Funding startups, and one startup in particular."
    )
    # Whatever the capitals were, the spoken form is the same word.
    assert apply("A Start-Up raised money.", rules) == "A startup raised money."
    # It must not reach inside an unrelated word.
    assert apply("They restart-upgrade nightly.", rules) == "They restart-upgrade nightly."
