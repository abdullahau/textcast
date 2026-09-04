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
    "Australian dollars": "Australian dollar",
    "Canadian dollars": "Canadian dollar",
    "Hong Kong dollars": "Hong Kong dollar",
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
#: 8:00am came out "eight zero zero a m", because espeak reads the zero
#: minutes and then loses the space before the suffix. Measured: "8 a.m." is
#: ˈAt ˌAˈɛm, which is right, and "8am" is ˈAt æm — "eight" and then "am" the
#: verb. A non-zero time is already correct (10:47 is "ten forty seven"), so
#: only the o'clock case and the missing space are touched.
CLOCK = re.compile(r"(?<![\d:.])(\d{1,2})(?::00)?\s*([ap])\.?m\.?(?![\w.])", re.IGNORECASE)

#: The same o'clock, with no am or pm after it.
OCLOCK = re.compile(r"(?<![\d:.])(\d{1,2}):00(?![\d:])")

#: Emphasis markers that reached the block text. Matt Levine writes *before*
#: for italics and the newsletter carries the asterisks through as characters,
#: so the engine said the word "asterisk" out loud. The same patterns as
#: ``ingest.documents``, which strips them from Markdown files at parse time;
#: these survive from every other source.
#:
#: Paired only. A lone asterisk is a footnote marker or a bullet, and stays.
EMPHASIS = [
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"\1"),
    (re.compile(r"(?<![\w_])__([^_]+)__(?![\w_])"), r"\1"),
    (re.compile(r"~~([^~]+)~~"), r"\1"),
]

#: A footnote the parser inlined. Matched whole, so the closing bracket goes
#: too and the aside is bounded by pauses on both sides.
#:
#: The body allows one level of bracket nesting -- `[2]`, a citation the
#: footnote itself cites -- so a `.*?` closing at that inner `]` instead of
#: the footnote's own does not cut the aside short and leave a stray `]` in
#: the spoken output.
FOOTNOTE = re.compile(r"\[Footnote (\d+):\s*((?:[^\[\]]|\[[^\[\]]*\])*)\]", re.S)


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


#: Pictographs and dingbats, which an engine reads out by name. Deliberately
#: not the general symbol blocks: ©, ®, ™, °, currency and the arrows all carry
#: meaning and are all spoken sensibly.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF]"
    "[\U0000FE0E\U0000FE0F]?|[\U0000FE0F\U0000200D]"
)

#: A line ending without terminal punctuation, followed by a blank line.
PARAGRAPH_BREAK = re.compile(r"([^\s.!?:;\"'\u2019\u201d)\]])[ \t]*\n\s*\n\s*")


def normalize(
    text: str,
    rules: list[pronounce.Rule] | None = None,
    g2p: str = pronounce.DEFAULT_G2P,
    phonemes: bool = True,
) -> str:
    """Rewrite one block of text for speech.

    Two layers. The structural transforms below handle anything with a shape —
    money, percentages, quarters, spans — because those need a callback, not a
    lookup. Word-level rules then come from the pronunciation table.

    Everything here is engine-agnostic except a phoneme rule, which is written
    in one G2P's notation. ``g2p`` names the target engine's, and ``phonemes``
    is False for an engine that cannot take injected phonemes at all.
    """
    if not text:
        return text

    # First, so a marker cannot sit between a currency and its number.
    for pattern, replacement in EMPHASIS:
        text = pattern.sub(replacement, text)

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
    text = CLOCK.sub(lambda m: f"{m.group(1)} {m.group(2).lower()}.m.", text)
    text = OCLOCK.sub(r"\1", text)

    # An emoji is read out by its name: espeak says "money bag" for the
    # 💰 in an FT table headed "Ker-CHING 💰", and "Table: Ker-CHING money
    # bag" is not what anyone wants to hear. Dropped for speech only — the
    # page keeps what the publication printed. ©, ® and ™ are left alone: they
    # are named correctly and a photo credit means to say them.
    text = EMOJI.sub(" ", text)

    # Smart punctuation the engines mispronounce or read aloud. Before the
    # rules, not after: web prose is full of curly apostrophes, and a rule
    # written for who'll would never have matched who’ll.
    text = (
        text.replace("—", ", ")
        .replace("–", " to ")
        .replace("…", "...")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
    )

    # Word-level rules come from the database, so they can be edited on the
    # settings page rather than only here.
    text = pronounce.apply(
        text, rules if rules is not None else pronounce.active(), g2p, phonemes
    )

    # Pauses on both sides keep the aside from running into the sentence.
    text = FOOTNOTE.sub(r"... Footnote \1. \2. ...", text)

    # A paragraph break inside a block is a sentence break. Collapsed to a
    # space by the rule below, a quote's bolded lead-in ran into the sentence
    # after it: "Compute Services Agreements with Third Parties We believe".
    # Only where the line does not already end in something terminal.
    text = PARAGRAPH_BREAK.sub(r"\1. ", text)

    # Replacing an em dash with a comma can leave " , "; tighten it. The full
    # stop is handled separately so the space before an ellipsis survives.
    text = re.sub(r"\s+([,;:!?])", r"\1", text)
    text = re.sub(r"(?<!\.)\s+\.(?!\.)", ".", text)
    return re.sub(r"\s{2,}", " ", text).strip()
