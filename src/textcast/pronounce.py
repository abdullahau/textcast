"""User-editable pronunciation rules.

Each rule rewrites text on its way to the engine. Three kinds of fix:

* **Words.** "Jul" becomes "July", "vs." becomes "versus".
* **Respellings.** "GAAP" becomes "gap". The first choice for an acronym said
  as a word: anyone can read and edit it, and it needs no phonetic alphabet.
* **Phonemes.** A last resort. IPA, wrapped as ``[word](/ipa/)``, handed to
  the model verbatim.

So a rule has up to three replacements, and all three are optional:

``replacement``  plain text; reaches every engine, because the engine never
                 knows a rule ran.
``misaki``       IPA for engines phonemised by misaki: ``kokoro``.
``espeak``       IPA for engines phonemised by espeak: ``kokoro-onnx``.

Two IPA fields, not one, because the notations differ: misaki's capital ``A``
is the /eɪ/ of "day" and ``I`` the /aɪ/ of "eye", which espeak writes out and
reads as letters. Each engine takes the IPA for its own phonemiser if there is
any and the plain replacement otherwise, so one rule can serve everything, one
phonemiser, or both. A rule with neither does not fire there at all.

Rules live in the database, so they are edited on the settings page, and are
cached in the worker because a build applies them thousands of times.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger("textcast.pronounce")

KINDS = ("word", "phrase", "regex")

#: The two grapheme-to-phoneme paths an engine can take. Every engine declares
#: which one it is, and a phoneme rule is written for one, the other, or both.
G2P = ("misaki", "espeak")
DEFAULT_G2P = "misaki"


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str
    #: Plain text, for any engine. Empty is allowed, when the rule speaks only
    #: in phonemes.
    replacement: str = ""
    #: The same word in IPA, one field per phonemiser. Both optional.
    misaki: str = ""
    espeak: str = ""
    ignore_case: bool = False
    note: str = ""
    sort_order: int = 100
    id: int | None = None

    @property
    def is_phonemes(self) -> bool:
        """Whether this rule speaks in phonemes to anything.

        Derived rather than stored: the field you fill is what decides, and a
        flag that could disagree with the fields is a flag that will.
        """
        return bool(self.misaki or self.espeak)

    def phonemes_for(self, g2p: str = DEFAULT_G2P) -> str:
        """The IPA this rule offers that phonemiser, or an empty string."""
        source = self.espeak if g2p == "espeak" else self.misaki
        return source.strip().strip("/")

    def says_anything(self) -> bool:
        """A rule with all three replacements empty is not a rule."""
        return bool(self.replacement.strip() or self.misaki.strip() or self.espeak.strip())

    def compile(self) -> re.Pattern | None:
        """Build the matching pattern, or None when it cannot be compiled."""
        return _compiled(self.kind, self.pattern, self.ignore_case)

    def substitution(self, g2p: str = DEFAULT_G2P, phonemes: bool = True) -> str:
        r"""What to put in place of a match, for this engine.

        The IPA written for the engine's own phonemiser if there is any, and
        the plain replacement otherwise. Phonemes keep the original text
        visible to the tokeniser and go in the link target, so ``\g<0>``
        carries the match through.
        """
        ipa = self.phonemes_for(g2p) if phonemes else ""
        return rf"[\g<0>](/{ipa}/)" if ipa else self.replacement

    def fires_for(self, g2p: str = DEFAULT_G2P, phonemes: bool = True) -> bool:
        """Whether this rule has anything to say to that engine."""
        if phonemes and self.phonemes_for(g2p):
            return True
        return bool(self.replacement)


@lru_cache(maxsize=2048)
def _compiled(kind: str, pattern: str, ignore_case: bool) -> re.Pattern | None:
    """Compile once per rule, not once per block.

    A build applies every rule to every block, so recompiling here dominated
    the cost of normalising a long article.
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        if kind == "regex":
            return re.compile(pattern, flags)
        if kind == "word":
            # A word rule should not fire inside a longer word.
            return re.compile(rf"(?<![\w']){re.escape(pattern)}(?![\w'])", flags)
        return re.compile(re.escape(pattern), flags)
    except re.error as exc:
        log.warning("skipping rule %r: %s", pattern, exc)
        return None


def apply(
    text: str,
    rules: list[Rule],
    g2p: str = DEFAULT_G2P,
    phonemes: bool = True,
) -> str:
    """Rewrite the text for one engine.

    ``g2p`` names the engine's phonemiser and ``phonemes`` says whether it can
    take injected phonemes at all. A phoneme rule that has nothing for this
    engine is skipped outright rather than substituted with itself, so it
    cannot disturb a later rule's match.
    """
    for rule in rules:
        if not rule.fires_for(g2p, phonemes):
            continue
        pattern = rule.compile()
        if pattern is None:
            continue
        try:
            text = pattern.sub(rule.substitution(g2p, phonemes), text)
        except re.error as exc:
            log.warning("rule %r failed to substitute: %s", rule.pattern, exc)
    return text


def preview(
    text: str,
    rules: list[Rule],
    g2p: str = DEFAULT_G2P,
    phonemes: bool = True,
) -> list[tuple[Rule, list[str]]]:
    """Which rules fire, and what each one matched.

    Applied in order against the running result, not all against the original.
    Otherwise "vs." and "vs" both look like they fire, when the first has
    already consumed the text and the second never runs.
    """
    hits: list[tuple[Rule, list[str]]] = []
    for rule in rules:
        if not rule.fires_for(g2p, phonemes):
            continue
        pattern = rule.compile()
        if pattern is None:
            continue
        found = [m.group(0) for m in pattern.finditer(text)]
        if found:
            hits.append((rule, found))
            try:
                text = pattern.sub(rule.substitution(g2p, phonemes), text)
            except re.error:
                continue
    return hits


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

_lock = threading.Lock()
_cache: list[Rule] | None = None


def active() -> list[Rule]:
    """Enabled rules, in order, read once and kept."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            from . import db

            _cache = db.list_pronunciations(enabled_only=True)
        except Exception:
            # Normalisation must never be what breaks a build.
            log.debug("no pronunciation rules available", exc_info=True)
            _cache = []
        return _cache


def invalidate() -> None:
    """Call after any edit, so the next build picks the change up."""
    global _cache
    with _lock:
        _cache = None


# --------------------------------------------------------------------------
# what ships out of the box
# --------------------------------------------------------------------------

MONTHS = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "Jun": "June", "Jul": "July", "Aug": "August", "Sep": "September",
    "Sept": "September", "Oct": "October", "Nov": "November", "Dec": "December",
}

#: A month abbreviation only counts as a date next to a number: "Jul 2 2025"
#: or "2 Jul". Without that guard "Mar" renames a person and "Aug" mangles
#: "August" itself, while "May" and "March" are ordinary words year-round.
def month_pattern(abbrev: str) -> str:
    """Two shapes: "Jul 2" and "2 Jul".

    The leading form may swallow its abbreviation dot, because a digit follows
    it. The trailing form may not: in "He left on 2 Jul." that dot ends the
    sentence, and eating it runs two sentences together.
    """
    leading = rf"(?<![A-Za-z]){abbrev}\.?(?![A-Za-z])(?=\s*\d)"
    trailing = rf"(?<=\d\s){abbrev}(?![A-Za-z])"
    return f"{leading}|{trailing}"


#: Acronyms said as words. Respellings, not IPA: anyone can edit them. Each
#: was checked against Kokoro; the comment gives what it says without the rule.
SAY_AS_WORD = {
    # Broken without a rule.
    "GAAP": "gap",              # G-A-A-P
    "EBITDA": "ee bitda",       # E-B-I-T-D-A
    "EBIT": "ee bit",           # E-B-I-T
    "SOFR": "sofer",            # S-O-F-R
    "FAANG": "fang",            # F-A-A-N-G
    "PIK": "pick",              # P-I-K-A
    "FICO": "fyco",             # F-I-C-O
    "REIT": "reet",             # R-E-I-T
    "WACC": "whack",            # W-A-C-C
    "MOIC": "moyck",            # M-O-I-C
    "CUSIP": "queue sip",       # C-U-S-I-P
    "CAGR": "kaygur",           # C-A-G-R
    "ARPU": "arpoo",            # A-R-P-U
    "ESOP": "ee sop",           # E-S-O-P
    "ROIC": "ro ick",           # R-O-I-C
    # Already correct. Kept as explicit rules so they stay correct if the
    # voice or the model changes, and so the list reads as a full inventory.
    "NASDAQ": "nazdack",
    "SPAC": "spack",
    "NAV": "nav",
    "SaaS": "sass",
    "TIPS": "tips",
    "HODL": "hoddle",
}

#: Phonemes, for the rare word a respelling makes worse. LIBOR is the one:
#: Kokoro already says LIE-bor and every respelling lands somewhere else.
#:
#: Two spellings of one sound, because the notations differ — misaki's capital
#: ``I`` is the /aɪ/ of "eye", which espeak spells out and reads as the letter.
#: Both measured, not converted: espeak's own "lie bore" is ``lˈaɪ bˈɔːɹ``.
#: Left alone it says ``lˈɪbɚ``, "LIB-er", so the rule earns its place there too.
#: A hint may name one phonemiser only. "homogeneous" is one: misaki says
#: hˌOməʤˈiniəs, "ho-muh-JEE-nee-us", which is right, while espeak drops a
#: syllable and gives həmˈoʊdʒniəs — very nearly "homogenous", a different
#: word. Every respelling tried either failed on espeak or moved misaki off a
#: correct answer; "homo geneous" fixed both and inserted a pause. So espeak
#: gets IPA and misaki is left alone, which is what an empty field means.
PHONEME_HINTS = {
    "LIBOR": {"misaki": "lˈIbɔɹ", "espeak": "lˈaɪbɔːɹ"},
    "homogeneous": {"espeak": "hˌoʊmoʊdʒˈiːniəs"},
}

#: Names and words the phonemisers get wrong, respelled. A respelling is
#: ordinary text, so it reaches both engines and had to be measured on both —
#: the useful cases are where the engine that was already right does not move.
#:
#: * **Kleinman** "KLAYN-man" on both → klˈInmən / klˈaɪnmən.
#: * **Stearns** stˈɪɹnz, "STEERNS" → stˈɜɹnz / stˈɜːnz. The surname alone,
#:   so Bear Stearns and a bare Stearns both land.
#: * **Solomon** right for misaki (sˈɑləmən), doubled for espeak (sˈɑːlɑːmən).
#:   "Solamon" leaves misaki alone and fixes espeak.
#: * **accretive** right for misaki (əkɹˈiTɪv), "uh-KRET-iv" for espeak.
#: * **acquisition** an s where the word has a z, on espeak. Both ˌækwɪzˈɪʃən.
#: * **OpenAI** ran together for misaki (ˌOpᵊnˈAˌI); the space restores it.
#: * **Tokenization** "token-ih-ZAY-shun" on both, the verb's vowel reduced
#:   away.
SAY_AS_WRITTEN = {
    "Kleinman": "Klineman",
    "Stearns": "Sterns",
    "Solomon": "Solamon",
    "OpenAI": "Open AI",
}

#: The same, but case-insensitive because they are ordinary words rather than
#: names, and a sentence can start with one.
#: * **culinary** espeak puts a short vowel after the glide: kjˈʊlɪnˌɛɹi,
#:   "kyull-in-air-ee". misaki is already right (kˈʌlənˌɛɹi). "cullinary" gives
#:   kˈʌlɪnˌɛɹi on both, which is misaki's answer in all but the schwa.
#: * **stochastic** espeak says the ch: stətʃˈæstɪk, "stuh-CHAS-tik".
#:   "stokastic" gives stəkˈæstɪk on both — and that is misaki's original,
#:   character for character, which is the case this table exists for.
#: * **sensuous** espeak keeps a bare /sj/: sˈɛnsjuːəs. "senshoous" is
#:   sˈɛnʃuːəs / sˈɛnʃuəs. "senshuous" was measured first and leaves a stray
#:   /j/ behind on both.
SAY_AS_WRITTEN_ANYCASE = {
    "accretive": "accreetive",
    "tokenization": "token-eye-zation",
    "culinary": "cullinary",
    "stochastic": "stokastic",
    "sensuous": "senshoous",
}

#: Names that are one thing with one stress. "Wall Street" is wˈɔl stɹˈit, a
#: primary stress on each; "Wallstreet" is wˈɔlstɹit, which is how it is said.
COMPOUND_NAMES = {
    "Wall Street": "Wallstreet",
}

#: A company suffix is an abbreviation, not a sentence end: both engines
#: paused mid-title in "Goldman Sachs Group Inc. CEO David Solomon". The sound
#: is ˈɪŋk either way. Only when something follows on the same line — at the
#: end of a block the stop is doing its ordinary job.
SUFFIXES = {
    r"\bInc\.(?=\s)": "Inc",
    r"\bCorp\.(?=\s)": "Corp",
    r"\bCos\.(?=\s)": "Cos",
}

#: The stylistic "-y": crypto-y, meme-y, bank-y. Both engines read the y as
#: the letter with a w in front — kɹˈɪptˌOwˌI, "crypto-why". At least two
#: letters before the hyphen, so a stray "-y" is not caught.
STYLISTIC_Y = {
    r"(?<=[A-Za-z]{2})-y(?![\w'])": "-ee",
}

#: Hyphens read as a pause rather than a join. Measured on Kokoro: "fund
#: start-ups that" leaves a 182 ms break against 113 ms for "startups", and
#: runs 80 ms longer. Spelled out rather than captured, so the capitals of the
#: input cannot put the two-word reading back.
JOINED = {
    r"\bstart-ups\b": "startups",
    r"\bstart-up\b": "startup",
}

#: Contractions the phonemiser gets wrong. "she'll" is ʃil and "you'll" jul,
#: one syllable each and right; "who'll" is hˌuəl, "hoo-ULL". Respelled: hˈul.
CONTRACTIONS = {
    "who'll": "hool",
}

#: Written forms the engines get wrong for reasons that are not acronyms.
#:
#: "401(k)" is read "four hundred one k"; "four oh one k" is fˈɔɹ ˈO wˈʌn kˈA.
#: "INmune" is "I EN-mune" — a leading IN looks like an initialism — while the
#: possessive "INMune's" already came out ɪn mjˈunz. "InMune" gives that for
#: one changed capital.
#:
#: All regexes with (?!\w), not word rules: a word rule's (?![\w']) refuses to
#: match before an apostrophe, and would miss every possessive.
RESPELL = {
    r"(?<!\d)401\(k\)": "four oh one k",
    r"(?<!\w)INmune(?!\w)": "InMune",
    # espeak says the s of "acquisition" as an s; the word has a z. A regex
    # rather than two word rules, so the plural comes with it.
    r"(?<!\w)acquisition(s?)(?!\w)": r"ackwizition\1",
    # "Shane" on both: ʃˈAn / ʃˈeɪn. "Sheein" is ʃˈiɪn / ʃˈiːɪn. Case-blind,
    # because the brand writes itself SHEIN — and misaki spells an all-capital
    # SHEEIN out letter by letter.
    r"(?<!\w)Shein(?!\w)": "Sheein",
    # V. S. Naipaul is "NY-pawl" and both engines said "NAY-pawl": nˈeɪpɔːl on
    # espeak, nˈApɔl on misaki, whose capital A is that same /eɪ/. "Nypaul" is
    # nˈaɪpɔːl / nˈIpɔl, right on both. A regex, so Naipaul's comes too.
    r"(?<!\w)Naipaul(?!\w)": "Nypaul",
    # The noun's stress on every form. Not a correction: ɹəfˈʌnd / ɹᵻfˈʌnd is
    # the textbook verb, and this is a house preference. Delete it on the
    # Voice page to go back. The inflections are named rather than left to a
    # bare prefix, because "reefundable" wrecks ɹˈifəndəbᵊl.
    r"(?<!\w)refund(s|ed|ing)?(?!\w)": r"reefund\1",
}

#: Written with dots. "AI" is ˈAˌI, already right; "A.I." is ˌAˈI with the
#: stress reversed, and "A.I.s" is ˌAˌIˈɛs, "ay-eye-ESS". Dropping the dots
#: puts every form through the path misaki gets right.
DOTTED = ["A.I."]


def dotted_patterns(dotted: str) -> list[tuple[str, str]]:
    """Two shapes, because the last dot is doing two jobs.

    Mid-sentence it is only the abbreviation's, and eating it is right. At the
    end of a sentence it is the full stop as well, and eating it runs two
    sentences together — the same trap the month rules have.
    """
    letters = re.escape(dotted.rstrip("."))
    plain = dotted.replace(".", "")
    return [
        (rf"\b{letters}\.(?=\s+[A-Z\u201c\"']|\s*$)", f"{plain}."),
        (rf"\b{letters}\.?", plain),
    ]


#: Written short, said long.
ABBREVIATIONS = {
    "approx.": "approximately",
    "e.g.": "for example",
    "i.e.": "that is",
    "vs.": "versus",
    "vs": "versus",
    "etc.": "et cetera",
    "cf.": "compare",
    "Ltd.": "Limited",
    "Co.": "Company",
    "No.": "Number",
    "Mr.": "Mister",
    "Mrs.": "Missus",
    "Ms.": "Miz",
    "Dr.": "Doctor",
    "St.": "Saint",
    "Jr.": "Junior",
    "Sr.": "Senior",
    "YoY": "year over year",
    "QoQ": "quarter over quarter",
    "MoM": "month over month",
    "bps": "basis points",
    "aka": "also known as",
    "AUM": "assets under management",
    "IPO": "I P O",
}

#: Initialisms misaki reads as a word when it should spell them out. There
#: were forty-five; forty-one sounded identical with the rule and without,
#: because misaki spells acronyms out and does it better — "CEO" is sˌiˌiˈO,
#: while the rule's "C E O" is sˈi ˈi ˈO, 120 ms longer and evenly spaced.
#: That is what made them sound recited. Ask `engine.phonemes()` before adding
#: one. What is left is where misaki says a real word instead.
SPELL_OUT = [
    "ETH", "ROE",
]


def _spelled(token: str) -> str:
    """S&P becomes "S and P"; SEC becomes "S E C"."""
    return " and ".join(" ".join(part) for part in token.split("&") if part)


def builtin_rules() -> list[Rule]:
    """The set seeded on first run. Every one is editable afterwards.

    Patterns are unique per kind in the database, so a word appearing in two
    lists would silently overwrite the earlier rule. The first entry wins here
    instead, and the duplicate is dropped.
    """
    rules: list[Rule] = []
    seen: set[tuple[str, str]] = set()

    def add(rule: Rule) -> None:
        key = (rule.kind, rule.pattern)
        if key in seen:
            log.debug("duplicate builtin rule %s %r, keeping the first", *key)
            return
        seen.add(key)
        rules.append(rule)

    for abbrev, full in MONTHS.items():
        add(Rule(
            kind="regex",
            pattern=month_pattern(abbrev),
            replacement=full,
            note=f"{abbrev} to {full}, only next to a number (Jul 2 2025, 2 Jul)",
            sort_order=20,
        ))

    for token, respelling in SAY_AS_WORD.items():
        add(Rule(
            kind="word",
            pattern=token,
            replacement=respelling,
            note=f"said as a word; Kokoro spells {token} out otherwise",
            sort_order=30,
        ))

    for token, spellings in PHONEME_HINTS.items():
        add(Rule(
            kind="word",
            pattern=token,
            # No plain replacement: this table exists for the words where
            # every respelling was worse than the engine's own guess.
            misaki=spellings.get("misaki", ""),
            espeak=spellings.get("espeak", ""),
            note="phonemes, where no respelling reaches it",
            sort_order=30,
        ))

    for written, said in CONTRACTIONS.items():
        add(Rule(
            kind="word",
            pattern=written,
            replacement=said,
            # Sentence-initial "Who'll" is the same word. The replacement is
            # lower case either way; nothing downstream reads capitals.
            ignore_case=True,
            note="the phonemiser gives this one an extra syllable",
            sort_order=35,
        ))

    for dotted in DOTTED:
        for order, (pattern, plain) in enumerate(dotted_patterns(dotted)):
            add(Rule(
                kind="regex",
                pattern=pattern,
                replacement=plain,
                note=f"{dotted} said the same way as {dotted.replace('.', '')}",
                sort_order=18 + order,
            ))

    for token, respelling in SAY_AS_WRITTEN.items():
        add(Rule(
            kind="word",
            pattern=token,
            replacement=respelling,
            note="both engines read the written form wrongly; measured on both",
            sort_order=26,
        ))

    for token, respelling in SAY_AS_WRITTEN_ANYCASE.items():
        add(Rule(
            kind="word",
            pattern=token,
            replacement=respelling,
            ignore_case=True,
            note="both engines read the written form wrongly; measured on both",
            sort_order=26,
        ))

    for phrase, joined in COMPOUND_NAMES.items():
        add(Rule(
            kind="phrase",
            pattern=phrase,
            replacement=joined,
            note="one name, one stress; two words gave it two and a gap",
            sort_order=26,
        ))

    for pattern, plain in SUFFIXES.items():
        add(Rule(
            kind="regex",
            pattern=pattern,
            replacement=plain,
            note="an abbreviation, not the end of a sentence",
            sort_order=19,
        ))

    for pattern, spelled in STYLISTIC_Y.items():
        add(Rule(
            kind="regex",
            pattern=pattern,
            replacement=spelled,
            note="the stylistic -y: meme-y, crypto-y. Read as the letter otherwise",
            sort_order=27,
        ))

    for pattern, respelling in RESPELL.items():
        add(Rule(
            kind="regex",
            pattern=pattern,
            replacement=respelling,
            ignore_case=True,
            note="the phonemiser reads the written form wrongly",
            sort_order=25,
        ))

    for pattern, joined in JOINED.items():
        add(Rule(
            kind="regex",
            pattern=pattern,
            replacement=joined,
            ignore_case=True,
            note="hyphen read as a pause; joined it is one word",
            sort_order=25,
        ))

    for short, long in ABBREVIATIONS.items():
        add(Rule(
            kind="phrase" if short.endswith(".") else "word",
            pattern=short,
            replacement=long,
            note="abbreviation",
            sort_order=40,
        ))

    for token in SPELL_OUT:
        add(Rule(
            kind="word",
            pattern=token,
            replacement=_spelled(token),
            note="initialism, read letter by letter",
            sort_order=50,
        ))

    return rules
