"""Where a spoken word came from, in the text a listener sees.

``normalize()`` and ``pronounce.apply()`` rewrite ``block.text`` into what
the engine reads: money spelled out, dates reordered, respellings swapped
in. Word-level highlighting needs to know, for each word an aligner times,
which displayed character range lit it up — and that mapping is not 1:1.
``"$4.4tn"`` becomes six spoken words; ``"Start quote."`` has no displayed
origin at all.

``TrackedText`` carries a string alongside a record of where each part of
it came from, at *word* granularity — not character-exact, matching every
other "the block is the unit" seam in this app. Every substitution stamps
its whole output with the origin span of whatever it replaced, which is
what lets a many-to-one rewrite like the money example above fall out for
free: the six spoken words all point at the one span "$4.4tn" occupied, and
the caller merges consecutive words sharing one origin into a single
highlight event without this module knowing that rule exists.

This module has no opinion on *what* the rewrites are — ``normalize.py``
and ``pronounce.py`` own that, in ``normalize_tracked`` and
``pronounce.apply_tracked``, sharing the same regex tables and callbacks as
their untracked counterparts. What lives here is only the plumbing: the
span type, and ``tracked_sub``/``tracked_replace`` as drop-in, origin-aware
replacements for ``re.sub``/``str.replace``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """One contiguous run of ``TrackedText.text`` that shares one origin.

    ``orig`` is ``None`` for text with no displayed origin at all — a quote
    marker ``Block.spoken()`` adds, the "Footnote 3." label a footnote
    rewrite inserts. A word built entirely from such runs is dropped by
    ``words()`` before it ever reaches the player: there is nothing on the
    page for it to highlight.

    ``exact`` distinguishes the two ways a run can relate to ``orig``, and
    conflating them was the first bug this module shipped with. Text
    carried through untouched — the ordinary case, most of any block — has
    a *precise* per-character correspondence: querying a sub-range of the
    run must offset into ``orig`` by the same amount, not hand back the
    whole run's origin. Text a substitution produced has no such
    correspondence at all — "4.4 trillion dollars" did not come from a
    character-by-character rewrite of "$4.4tn" — so the whole replacement
    takes one collapsed origin regardless of which part of it is queried.
    That collapse is what lets several spoken words fall onto one displayed
    word for free, and it must not leak backwards onto text nothing has
    rewritten yet.
    """

    start: int
    end: int
    orig: tuple[int, int] | None
    exact: bool = True


@dataclass(frozen=True)
class TrackedText:
    text: str
    #: Contiguous, ordered, covers all of ``text``. Never asserted — every
    #: function here maintains it by construction — because a mismatch
    #: would only ever show up as a wrong highlight, not a crash, and this
    #: module is small enough to get right by inspection instead.
    spans: tuple[Span, ...]

    @classmethod
    def identity(cls, text: str) -> TrackedText:
        """Every character maps to itself. The starting point of every chain."""
        return cls(text, (Span(0, len(text), (0, len(text))),) if text else ())

    def origin_of(self, start: int, end: int) -> tuple[int, int] | None:
        """The displayed span behind ``text[start:end]``.

        An exact run is offset precisely to the queried sub-range; a
        collapsed run (a substitution's own output) hands back its whole
        origin regardless of which part is queried — that is the point of
        it being collapsed. The union of whatever overlapping runs produce,
        not an intersection: word granularity means this only has to be
        roughly right, and a query spanning two origin runs (rare; a rule
        matching across what an earlier rule already rewrote) is better
        served by the widest span than by picking one arbitrarily.
        """
        lo = hi = None
        for span in self.spans:
            s, e = max(span.start, start), min(span.end, end)
            if s >= e or span.orig is None:
                continue
            if span.exact:
                offset = span.orig[0] - span.start
                o_lo, o_hi = s + offset, e + offset
            else:
                o_lo, o_hi = span.orig
            lo = o_lo if lo is None else min(lo, o_lo)
            hi = o_hi if hi is None else max(hi, o_hi)
        return None if lo is None else (lo, hi)

    def wrap(self, prefix: str = "", suffix: str = "") -> TrackedText:
        """Add literal text with no displayed origin — the quote markers, mainly.

        Not ``tracked_sub``: there is no match to replace, only text to add
        at either end, and ``Block.spoken()`` does this once, outside the
        rewrite chain rather than inside it.
        """
        if not prefix and not suffix:
            return self
        shifted = tuple(
            Span(s.start + len(prefix), s.end + len(prefix), s.orig, s.exact) for s in self.spans
        )
        pre = (Span(0, len(prefix), None),) if prefix else ()
        tail = len(prefix) + len(self.text)
        post = (Span(tail, tail + len(suffix), None),) if suffix else ()
        return TrackedText(prefix + self.text + suffix, pre + shifted + post)


def _copy_spans(spans: tuple[Span, ...], a: int, b: int, new_start: int, out: list[Span]) -> None:
    """Copy the portion of ``spans`` covering ``[a, b)`` into ``out``, shifted to ``new_start``.

    An exact run's ``orig`` is sliced to the same sub-range being copied — a
    partial copy of untouched text still corresponds precisely to a partial
    range of the original. A collapsed run's ``orig`` is carried through
    whole, on purpose: it already represents "no finer detail available,"
    and slicing it would fabricate detail that does not exist.
    """
    shift = new_start - a
    for span in spans:
        s, e = max(span.start, a), min(span.end, b)
        if s >= e:
            continue
        if span.orig is None:
            out.append(Span(s + shift, e + shift, None, span.exact))
        elif span.exact:
            offset = span.orig[0] - span.start
            out.append(Span(s + shift, e + shift, (s + offset, e + offset), True))
        else:
            out.append(Span(s + shift, e + shift, span.orig, False))


def tracked_sub(
    pattern: re.Pattern,
    repl: str | Callable[[re.Match], str],
    tt: TrackedText,
) -> TrackedText:
    """``pattern.sub(repl, tt.text)``, with the origin map carried through.

    A replacement's whole output takes one origin span rather than being
    tracked character by character — the range of whatever it replaced.
    Text a match did not touch keeps its existing spans, sliced to the
    piece that survived. ``repl`` takes a plain template string, the same
    as ``re.sub``, or a callable; a callable pairs with the same guard
    ``pronounce.Rule.substitution`` already needs (a regex rule may write
    ``\\1`` into its own capturing group; a word rule's replacement must
    never be read as a template).
    """
    call = repl if callable(repl) else (lambda m: m.expand(repl))
    parts: list[str] = []
    spans: list[Span] = []
    cursor = 0
    last = 0
    for m in pattern.finditer(tt.text):
        if m.start() > last:
            literal = tt.text[last : m.start()]
            parts.append(literal)
            _copy_spans(tt.spans, last, m.start(), cursor, spans)
            cursor += len(literal)
        out = call(m)
        if out:
            parts.append(out)
            # Collapsed: this text was produced by the substitution, not
            # carried through, so the whole replacement takes one origin
            # regardless of which part of it a later query touches.
            origin = tt.origin_of(m.start(), m.end())
            spans.append(Span(cursor, cursor + len(out), origin, exact=False))
            cursor += len(out)
        last = m.end()
    if last < len(tt.text):
        literal = tt.text[last:]
        parts.append(literal)
        _copy_spans(tt.spans, last, len(tt.text), cursor, spans)
        cursor += len(literal)
    return TrackedText("".join(parts), tuple(spans))


def tracked_replace(old: str, new: str, tt: TrackedText) -> TrackedText:
    """``tt.text.replace(old, new)``, tracked. For the smart-punctuation swaps.

    Empty ``old`` is refused the way ``str.replace`` itself would loop
    forever making sense of — ``re.escape("")`` matches everywhere, which
    ``tracked_sub`` would take literally.
    """
    if not old:
        return tt
    return tracked_sub(re.compile(re.escape(old)), lambda m: new, tt)


def tracked_strip(tt: TrackedText) -> TrackedText:
    """``tt.text.strip()``, tracked. For the final trim at the end of a chain."""
    start = len(tt.text) - len(tt.text.lstrip())
    end = len(tt.text.rstrip())
    if start >= end:
        return TrackedText("", ())
    out: list[Span] = []
    _copy_spans(tt.spans, start, end, 0, out)
    return TrackedText(tt.text[start:end], tuple(out))


_WORD = re.compile(r"\S+")


def words(tt: TrackedText) -> list[tuple[str, tuple[int, int] | None]]:
    """Split into whitespace-delimited words, each with its displayed origin.

    ``None`` for a word built entirely from unoriginated text — a quote
    marker, a footnote's own inserted label. The caller drops those before
    building a player payload: there is nothing on the page for them to
    highlight.
    """
    return [(m.group(0), tt.origin_of(m.start(), m.end())) for m in _WORD.finditer(tt.text)]
