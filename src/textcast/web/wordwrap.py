"""Wrapping a block's displayed words for the read-along's word-level highlight.

The word list comes straight from the payload the player already gets --
each `WordTiming.text` is a plain slice of the block's own displayed text
(see `audio.compose_word_timings`), not recomputed here from the normalizer
or the source map. Locating each one is a sequential substring search, safe
because they are non-overlapping, ordered slices of `block.text` by
construction. If the text has since been edited and a slice no longer
matches, wrapping is skipped for that block rather than guessed at, and it
falls back to exactly today's rendering -- the same "no words" fallback a
block that was never aligned already gets.
"""

from __future__ import annotations

import html as html_lib
import re

_TOKEN = re.compile(r"<[^>]+>|[^<]+")


_WHITESPACE = re.compile(r"\s+")


def _flexible(word: str) -> re.Pattern | None:
    """``word`` as a pattern, with every run of whitespace made interchangeable.

    A stored word is a slice of `block.text`, and it is looked for in the
    text of `block.rich` -- the same words, laid out by a different pass, so
    whitespace is where the two disagree. Two real examples, out of one
    library: a quote whose paragraphs are "Commitments\n\n" in one and
    "Commitments \n\n" in the other, and a list whose separator is " -- " in
    one and "\n--" in the other.

    Trailing whitespace drops out of the pattern rather than being matched
    loosely, which also stops a highlight covering the blank line after a
    word.
    """
    parts = [re.escape(p) for p in _WHITESPACE.split(word) if p]
    return re.compile(r"\s+".join(parts)) if parts else None


def find_word_ranges(text: str, word_texts: list[str]) -> list[tuple[int, int]] | None:
    """Each word's `(start, end)` in `text`, in order, or `None` if any is missing."""
    ranges: list[tuple[int, int]] = []
    pos = 0
    for w in word_texts:
        pattern = _flexible(w) if w else None
        if pattern is None:
            return None
        found = pattern.search(text, pos)
        if found is None:
            return None
        ranges.append(found.span())
        pos = found.end()
    return ranges


def _wrap_plain(text: str, ranges: list[tuple[int, int]]) -> str:
    parts = []
    pos = 0
    for i, (start, end) in enumerate(ranges):
        parts.append(html_lib.escape(text[pos:start]))
        parts.append(f'<span class="w" data-w="{i}">{html_lib.escape(text[start:end])}</span>')
        pos = end
    parts.append(html_lib.escape(text[pos:]))
    return "".join(parts)


def _wrap_rich(rich: str, ranges: list[tuple[int, int]]) -> str:
    """Walk `rich`'s text runs in document order, leaving every tag untouched.

    `rich` is already-sanitized HTML under a strict allowlist (see
    `ingest/visuals.py`), never arbitrary markup, so a tag/text tokenizer is
    enough here -- no need for a full DOM parser to do this safely.

    A merged word that happens to straddle a tag boundary (rare -- a
    footnote's own body, mainly, if it contains an allowed link) gets its
    `<span>` opened and closed across that boundary too, which is not
    strictly valid nesting. Browsers recover it the way they recover any
    overlapping inline markup (the HTML5 "adoption agency" algorithm); it is
    not worth a full tree-mutation rewrite for a case this rare.
    """
    out: list[str] = []
    pos = 0
    group_i = 0
    in_group = False
    n = len(ranges)

    for token in _TOKEN.findall(rich):
        if token.startswith("<"):
            out.append(token)
            continue
        plain = html_lib.unescape(token)
        i = 0
        while i < len(plain):
            if in_group:
                end = ranges[group_i][1]
                take = min(end - pos, len(plain) - i)
                out.append(html_lib.escape(plain[i : i + take]))
                pos += take
                i += take
                if pos >= end:
                    out.append("</span>")
                    in_group = False
                    group_i += 1
                continue
            if group_i < n and pos >= ranges[group_i][0]:
                out.append(f'<span class="w" data-w="{group_i}">')
                in_group = True
                continue
            take = (ranges[group_i][0] - pos) if group_i < n else (len(plain) - i)
            take = min(take, len(plain) - i)
            out.append(html_lib.escape(plain[i : i + take]))
            pos += take
            i += take
    if in_group:
        out.append("</span>")
    return "".join(out)


def _plain_text_of(rich: str) -> str:
    """Everything in ``rich`` that is not a tag, unescaped."""
    return "".join(
        html_lib.unescape(token) for token in _TOKEN.findall(rich) if not token.startswith("<")
    )


def word_wrap(text: str, rich: str | None, word_texts: list[str]) -> str:
    """The block's displayed content, ready for `|safe`.

    Falls back to the block's ordinary rendering -- `rich` HTML unchanged,
    `text` escaped -- whenever there is nothing to wrap or the stored words
    no longer match what is on the page.

    Where there is `rich`, the words are located in *its* own text and not in
    ``text``. The two carry the same words and not always the same
    characters: a normalizer collapses a run of whitespace, a source writes
    an ellipsis as ". . .", a footnote marker lands one side of a space
    rather than the other. The ranges are then walked through `rich`, so one
    character of disagreement anywhere put every highlight after it one
    character out -- silently, which is the one outcome this module exists to
    avoid. Measured over one real library: 16 blocks of the 234 that have
    `rich` at all.

    Locating them in `rich` costs nothing and fixes all sixteen, because
    `find_word_ranges` walks forward word by word and never cares what sits
    between two of them.
    """
    if not word_texts:
        return rich if rich else html_lib.escape(text)
    if rich:
        ranges = find_word_ranges(_plain_text_of(rich), word_texts)
        return _wrap_rich(rich, ranges) if ranges is not None else rich
    ranges = find_word_ranges(text, word_texts)
    return _wrap_plain(text, ranges) if ranges is not None else html_lib.escape(text)
