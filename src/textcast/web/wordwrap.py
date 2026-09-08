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


def find_word_ranges(text: str, word_texts: list[str]) -> list[tuple[int, int]] | None:
    """Each word's `(start, end)` in `text`, in order, or `None` if any is missing."""
    ranges: list[tuple[int, int]] = []
    pos = 0
    for w in word_texts:
        if not w:
            return None
        idx = text.find(w, pos)
        if idx == -1:
            return None
        ranges.append((idx, idx + len(w)))
        pos = idx + len(w)
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


def word_wrap(text: str, rich: str | None, word_texts: list[str]) -> str:
    """The block's displayed content, ready for `|safe`.

    Falls back to the block's ordinary rendering -- `rich` HTML unchanged,
    `text` escaped -- whenever there is nothing to wrap or the stored words
    no longer match the current text.
    """
    if word_texts:
        ranges = find_word_ranges(text, word_texts)
        if ranges is not None:
            return _wrap_rich(rich, ranges) if rich else _wrap_plain(text, ranges)
    return rich if rich else html_lib.escape(text)
