"""User-editable pronunciation rules.

Each rule rewrites text on its way to the engine. Two flavours:

* **Words.** "Jul" becomes "July", "vs." becomes "versus". The engine then
  pronounces ordinary English.
* **Respellings.** "GAAP" becomes "gap", "EBITDA" becomes "ee bitda". This is
  the first choice for an acronym said as a word: anyone can read and edit it,
  and it needs no phonetic alphabet.
* **Phonemes.** A last resort for a word no respelling reaches. The replacement
  is IPA, wrapped as ``[word](/ipa/)``, which misaki hands to Kokoro verbatim.

Rules live in the database so they can be edited from the settings page, and
they are cached in the worker because a build applies them thousands of times.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger("textcast.pronounce")

KINDS = ("word", "phrase", "regex")


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str
    replacement: str
    is_phonemes: bool = False
    ignore_case: bool = False
    note: str = ""
    sort_order: int = 100
    id: int | None = None

    def compile(self) -> re.Pattern | None:
        """Build the matching pattern, or None when it cannot be compiled."""
        return _compiled(self.kind, self.pattern, self.ignore_case)

    def substitution(self) -> str:
        r"""What to put in place of a match.

        Phoneme rules keep the original text visible to misaki's tokeniser and
        put the IPA in the link target, so ``\g<0>`` carries the match through.
        """
        if self.is_phonemes:
            ipa = self.replacement.strip().strip("/")
            return rf"[\g<0>](/{ipa}/)"
        return self.replacement


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


def apply(text: str, rules: list[Rule]) -> str:
    for rule in rules:
        pattern = rule.compile()
        if pattern is None:
            continue
        try:
            text = pattern.sub(rule.substitution(), text)
        except re.error as exc:
            log.warning("rule %r failed to substitute: %s", rule.pattern, exc)
    return text


def preview(text: str, rules: list[Rule]) -> list[tuple[Rule, list[str]]]:
    """Which rules fire, and what each one matched.

    Applied in order against the running result, not all against the original.
    Otherwise "vs." and "vs" both look like they fire, when the first has
    already consumed the text and the second never runs.
    """
    hits: list[tuple[Rule, list[str]]] = []
    for rule in rules:
        pattern = rule.compile()
        if pattern is None:
            continue
        found = [m.group(0) for m in pattern.finditer(text)]
        if found:
            hits.append((rule, found))
            try:
                text = pattern.sub(rule.substitution(), text)
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


#: Acronyms said as words. A respelling is the first choice: readable,
#: editable by anyone, and it needs no phonetic alphabet. Every entry was
#: checked against Kokoro rather than guessed — the comment gives what it
#: says without the rule.
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

#: Phonemes, for the rare word a respelling makes worse rather than better.
#: LIBOR is the one: Kokoro already says LIE-bor, and every plain respelling
#: ("Libor", "lybor", "lie bore") lands somewhere else.
PHONEME_HINTS = {
    "LIBOR": "lˈIbɔɹ",
}

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

#: Initialisms to read letter by letter.
SPELL_OUT = [
    "AI", "API", "ARR", "ATM", "BTC", "CDO", "CDS", "CEO", "CFO", "CFTC", "COO",
    "CTO", "DAO", "DOJ", "EPS", "ESG", "ETF", "ETH", "EU", "FCA", "FDA", "FTC",
    "FX", "GDP", "HFT", "IRS", "KYC", "LBO", "LLC", "LP", "M&A", "NDA", "NFT",
    "NYSE", "OTC", "P&L", "ROE", "ROI", "RSU", "S&P", "SEC", "SPV",
    "TAM", "UK", "US", "USD", "VC", "VIX", "YTD",
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

    for token, ipa in PHONEME_HINTS.items():
        add(Rule(
            kind="word",
            pattern=token,
            replacement=ipa,
            is_phonemes=True,
            note="phonemes, where no respelling reaches it",
            sort_order=30,
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
