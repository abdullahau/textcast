"""Rewrite text so a TTS engine reads it the way a person would.

Financial writing is dense with forms no speech model handles well:
``$72mm``, ``£5bn``, ``150bps``, ``Q3``, ``2019-21``. Left alone an engine
spells them out letter by letter, mangles them, or skips them.

Anything with a *shape* is handled here, because matching it needs a callback.
Anything that is a plain lookup — abbreviations, initialisms, months, phoneme
hints — lives in the pronunciation table instead, where it can be edited.

This runs only on the text handed to the engine. What you read on screen is
never touched, so the page keeps the author's punctuation.
"""

from __future__ import annotations

import re

from . import pronounce

CURRENCIES = {
    "$": "dollars",
    "£": "pounds",
    "€": "euros",
    "¥": "yen",
    "₹": "rupees",
    "R$": "reais",
    "A$": "Australian dollars",
    "C$": "Canadian dollars",
    "HK$": "Hong Kong dollars",
}

SINGULAR = {
    "dollars": "dollar",
    "pounds": "pound",
    "euros": "euro",
    "yen": "yen",
    "rupees": "rupee",
    "reais": "real",
}

#: Suffixes as finance writes them. ``mm`` is millions, not millimetres.
SCALES = {
    "k": "thousand",
    "m": "million",
    "mm": "million",
    "mn": "million",
    "b": "billion",
    "bn": "billion",
    "bln": "billion",
    "t": "trillion",
    "tn": "trillion",
    "trn": "trillion",
    # Already-spelled scales, so "$19 million" reorders to "19 million dollars"
    # instead of leaving a stranded "dollars million".
    "thousand": "thousand",
    "million": "million",
    "billion": "billion",
    "trillion": "trillion",
}

_CURRENCY_CHARS = "".join(re.escape(c) for c in "$£€¥₹")
_PREFIXES = r"(?:R\$|A\$|C\$|HK\$|US\$|[" + _CURRENCY_CHARS + r"])"
_SCALE_ALT = "|".join(sorted(SCALES, key=len, reverse=True))

#: $72mm, £5bn, €300k, US$1.2tn — a currency, a number, an optional scale.
MONEY = re.compile(
    rf"(?<![A-Za-z0-9])({_PREFIXES})\s?(\d[\d,]*(?:\.\d+)?)(?:\s?({_SCALE_ALT}))?\b",
    re.IGNORECASE,
)

#: 72mm shares, 5bn users — a scale with no currency in front.
BARE_SCALE = re.compile(
    rf"(?<![A-Za-z0-9$£€¥₹])(\d[\d,]*(?:\.\d+)?)\s?({_SCALE_ALT})\b(?!\w)"
)

PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?%")
BPS = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)\s?bps?\b", re.IGNORECASE)
QUARTER = re.compile(r"\b([QH])([1-4])\b")
FISCAL = re.compile(r"\b(FY|CY)\s?(\d{2,4})\b")
#: 2019-21 and 2019-2021 as a span, not a subtraction.
YEAR_RANGE = re.compile(r"\b(19|20)(\d{2})\s?[–—-]\s?((?:19|20)?\d{2})\b")
TIMES = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)\s?x\b", re.IGNORECASE)
#: A footnote the parser inlined. Matched whole, so the closing bracket goes
#: too and the aside is bounded by pauses on both sides.
FOOTNOTE = re.compile(r"\[Footnote (\d+):\s*(.*?)\]", re.S)


def _strip_commas(number: str) -> str:
    return number.replace(",", "")


def _is_one(number: str) -> bool:
    try:
        return float(_strip_commas(number)) == 1
    except ValueError:
        return False


def _money(match: re.Match) -> str:
    symbol, number, scale = match.group(1), match.group(2), match.group(3)
    unit = CURRENCIES.get(symbol.upper() if symbol.upper() in CURRENCIES else symbol)
    if unit is None:
        unit = CURRENCIES.get(symbol.replace("US", ""), "dollars")

    number = _strip_commas(number)
    if scale:
        return f"{number} {SCALES[scale.lower()]} {unit}"
    if _is_one(number):
        return f"{number} {SINGULAR.get(unit, unit)}"
    return f"{number} {unit}"


def _bare_scale(match: re.Match) -> str:
    return f"{_strip_commas(match.group(1))} {SCALES[match.group(2).lower()]}"


def _year_range(match: re.Match) -> str:
    start = match.group(1) + match.group(2)
    end = match.group(3)
    if len(end) == 2:
        end = match.group(1) + end
    return f"{start} to {end}"


def normalize(text: str, rules: list[pronounce.Rule] | None = None) -> str:
    """Rewrite one block of text for speech.

    Two layers. The structural transforms below handle anything with a shape —
    money, percentages, quarters, spans — because those need a callback, not a
    lookup. Word-level rules then come from the pronunciation table.
    """
    if not text:
        return text

    # Order matters: money before bare scales, so "$5bn" is not caught twice.
    text = MONEY.sub(_money, text)
    text = BPS.sub(lambda m: f"{_strip_commas(m.group(1))} basis points", text)
    text = BARE_SCALE.sub(_bare_scale, text)
    text = PERCENT.sub(lambda m: f"{_strip_commas(m.group(1))} percent", text)
    text = YEAR_RANGE.sub(_year_range, text)
    text = QUARTER.sub(
        lambda m: f"{'quarter' if m.group(1) == 'Q' else 'half'} {m.group(2)}", text
    )
    text = FISCAL.sub(
        lambda m: f"{'fiscal' if m.group(1) == 'FY' else 'calendar'} year {m.group(2)}", text
    )
    text = TIMES.sub(lambda m: f"{_strip_commas(m.group(1))} times", text)

    # Word-level rules come from the database, so they can be edited on the
    # settings page rather than only here.
    text = pronounce.apply(text, rules if rules is not None else pronounce.active())

    # Pauses on both sides keep the aside from running into the sentence.
    text = FOOTNOTE.sub(r"... Footnote \1. \2. ...", text)

    # Smart punctuation the engines mispronounce or read aloud.
    text = (
        text.replace("—", ", ")
        .replace("–", " to ")
        .replace("…", "...")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
    )

    # Replacing an em dash with a comma can leave " , "; tighten it. The full
    # stop is handled separately so the space before an ellipsis survives.
    text = re.sub(r"\s+([,;:!?])", r"\1", text)
    text = re.sub(r"(?<!\.)\s+\.(?!\.)", ".", text)
    return re.sub(r"\s{2,}", " ", text).strip()
