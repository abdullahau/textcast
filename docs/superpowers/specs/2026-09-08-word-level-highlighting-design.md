# Word-level highlighting — design

Branch: `word-highlight-forced-align`. Extends the read-along from
block-level highlighting (today's `BlockTiming`, one entry per paragraph)
to word-level, the way Chrome's Reading Mode and Apple's screen reader do.

## Problem

`docs/decisions.md` already names the gap: block timing comes from the
engine's own audio length, for free, with no forced aligner. Word timing
needs one — Kokoro and kokoro-onnx do not return per-word timestamps.

Two problems sit on top of "which aligner":

1. **Process cost.** This app measures every model's residency in
   `docs/decisions.md` and keeps the ONNX engine deliberately torch-free.
   An aligner that reintroduces torch into that build path undoes a
   measured decision.
2. **The seam.** `Block.spoken()` is not `block.text`. Money, dates,
   quarters and pronunciation rules reshape the string an aligner would
   align against, so a naive word-index mapping back to the displayed
   page is wrong on exactly the content (Bloomberg, FT, Economist) this
   app is mostly used to read.

## Goals

- Word-level highlight in the reader, accurate enough to read by.
- Works on both engines, not one.
- No increase in steady-state (non-building) memory or import cost.
- Peak build memory does not exceed today's peak by more than one
  resident model.
- Every claim about a dependency's footprint is measured, not assumed —
  same standard as everything else in `docs/decisions.md`.

## Non-goals

- Perfect character-level alignment. Word-level is the unit everywhere
  else in this app (`CLAUDE.md`: "the block is the unit") and it is the
  unit here too.
- Any change to what is spoken. `Block.spoken()`'s output is untouched;
  this only adds a parallel record of where each word landed.

---

## Architecture

### Phase order: synthesize, release, then align

A build already runs one article per child process that exits when the
job ends. Word alignment adds a **second phase inside that same child**,
strictly after the first:

```
render_article() [existing, unchanged]
  → TTS engine/pool resident
  → every block synthesized, cached as .i16, encoded to Opus
  → engine pool dropped: gc.collect(); malloc_trim(0)
                                                    ← existing pattern,
                                                      jobs.py already does
                                                      this for the pool

align_article() [new, runs after the above, same child, same job]
  → aligner model loaded (lazy import, first use only)
  → per block: read the cached .i16 PCM back (no re-synthesis)
  → run forced alignment against block.spoken()
  → compose with the source map (below) into displayed-word timings
  → write alongside the audio cache, compose into the manifest
  → aligner released before the child exits anyway
```

The two models are never resident together. Peak memory during a build
becomes `max(engine pool, aligner)` for the alignment phase, not their
sum — the same "one process, one engine" discipline `decisions.md`
already enforces, extended to "one model at a time," not "one model
total."

Both the TTS engine and the aligner are imported lazily, inside the
function that first uses them — never at module top level in
`audio.py` or `jobs.py`. This is the existing rule
(`docs/traps.md`: *"The parent must stay clean… `audio.py` is safe — it
imports numpy and the engine import lives inside `KokoroEngine.__init__`"*)
applied to one more dependency. The parent worker (38 MB, polls the job
table) and the web app (never loads a model inside a request) are
unaffected either way — this is entirely inside the one child process
that already owns the whole cost of a build.

### Why alignment reads the cache instead of re-synthesizing

`_speak()` already writes each block's raw samples to `cache/*.i16`
before `trim_silence` and `encode_opus` touch them. Phase two reads that
same file back — the exact audio that will ship, not a resynthesis of
it — so alignment can never disagree with what a listener actually
hears. It also means alignment is skippable per block: a block whose
`.i16` came from a **cache hit** (unchanged text) already has whatever
`.words.json` was written for it last time; only a genuinely new or
edited block pays for a fresh alignment pass.

---

## A. Source-mapped normalization

`normalize()` and `pronounce.apply()` are chains of `re.sub` and
`str.replace`. A new module, `textcast/sourcemap.py`, adds a
`TrackedText` type: a string plus a run-length-encoded array of origin
spans into the original `block.text`. Every substitution gets a tracked
counterpart (`tracked_sub`, `tracked_replace`) that stamps a
replacement's output with the origin span of what it replaced —
word-level granularity, matching the rest of this app, not
character-perfect.

New functions, **not** replacements: `normalize_tracked()` and
`pronounce.apply_tracked()` sit beside the existing `normalize()` and
`apply()`, sharing the same regex tables and callbacks so a fix to one
rule cannot silently diverge from the other's understanding of it. The
existing untracked path is the one every build already measures and
tunes (`docs/traps.md`: 1.014 s → 0.633 s over 2,400 blocks) and stays
exactly as it is — it still computes the cache key and drives ordinary
synthesis. The tracked path is a separate, cacheable call, described
next.

At the end of the tracked chain, the spoken text is tokenized into
words, each carrying the displayed character range it descended from.
Consecutive spoken words sharing one origin span merge into a single
highlight event — this is how "four point four trillion dollars" (six
spoken words) becomes one highlight over the displayed `"$4.4tn"`, with
no special case written for money, dates, or any other expansion: it
falls out of the merge rule for free. A spoken word with no origin at
all — `"Start quote."`, a `"Footnote 3."` marker inserted by
`FOOTNOTE.sub` — carries no span and is dropped before it reaches the
player.

**Cache key:** `(block.text, g2p, phonemes, rules-fingerprint)`. Not
voice, not engine identity, not pace — the source map is a property of
the *text*, and is reusable across every build that has not changed the
block or the pronunciation rules. Cheap relative to alignment; computed
once per unique block and rule set, kept until either changes.

---

## B. Forced alignment

`ctc-forced-aligner` (PyPI), the ONNX-runtime path, MMS_FA model — CTC
alignment via its own Viterbi decoder, not torchaudio's `forced_align`,
which is what lets it skip torch. **Unverified claims that must become
verified facts in implementation task 1**, before anything else is
built on top of them:

- The installed package's ONNX path genuinely never imports torch.
  Checked the way this codebase already checks such things: instantiate
  it inside a throwaway child process and read `/proc/<pid>/maps` for
  `torch/lib`, the same test `docs/traps.md` prescribes for the engine
  processes.
- Resident memory of the loaded aligner, measured the same way every
  engine in `decisions.md` is: four independent instantiations, one
  shared, whichever this ends up needing given the phase-order design
  above (likely one instance is enough, since alignment is not run
  concurrently across a thread pool the way synthesis is — that itself
  is worth deciding from the CPU measurement below rather than assumed).
- Wall-clock cost per block and per article, CPU-bound, on this box (the
  4-core ARM Neoverse-N1 every other number in `decisions.md` is from).

If any of these come back wrong — torch leaks in, or the model is too
large or too slow to be worth it — the fallback is torchaudio
`forced_align` gated to the Kokoro (torch) engine only, with
kokoro-onnx builds keeping block-level highlighting. That fork is a
one-line decision at that point, not a redesign: phase two already reads
cached PCM and writes the same `WordTiming` shape regardless of which
aligner produced it.

**License:** the default MMS_FA weights are CC-BY-NC 4.0 —
non-commercial. `textcast` is self-hosted and not sold, so this is
likely fine, but it is a conscious call to record here rather than
discover later, the way `decisions.md` records Supertonic's licence as
the reason it was dropped.

---

## C. Cache and data model

`BlockTiming` (in `audio.py`) gets one new optional field:

```python
@dataclass
class WordTiming:
    text: str        # the displayed word (post-merge, per the source map)
    start_ms: int     # absolute within the section's audio
    dur_ms: int

@dataclass
class BlockTiming:
    ...                       # unchanged
    words: list[WordTiming] = field(default_factory=list)
```

`words` is empty wherever alignment was skipped or failed for that
block — the player already has to handle "no words," so it is the same
code path as an article built before this feature existed.

On disk, alongside the existing `cache/<key>.i16`:

- `cache/<key>.words.json` — the composed, final `WordTiming` list for
  that block, keyed by the **same** hash as the audio
  (`engine, voice, rate, text`). A cache hit on the audio is a cache hit
  on the words; `sweep_cache` (`cache.py`) is extended to sweep both
  suffixes by the same reachability computation it already does for
  `.i16` — no second sweep pass, one more suffix in the same walk.

The source map itself is not written to `cache/` at all — it is cheap
enough to recompute from `(block.text, g2p, phonemes, rules)` inside the
alignment phase, an in-memory `lru_cache` for the duration of one build
is enough, and giving it a disk cache would be a second cache to keep
in step with pronunciation-rule edits for no measured benefit.

---

## D. Wire format

WebVTT is unchanged — still block-level cues only. It is documented as
a background-tab backstop (`docs/decisions.md`: *"the file's own record
of the timings"*), not the render path; word-level cues there would
complicate the format for a case (a hidden tab) that does not need
word precision.

Word timings ride the existing JSON payload, one array appended per
block entry, delta-encoded against the block's own `start_ms` to keep
the payload small on a long article:

```
[id, start_ms, dur_ms, words]
words = [[text, start_ms_delta, dur_ms], ...]   // delta from block's start_ms
```

---

## E. Player rendering

The harder half of the client change is not the highlight loop — it is
that a block can carry `rich` HTML (bold, italic, links under a strict
allowlist), and wrapping individual words has to walk *rendered* markup
without breaking it, not just split `block.text` on whitespace.

Word-wrapping happens **server-side**, in `reader.html` at render time:
an HTML-aware word-splitter walks each block's rendered text nodes
(`rich` if present, `text` otherwise), wraps each word in
`<span class="w" data-w="N">`, and leaves every existing tag alone.
The player references words by index (`data-w`), matching the payload's
array position — not by character offset, which would need to survive
`rich`'s tags and be fragile to reproduce identically client-side.

`player.js` extends `blockAt`'s existing bisection: once the active
block is known, a second bisect over its `words` array (already sorted
by construction) finds the active word, reusing the same
`requestAnimationFrame` clock-reading loop — `followClock` calls into
one more bisect per frame, not a second timer. Applying the class is
the same pattern `highlight()` already uses: compare against the
currently-active word index, do nothing if unchanged, toggle one class
otherwise.

A block with an empty `words` array (alignment skipped, or an article
built before this shipped) falls back to exactly today's block-only
highlight — no branch needed beyond "is `words` empty."

---

## F. Degradation and error handling

- A block that fails alignment (aligner error, or a block whose spoken
  text the aligner cannot handle) keeps `words: []` for that block only.
  Never fails the build — the same principle as
  `docs/traps.md`'s emoji-only-block fix: "nothing to say is silence,
  not an error," extended to "nothing aligned is block-level, not an
  error."
- An article built before this feature ships has no `.words.json`
  files; nothing re-aligns it until its next real rebuild (a text edit,
  a re-parse, a voice change) — consistent with how every other cached
  render already behaves.

---

## G. Rollout

A new build option, `word_highlight: bool`, alongside `voice`,
`quote_voice`, `speed` — **off by default** until the measurements in
task 1 are in and reviewed. This is the same posture `decisions.md`
takes toward every engine and dependency choice: default off, prove the
cost, then decide the default. Flipping it on is a one-line change to
`voice_defaults` once the numbers are known.

---

## H. Testing

- `tests/corpus`: forced-alignment output over a handful of fixtures
  spanning plain prose, a money-heavy Bloomberg-style block, a footnote,
  and a quote — checked by eye once, then locked as a byte-identical
  regression the way other corpus tests already work.
- `sourcemap.py`: unit tests per transform (`_money`, `_decimal`,
  `_year_range`, the emphasis strip, the footnote insertion) asserting
  the origin span, not just the output string — this is the part most
  likely to silently drift from `normalize()`'s own behaviour if the two
  are ever edited out of step.
- Player: extend the existing Playwright read-along tests
  (`test_seeking_to_a_block_highlights_that_block` and neighbours) with
  one asserting a word-level class lands on the right `data-w` at a
  known point in a fixture's audio.
- Cache: a test that an audio cache hit produces a `.words.json` hit
  too, and that editing pronunciation rules invalidates the source map
  the same build cycle it invalidates the audio.

---

## I. Measurement plan

Recorded in `docs/decisions.md` once known, in the same table format as
the existing engine and audio/timing entries — this design explicitly
does not assert numbers it has not measured:

- Aligner resident memory, loaded once, measured the way every engine
  in this app already is (`docs/decisions.md`'s "The engines" table).
- Wall-clock cost of phase two per block and per a representative full
  article, against phase one's existing synthesis time, on the same
  4-core ARM Neoverse-N1 box every other number here is from.
- Whether the ONNX path actually avoids torch at runtime — a pass/fail,
  not a number, but the gating one.
- Peak RSS across the whole build (phase one + phase two), against
  today's peak with `word_highlight` off, to confirm the phased design
  actually delivers `max()` rather than accidentally `sum()`.

---

## Open risks going into implementation

1. Which `ctc-forced-aligner` PyPI release is actually torch-free at
   runtime — verify before writing anything else against it.
2. MMS_FA's CC-BY-NC licence — a conscious accept, not a default.
3. The HTML-aware word-splitter in `reader.html` is new surface area
   this app has not needed before; `docs/traps.md`'s CSS section is a
   reminder of how much this kind of markup-walking code tends to bite
   once it meets real publication HTML, not just fixtures.
4. If task 1 fails the torch check, the fallback (torchaudio,
   Kokoro-only) turns this from an engine-agnostic feature into an
   engine-dependent one — a real product decision, not just an
   implementation detail, and worth surfacing again at that point rather
   than silently degrading.
