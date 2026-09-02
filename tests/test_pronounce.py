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
    rules = [Rule(kind="word", pattern="LIBOR", misaki="lˈIbɔɹ")]
    assert apply("the LIBOR rate", rules) == "the [LIBOR](/lˈIbɔɹ/) rate"


def test_phoneme_replacement_tolerates_wrapping_slashes():
    rules = [Rule(kind="word", pattern="X", misaki="/eks/")]
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


def test_a_rule_added_to_a_later_release_reaches_an_old_library(conn, monkeypatch):
    """Skipping whenever anything was stored kept deleted rules deleted, and
    also meant a new built-in never arrived."""
    from textcast import pronounce

    before = len(db.list_pronunciations(conn))
    extra = pronounce.Rule(kind="word", pattern="ZZZTOP", replacement="zee zee top")
    shipped = pronounce.builtin_rules()
    monkeypatch.setattr(pronounce, "builtin_rules", lambda: [*shipped, extra])

    assert db.seed_pronunciations(conn) == 1, "only the new one"
    assert len(db.list_pronunciations(conn)) == before + 1
    assert db.seed_pronunciations(conn) == 0, "and not again on the next start"


def test_a_library_predating_the_record_is_offered_the_built_ins_once_more(conn):
    """The cost of the record, paid once.

    Without it there is no way to tell a rule the user deleted from one they
    have never been shown, so a library that deleted a built-in before the
    record existed gets that one back on the first start after upgrading. The
    baseline is written on the way out, so it cannot happen twice.
    """
    gone = db.list_pronunciations(conn)[0]
    db.delete_pronunciation(gone.id, conn)
    conn.execute("DELETE FROM setting WHERE key = ?", (db.SEEDED_KEY,))

    assert db.seed_pronunciations(conn) == 1, "the deleted rule came back, once"

    db.delete_pronunciation(db.list_pronunciations(conn)[0].id, conn)
    after = len(db.list_pronunciations(conn))
    assert db.seed_pronunciations(conn) == 0, "and never again"
    assert len(db.list_pronunciations(conn)) == after


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


def test_the_dotted_and_plain_spellings_of_ai_are_read_alike():
    """Measured against Kokoro: AI gives ˈAˌI, already "ay-eye", while A.I.
    gives ˌAˈI and A.I.s gives ˌAˌIˈɛs — "ay-eye-ESS"."""
    rules = builtin_rules()

    assert apply("About A.I. today.", rules) == "About AI today."
    assert apply("About A.I today.", rules) == "About AI today."
    assert apply("The A.I.s are coming.", rules) == "The AIs are coming."
    assert apply("Plain AI here.", rules) == "Plain AI here.", "misaki already says this one"


def test_a_dotted_abbreviation_at_the_end_keeps_its_full_stop():
    """The last dot is the abbreviation's and the sentence's. Eating it runs
    two sentences together — the trap the month rules already carry."""
    rules = builtin_rules()

    assert apply("It is all about A.I.", rules) == "It is all about AI."
    assert apply("We discussed A.I. Then we left.", rules) == "We discussed AI. Then we left."
    assert apply("A.I., in short.", rules) == "AI, in short."


def test_ai_is_not_spelled_out_by_a_rule():
    """The rule turned it into "A I", which only put a word break between two
    letters the phonemiser already reads as one token."""
    from textcast.pronounce import SPELL_OUT

    assert "AI" not in SPELL_OUT
    assert apply("Investing in AI.", builtin_rules()) == "Investing in AI."


def test_only_the_initialisms_the_phonemiser_gets_wrong_have_rules():
    """Measured against Kokoro: 41 of the 45 spell-out rules produced exactly
    the same sounds as leaving the word alone, and cost word breaks and a
    stressed syllable per letter — "C E O" against misaki's own sˌiˌiˈO."""
    from textcast.pronounce import SPELL_OUT

    rules = builtin_rules()
    for already_right in ("CEO", "SEC", "ETF", "GDP", "M&A", "US", "ARR", "TAM"):
        assert already_right not in SPELL_OUT
        assert apply(f"The {already_right} today.", rules) == f"The {already_right} today."

    # The ones kept are where misaki says a real word instead of the letters.
    assert apply("Return on equity, or ROE, fell.", rules) == "Return on equity, or R O E, fell."


# -- one sound, two notations ---------------------------------------------


def test_a_phoneme_rule_carries_a_spelling_for_each_phonemiser():
    """misaki's notation is not IPA — its capital I is the /aɪ/ of "eye" —
    and espeak reads that as the letter. One sound, two spellings."""
    rule = Rule(
        kind="word", pattern="LIBOR", misaki="lˈIbɔɹ", espeak="lˈaɪbɔːɹ",
    )

    assert rule.substitution("misaki") == r"[\g<0>](/lˈIbɔɹ/)"
    assert rule.substitution("espeak") == r"[\g<0>](/lˈaɪbɔːɹ/)"


def test_a_phoneme_rule_with_nothing_for_this_engine_does_not_fire():
    """Handing misaki's markup to espeak made it read the notation aloud —
    "libber slash el stress eye bee open-or turned-ar slash"."""
    rule = Rule(kind="word", pattern="LIBOR", misaki="lˈIbɔɹ")

    assert rule.fires_for("misaki") is True
    assert rule.fires_for("espeak") is False
    assert apply("The LIBOR rate", [rule], g2p="espeak") == "The LIBOR rate"
    assert "[LIBOR]" in apply("The LIBOR rate", [rule], g2p="misaki")


def test_an_engine_that_takes_no_phonemes_gets_only_the_other_rules():
    """The safety net for an engine that understands no phoneme markup at all:
    every IPA rule is dropped and every respelling still lands."""
    ipa = Rule(kind="word", pattern="LIBOR", misaki="lˈIbɔɹ", espeak="lˈaɪbɔːɹ")
    respelling = Rule(kind="word", pattern="GAAP", replacement="gap")

    out = apply("GAAP and LIBOR", [respelling, ipa], g2p="misaki", phonemes=False)

    assert out == "gap and LIBOR", "the respelling fired, the phonemes did not"
    assert "[LIBOR]" not in out


def test_a_respelling_is_the_same_rule_for_every_engine():
    """Only a phoneme rule is written in a notation. This is why respellings
    are the first choice and phonemes the last."""
    rule = Rule(kind="word", pattern="GAAP", replacement="gap")

    for g2p in ("misaki", "espeak"):
        assert apply("Under GAAP", [rule], g2p=g2p) == "Under gap"
        assert rule.fires_for(g2p, phonemes=False) is True


def test_every_shipped_phoneme_rule_speaks_both_notations():
    """A built-in that only misaki can read is a rule that quietly stops
    working the moment the engine is switched."""
    from textcast.pronounce import builtin_rules

    phoneme_rules = [r for r in builtin_rules() if r.is_phonemes]

    assert phoneme_rules, "there is at least one, and it is LIBOR"
    for rule in phoneme_rules:
        assert rule.espeak, f"{rule.pattern} has no espeak spelling"
        # espeak writes diphthongs out; misaki's capitals would be read as
        # letters, so finding one here means the notations were swapped.
        assert not set(rule.espeak) & set("AIOWY"), rule.pattern


# -- names and words both engines got wrong -------------------------------


def test_a_company_suffix_is_an_abbreviation_not_a_full_stop(conn):
    """Both engines kept the stop and paused in the middle of the man's title:
    "Goldman Sachs Group Inc. [pause] CEO David Solomon"."""
    out = normalize("Goldman Sachs Group Inc. CEO David Solomon. Bank of America Corp. said so.")

    assert "Inc CEO" in out
    assert "Corp said" in out


def test_a_suffix_that_ends_the_line_keeps_its_stop(conn):
    """There the stop is doing its ordinary job."""
    assert normalize("The firm is Goldman Sachs Group Inc.").endswith("Inc.")


def test_the_stylistic_y_is_spelled_out(conn):
    """crypto-y was kɹˈɪptˌOwˌI on both — "crypto-why"."""
    out = normalize("a crypto-y company at meme-y prices, in computer-y ways")

    assert "crypto-ee" in out
    assert "meme-ee" in out
    assert "computer-ee" in out


def test_the_y_rule_needs_a_word_in_front_of_it(conn):
    """A lone hyphen and a y is not the construction."""
    assert "-ee" not in normalize("the x-y axis and a-y")


def test_a_two_word_name_with_one_stress_is_joined(conn):
    """"Wall Street" took a primary stress on each word and a gap between."""
    assert "Wallstreet Journal" in normalize("The Wall Street Journal")


def test_the_names_both_engines_mispronounced_are_respelled(conn):
    out = normalize("Kleinman Parker, Bear Stearns and David Solomon on acquisitions")

    assert "Klineman" in out
    assert "Sterns" in out
    assert "Solamon" in out
    assert "ackwizitions" in out


def test_openai_gets_its_word_boundary_back(conn):
    """misaki ran it together as ˌOpᵊnˈAˌI."""
    assert "Open AI" in normalize("SpaceX and OpenAI remain private")


def test_a_rule_can_carry_a_respelling_and_phonemes_at_once():
    """The respelling is the fallback for any phonemiser the rule says
    nothing to, so one rule can cover every engine."""
    rule = Rule(
        kind="word", pattern="LIBOR", replacement="lye bore",
        misaki="lˈIbɔɹ",
    )

    assert apply("the LIBOR rate", [rule], g2p="misaki") == "the [LIBOR](/lˈIbɔɹ/) rate"
    assert apply("the LIBOR rate", [rule], g2p="espeak") == "the lye bore rate"
    # An engine that takes no phonemes falls back the same way.
    assert apply("the LIBOR rate", [rule], g2p="misaki", phonemes=False) == "the lye bore rate"


def test_every_replacement_is_optional_but_not_all_of_them(conn):
    from textcast import db

    db.add_pronunciation("word", "OnlyEspeak", "", conn, espeak="ˈoʊnli")
    db.add_pronunciation("word", "OnlyText", "plain", conn)

    with pytest.raises(ValueError, match="something to say"):
        db.add_pronunciation("word", "Nothing", "", conn)


def test_the_ipa_flag_follows_the_fields(conn):
    """It used to be a checkbox that could disagree with them."""
    from textcast import db

    assert Rule(kind="word", pattern="a", replacement="b").is_phonemes is False
    assert Rule(kind="word", pattern="a", espeak="ˈbiː").is_phonemes is True

    db.add_pronunciation("word", "Spoken", "", conn, misaki="spˈOkən")
    row = conn.execute("SELECT * FROM pronunciation WHERE pattern = 'Spoken'").fetchone()
    assert row["is_phonemes"] == 1
    assert row["replacement_misaki"] == "spˈOkən"
    assert row["replacement"] == ""
