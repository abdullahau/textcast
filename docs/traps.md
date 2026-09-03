# Traps

Things that have already bitten once. Each says what broke and why the code
looks the way it does, so nobody spends that afternoon twice.

This is not in `CLAUDE.md` because that file is read at the start of every
session and this one is only wanted once you are in the code it describes.
**Read the section you are about to touch**, and add to it when something
bites you.

## Parsing: the DOM, the adapters and the visuals

- **`node.css()` searches the node *and* its subtree.** So a `<table>` asked
  whether it holds another table says yes about itself, a container matches
  its own selectors, and `blocks_from_dom` turned a whole article into one
  figure the moment `article-grid--no-full-width-graphics` matched
  `[class*="graphic"]`. `css_matches` and `any_css_matches` are no help: both
  are true when a *descendant* matches, which is how every wrapper on an FT
  page "matched" `.o-table` and rescued the promo banners the junk filter had
  just refused. The only self-only test is `node.css_first(sel) == node`.
- **Two lexbor nodes for the same element are not `is` each other.** lexbor
  hands out a fresh wrapper per lookup. They compare equal — `__eq__` and
  `__hash__` are the node's `mem_id` — so every ancestor walk, every "have I
  emitted this" check and every `stop=container` guard uses `==`. `ancestor_
  tags` had the identity bug from the start and walked past its stop to the
  root; harmless for `blockquote` and `li`, and not harmless at all once the
  same helper was asked about figures.
- **A srcset is not comma-separated, quite.** Substack serves every picture
  through Cloudinary, whose path is `w_1456,c_limit,f_webp`, so
  `split(",")` cut one candidate into three and produced an address that
  resolved against the article's own host. The comma that separates two
  candidates has whitespace after it; a comma inside a URL never does.
  Reading the srcset at all is worth it twice over: the widest candidate is
  the best copy on a live page, and on a page saved to disk it is the only
  *absolute* address, because the browser rewrote `src` to point at the
  `_files` directory beside the HTML.
- **`figure` was in both noise lists, and that is what dropped every chart.**
  FT and Bloomberg each opened with `"figure"`, which is a blunt way of saying
  "teasers, promos and author headshots". Those are named directly now —
  `.o-teaser`, `[class*="AuthorBio"] img`, `event-promo` via the shared
  `JUNK_CLASS` — and the pictures that carry an argument survive.
- **A `keep` rule must not outrank the junk filter from six levels up.**
  `VisualRules.keep` exists to rescue a publication's charts from
  `JUNK_CLASS`. Matched loosely it rescued the FT's event promo instead,
  because an ancestor of the promo also contained the article's `o-table`.
  Both walks are depth-capped and both stop at the container.
- **`©\b` never matches.** Neither the symbol nor the space after it is a
  word character, so the boundary has nowhere to sit. `BARE_CREDIT` puts the
  `\b` on the words only, which is why "© Reuters" is now recognised as a
  credit and kept out of the caption.
- **A cell may claim any span it likes.** The FT's table footer is
  `colspan="1000"`, which is a way of saying "the whole row", not a width.
  Left alone it made a row of a thousand cells. Capped at 12, and `tfoot` is
  read into `media["foot"]` rather than into the rows: it is a credit line.
- **`extract.STRIP` removed the visuals before the walk could see them.**
  `figure`, `figcaption` and `iframe` were on it, so giving the catch-all
  adapter a `VisualRules` did precisely nothing and the failure was silent —
  the rules were right, the DOM had already been emptied. They are off the
  list; `visuals.py` refuses the furniture, which is what it is for.
- **A publication puts its byline in the head, not the body.** Bloomberg names
  the writer in `parsely-author`, `sailthru.author` and `author`, and again in
  a byline whose class carries a build hash. The meta tags are the stable
  read; the byline is the fallback. The author is editable on the article, for
  anything pasted.
- **A reader library raises its own errors, not yours.** A file named `.pdf`
  that is not one made pypdf throw `PdfStreamError` straight through to the
  browser as a 500 — on single uploads as well as batches. The readers turn
  anything they cannot open into `UnsupportedDocument`, and the batch loop
  catches broadly on purpose: one bad file must not cost you the other
  nineteen.

## Re-parsing, and editing an article

- **Re-parse used to delete every summary in the library.** A summary is a
  block, and no stored source ever held one, so rebuilding the blocks from the
  source simply did not produce them. Thirty-five of them, across seven
  articles, each one a call to a model, and nothing said a word — the word
  count dropped by ~500 an article and that was the only sign. They are
  carried across now, filed under their section title before the delete and
  put back at the head of the section they belonged to. The title is the only
  handle there is: a section the parser fix renames or splits loses its
  summary, and `Ingested.summaries_lost` says how many so it is visible rather
  than silent.
- **`media["file"]` is bookkeeping and must not be compared.** The picture
  fetch writes it after `store` has run, so the stored copy always carries one
  and a fresh parse never does. Anything diffing a stored article against a
  fresh parse has to drop it first, or every article with a picture in it
  looks changed for ever.
- **An audio wipe must ask what it is deleting.** `delete_audio` and the wipe
  after a block edit both did `for child in media.glob("*"): child.unlink()`,
  which is fine while everything in there is a file and wrong the moment
  `images/` is beside them — `unlink` on a directory raises, and the pictures
  would have gone with audio that can simply be built again. Both take files
  only now, and `_tidy` removes the directory only when nothing is left in it.
  `service.delete` is the one that takes the lot, with `shutil.rmtree`.
- **A re-parse remembers nothing, so the disk has to.** Re-parsing rebuilds
  every block from scratch, and none of them carries the `file` the last parse
  stored. Without a check against the directory, re-parsing the library would
  download every picture in it again to write bytes already there.
  `already_stored` globs for the address's hash and reuses what it finds.
- **Editing text is cheap; removing a block is not.** Text edits leave the ids
  alone, so the audio and the timing map stay valid and a rebuild re-reads only
  what changed. Removing one moves every id after it, so the audio has to go —
  `replace_blocks` clears it and sets the status, and the media files are
  deleted. The block cache is deliberately kept: it is keyed by the text, so
  the rebuild is an encode and nothing goes back to the model.
- **Deleting summaries must not call `delete_audio`.** It did, and
  `delete_audio` also takes every render only this article wants — so dropping
  one summary sent every *other* block back to the model on the next build,
  minutes of synthesis to undo a paragraph the article was keeping. Removing a
  block is a hand edit by another name, and `edit_blocks` had always kept the
  cache for exactly this reason. `_drop_media` is now the shared piece: the
  files go, and keeping the cache is the separate decision it always was.

## Pronunciation, phonemes and the rules

- **A rule has three replacements, and all three are optional.**
  `replacement` is plain text and reaches every engine, because the engine
  never knows a rule ran. `misaki` and `espeak` are IPA, one field per
  phonemiser. For each engine the rule takes the IPA written for *its*
  phonemiser if there is any, and the plain replacement otherwise; a rule with
  neither does not fire there at all. One rule can therefore be written once
  for everything, aimed at one phonemiser, or both.
- **Why two IPA fields and not one.** `pronounce.py` emits IPA as
  `[word](/ipa/)`, which is *misaki's* markup: misaki reads it and removes it.
  espeak has never heard of it and read it out — `[LIBOR](/lˈIbɔɹ/)` became
  "libber slash el stress eye bee open-or turned-ar slash", 3.71 s of audio
  for two words. Nor are the notations the same: misaki's capital `I` is the
  /aɪ/ of "eye", which espeak spells `aɪ` and reads as the letter I.
- **`is_phonemes` is derived, not stored.** `bool(misaki or espeak)`. It was
  a column beside the fields it describes, so it could disagree with them.
- **An engine that takes no phonemes at all gets only the other rules.**
  `accepts_phonemes = False` makes the normaliser drop every phoneme rule
  rather than hand over markup something would read aloud. Nothing shipping
  today sets it; the test does, because that is the failure it exists to stop.
- **`TTSEngine` is `runtime_checkable`, so its body is a contract.** `g2p` and
  `accepts_phonemes` are deliberately *not* declared there: naming an
  attribute in a runtime-checkable Protocol makes `isinstance` demand it, and
  every existing engine and test double would have failed. `tts.g2p_of` reads
  them off whatever it is given and answers "misaki" for anything silent.
- **A respelling cannot be aimed at one engine.** It is ordinary text, so it
  reaches both. Every rule added for one has to be measured on the other, and
  the useful cases are the ones where the good engine does not move: "Solamon"
  leaves misaki's `sˈɑləmən` exactly as it was and fixes espeak's doubled
  vowel; "accreetive" leaves `əkɹˈiTɪv` alone and fixes `ɐkɹˈɛɾɪv`. When no
  spelling does both, a phoneme rule with two spellings is the tool.
- **The pre-filter tests the running text, not the text `apply` was given.**
  A rule may write a word a later rule matches. Testing the original would
  skip the second rule and leave the first one's output unread, so the
  lower-cased haystack is refreshed whenever a rule fires — which is rare, so
  it costs nothing. And the test is lower-cased on both sides whatever the
  rule says about case: it only has to be a *superset* of what the pattern
  would match, and a rule that gets past it still meets its own guards.
- **`_prepared` is keyed on the rules, not on the list.** Keyed on the list, a
  rule edited on the Voice page would go on speaking with the wording it had
  when the process started. `Rule` is a frozen dataclass, so the tuple of them
  is the key and an edit is a different key.
- **Seeding is recorded, not inferred.** It used to skip whenever the table
  had anything in it, which kept a deleted rule deleted and also meant a rule
  added to a later release never reached an existing library. `db.SEEDED_KEY`
  holds every built-in ever offered, so both hold at once. A library from
  before the record takes its current rules as the baseline, which costs one
  pass: a built-in deleted back then comes back on the first start after
  upgrading. Nothing can tell it apart from one never shown.
- **Built-in pronunciation patterns must be unique per kind.** The seed's
  `ON CONFLICT` silently overwrites, which once turned `REIT` into "R E I T".
  `builtin_rules()` now refuses duplicates.
- **`preview()` applies rules in sequence**, not all against the original text,
  or `vs.` and `vs` both appear to fire when only the first does.
- **A month rule needs a number beside it.** `Jul 2` and `2 Jul` are dates;
  `Julian`, `March` and a colleague called Jan are not. The leading form may
  swallow its abbreviation dot; the trailing form may not, or it eats the
  sentence's full stop.
- **`phonemizer  words count mismatch` is information, not an error.** espeak
  returned a different word count than the single token it was given. Usually
  it is right — `JPMorgan`, `INmune`, `DeFi`, `McKinsey` all split correctly.
  It is still worth grepping the log for, because it is the only signal that
  something reached the engine that should not have: it is how the stray
  emphasis markers were found.

## The TTS engines

- **The ONNX engine does misaki's job itself.** It phonemises the prose with
  espeak, splices the rule's phonemes in between, and hands the whole string
  to the model as phonemes. That works because both engines share one phoneme
  vocabulary — 114 symbols, checked, misaki's capitals included. Measured:
  `The LIBOR rate rose` is `lˈɪbɚ` untouched and `lˈaɪbɔːɹ` with the rule.
- **The two engines offer the same voices under the same names.** They are
  the same voices: same ids, same names, same order. Nothing in a picker
  distinguishes them and nothing needs to — every picker shows one engine's
  voices at a time and the engine select above it says which. The ONNX names
  carried an `(ONNX)` suffix for a while; it repeated on twenty lines what one
  control already said. Four pickers show voices — Default voice and Test the
  voice on the Voice page, voice and quote voice on the article — and all four
  read from the same `_voices_by_engine` payload, so they cannot drift.
- **Engine availability probes the dependency, not the wrapper.** The heavy
  import lives inside `__init__`, so `import textcast.tts.kokoro` always
  succeeds. `is_installed` checks `spec.requires`.
- **Three warnings fire on every engine start and none is ours.**
  `weight_norm` is deprecated and kokoro is built on it; kokoro's config asks a
  one-layer LSTM for dropout, which torch ignores and says so; and thinc, under
  spaCy, under misaki, calls `torch.jit.script`, which torch says "may break"
  on Python 3.14. `tts/kokoro._quiet_known_warnings` drops those three by their
  exact text. Do not widen it: `phonemizer  words count mismatch` is a warning
  worth reading, and it is how the stray emphasis markers were found.
- **Nothing may load a model inside a request.** The voice picker broke this
  rule for months: every article page called `_voices()`, which built an
  engine, so the first page load after a restart waited for the weights. It
  also made the player tests flaky. `tts.catalogue` reads the voice table from
  the wrapper module instead, and loads nothing. `_phonemes_for` uses
  `tts.loaded_engine`, which never builds one — a phoneme line is a nicety and
  is not worth stalling a web-only process for seconds. `/api/say` may build,
  because the user asked to hear something.
- **The KModel registry is weak on purpose.** `tts.kokoro._models` is a
  `WeakValueDictionary`. A strong one would be the single thing keeping 312 MB
  of weights alive after the last pipeline had gone, which is exactly what the
  idle unload exists to prevent.
- **Building a second pool beside the first is how you reach 7.5 GB.**
  `tts._shared` is a *strong* dict, and `engines_for` publishes each pool's
  first instance into it. Nothing but `release_shared` takes it back, so the
  old pool's first instance pinned its session for the life of the child. One
  queue that switched engine mid-drain: 1.8 GB on ONNX, 3.5 GB the moment the
  kokoro pool finished loading, peak **7,508 MB**. No process loads two
  engines now — a job for the other one is left for the next process — and
  `engines_for` raises rather than building a second pool. The
  `WeakValueDictionary` in `tts/kokoro_onnx.py` cannot help here: a strong
  reference holds its value.

## The player and the read-along

- **Blocks are `<p>`, never `<button>`.** They were buttons once, and every
  attempt to select a paragraph started playback. Seeking is the gutter handle
  and nothing else: tapping the text was an option for a while and it fought
  with selecting a sentence for no gain, since every block has a play button.
- **`seeked` is not "ready to play".** It means the playhead moved, not that
  anything is decoded there. Calling `play()` on it runs the clock while no
  sound comes out — the first word or two of a block, gone. Wait for
  `readyState >= 3`, with a timeout so a missing event never blocks playback.
  `preload="metadata"` makes this likely on the first seek deep into a file.
- **Not every seek comes from this file.** media-chrome owns the skip buttons
  and the scrub bar, and they set `currentTime` themselves. Anything that only
  resyncs inside `seekWithin` will not see them; the `seeked` listener on the
  element does.
- **`activeCues[0]` is the cue that is *ending*.** At a boundary the browser
  reports both the outgoing and the incoming cue, ordered by start time, so
  the highlight sat a block behind after every seek. Take the last one. And
  `cuechange` fires only on a *change*, so a track that loads with a cue
  already active highlights nothing until the next boundary — `syncHighlight`
  runs on track load and after every seek.
- **Never resume playback on a bare `seeked` event.** It is asynchronous, so
  an armed listener fires on whatever seek happens next — including one the
  user made. `seekWithin` checks the playhead actually landed where it asked
  before playing.
- **A section's place in the payload is not its index.** `build_payload` drops
  a section with no audio, so the array position and `section.idx` diverge.
  The player maps one to the other; the page marks blocks with `idx`.
- **The player must not stop on a block you asked it to jump to.** Seeking to
  a figure is already a decision to look at it, and pausing there fights the
  person who pressed the button. `stopToLook` only fires while the audio is
  playing, and remembers the block it stopped on so pressing play again
  carries on past it instead of stopping on the same one for ever.
- **The player's own toggles are inside the sheet, which is hidden.**
  Playwright's `check()` waits for a visible element and times out on one that
  is not. A test about the *setting* sets the property and dispatches
  `change`; opening the sheet is a different test's business.
- **A block is a `<p>` or a `<figure>`, and never a `<button>`.** The old test
  asserted every block was a `P`, which was a proxy for the thing that
  actually matters — a block that is itself a control swallows the click that
  would have selected a sentence. A figure has no prose in it to select.
- **A position at the end is the end, whatever the flag says.** A decoded
  section runs a little longer than the manifest says — the clock reads the
  *decoded* audio and Opus sits on a ~1.02 s page grid — so a finished article
  can report a few seconds past its total, and `duration()` rendered that as
  **"-1:59:57 left"** for ever. `save_position` marks anything within
  `end_slack` of the total as finished, `continue_listening` drops such a row
  anyway, and `duration()` refuses to render a negative. The slack is a tenth
  of the article capped at five seconds: a flat five is most of a
  twenty-second note, and starting one would have marked it played.
- **The player must carry `finished`, not recompute it.** Every ordinary save
  sent `finished: false`, so opening a completed article and leaving without
  playing threw the completed state away. It was invisible until the badge
  surfaced it. Only the end of the last section sets the flag, and only
  pressing play clears it.
- **Clearing the position must silence the saves that follow it.** "Stop and
  forget my place" pauses and seeks, and both a `pause` and a `timeupdate`
  write the row — so it came straight back. A flag in `player.js` stops the
  writes after the button.
- **A menu with no way out is a trap.** The player sheet had only Escape,
  which a phone does not have: it now closes on its own button and on a tap
  outside. The nav menu does the same.

## The library, search and the pager

- **A `<select>` default must match an option string exactly.** The reading
  pace was formatted with `%g`, which writes 1.0 as "1", matching none of
  "0.8".."1.3". Nothing was marked selected, so the browser silently chose the
  first option and every new build defaulted to 0.8x.
- **What someone types is not an FTS5 expression.** Raw input went to `MATCH`,
  so a hyphen, an ampersand or a trailing `OR` was a syntax error — searching
  for "Drug-Trial" or "roll-up", words in the library's own titles, returned a
  500. `db.fts_query` quotes every term as a phrase.
- **Blocks are not the whole article.** `block_fts` indexes block text, which
  covers summaries, quotes and footnotes because those are blocks. It can
  never cover the title, the byline, the publication or the tags, which are
  columns on `article`. `search` runs a substring match over those as well and
  puts the article hits first; they open at the top, having no block to point
  at.
- **A pager needs one filter, not two.** The rows and the count that divides
  them read the same `db._article_filter`. Written apart, a filter added to
  one and not the other is a library that says "26-50 of 300" and shows a
  different 25.
- **The library is ordered by when it was added, not when it was published.**
  A newsletter from last March, read today, belongs at the top with everything
  else that arrived today. The old order fell back to `added_at` whenever
  there was no publication date, so the two were already mixed.
- **The filter form carries no page.** Changing a filter starts at page 1,
  which is where the answer to a new filter is. The pager's own links carry
  the filters, or paging would clear them. A page number past the end is
  clamped to the last page: a bookmark outlives the filter that made it.
- **A page that polls `/api/jobs` must filter to itself.** The reader painted
  whatever job came back last over its own progress, so opening an article
  that was building showed someone else's summary a second later.
- **A message you cannot close is one you learn to read past.** A failed build
  or a part-refused summary pass keeps its card until another job replaces it.
  Every `.notice` and any box marked `data-dismissible` gets a cross, added by
  one script in `base.html` rather than in six templates. A box with a
  `data-dismiss-key` remembers the dismissal in `localStorage`; the key is the
  job id, so the *next* failure is a new message and shows. A running card is
  not dismissible: it is about to change.

## Layout and CSS

- **The layout rules that keep being relearned.** Each of these cost an
  afternoon once, and the incident is in git; what is left is the rule.
  - *Centring is `grid`, not `flex`.* A centred flex row centres the row, not
    the thing in the middle of it — "Previous" is wider than "Next", so the
    page number sat right of centre. `grid-template-columns: 1fr auto 1fr`.
  - *Alignment in a `.row` is on the bottom edge.* So a control with a taller
    intrinsic box drifts: a file input needs a fixed height rather than a
    minimum, and a checkbox beside a labelled field lines up by matching a
    field's height, not by `align-self: center`. `.row > *` is
    `flex: 1 1 9rem`, so anything that must not stretch says so.
  - *`hidden` loses to any rule that sets `display`.* A `.row` is
    `display: flex`, so `element.hidden` read true while the field showed —
    and a Playwright check of the property passed while the screenshot did
    not. `app.css` carries `[hidden] { display: none !important; }`.
  - *Equal boxes are not equal ink.* A text link reserves its own padding and
    a filled button reserves nothing, so an identical flex gap looks wrong.
    Measure the boxes *and* look at it.
  - *`vertical-align: middle` is a font metric, not the middle of the box.*
    It sits half an x-height above the baseline. `vertical-align: top` plus
    the input's own `line-height` is the whole answer.
  - *Specificity beats intent.* `.bar-links a:not(.btn)` outranks
    `.nav-group a`, so a menu rule never reached its links; and an exclusion
    naming a class nothing carries excludes nothing. Name the ancestor.
  - *A phone has no room for an absolutely positioned popover.* Under 44rem a
    hint is `fixed` to the bottom of the viewport and scrolls inside itself,
    and a control pinned with `margin-left: auto` takes a row of its own.
  - *A control that appears only sometimes is one you have to look for.* The
    pager shows at one page of one, both ends spent and inert: where you are
    is then something you already know rather than something you work out.
  - *Dark needs its own accent, not darker greys.* `--tick` is near-black in
    light and blue in dark; a grey wash on a black page cannot be tracked by
    eye. Both dark palettes must carry every token — a test compares them.

## Offline and the service worker

- **The service worker's cache name only moves when `__version__` does.**
  It is stamped automatically, which was the fix for forgetting to bump it by
  hand — but a stylesheet or script edit inside one version is still invisible
  to every installed client, because the cache name did not change. Two bugs
  were reported off a phone that were both correct in the code and measured
  so. **Bump `__version__` with any change under `static/`**, and keep
  `pyproject.toml` in step: it said 0.3.0 while the package said 0.3.1.
- **A service worker must not answer a byte range from `caches.match`.** An
  `<audio>` element does not fetch a file, it asks for ranges, and on iOS it
  does so for every play, pause and seek. Cache matching is by URL, so the
  Range header is ignored: a request for bytes 500000-600000 came back as
  status 200 with the whole 2,437,998-byte file. The element read the first
  byte it got as the byte it had asked for, so the clock and the audio parted
  company and every seek landed a few blocks late. `cache.put` refuses a 206
  as well, so the write silently rejected and nothing ranged was ever stored.
  `sw.js` now stores whole files and slices one into a real 206.
- **Parse the Range before reading the body, not after.** `sliceRange` read
  the whole response to measure it and *then* looked at the header, so its two
  "give up and hand back what we were given" paths — an unparseable header,
  and a bare `bytes=-` — returned a Response whose body was already consumed.
  The fetch failed outright rather than falling back to the whole file.
- **Slice a Blob, not an ArrayBuffer.** `arrayBuffer()` reads a whole section
  into memory to hand back a slice of it, and iOS asks for a range on every
  play, pause and seek: scrubbing a 22-minute section copied 4.6 MB per drag.
  `blob.slice` is lazy — the browser leaves the body where it is.
- **Clone a Response synchronously or not at all.** The page starts reading
  the response the moment the handler returns, and a Response cannot be cloned
  once its body is being consumed. Deferring the clone into the `caches.match`
  callback, to avoid cloning pages that are not held offline, is a saving that
  does not work: it has to be taken up front and thrown away unused.
- **The shell must precache what the reader actually needs.** media-chrome
  owns the play button, the scrub bar and the skip buttons, and it was left to
  be picked up opportunistically on the first reader page. Installing the app
  and marking an article offline before ever opening one gave you an offline
  article with no controls — the one case the feature exists for.
- **An offline navigation must answer with something.** `respondWith` rejects
  if it is handed `undefined`, and `caches.match` resolves to `undefined` for
  anything never stored, so an offline visit to a page the user had not marked
  fell through to the browser's own network error. There is a 503 page that
  says which it is.

## Summaries and their keys

- **A summary must be written before the audio, never after.** It is a block;
  inserting one at the head of a section moves every id after it, and the
  timing map is keyed by id. `_summarise` writes the blocks and puts the
  article back to `new`; `claim_job` had marked it `building`, and nothing
  else would have cleared that.
- **A failing section must not take the ones that worked with it.**
  `summarize_article` catches each call on its own and calls back per section,
  so the worker can store what arrived. With `replace`, a section whose new
  call fails keeps the summary it already had — otherwise a rate limit would
  delete text the model had written earlier and not replace it.
- **A summary job's progress needs the job, not the article status.**
  Summarising leaves `article.status` alone by design, and both the reader's
  status card and its poller keyed off that status. So a summary pass showed
  no progress, no result and no error: the page never even loaded
  `progress.js`. Both read the job now.
- **The summaries key field posts blank when untouched.** The form carries
  `keep_key`, or saving a new model name would wipe the stored key.
- **A key is a named thing, not an endpoint's.** Keyed by endpoint there was
  room for exactly one Gemini key, so a second account had nowhere to go —
  and a gateway of your own may front several. `summary_key` holds one row per
  key: `name` (the primary key, and what the model picker offers), `provider`,
  `base_url`, `api_key`, and the model it was last used with. Two rows may
  share a provider. `setting` holds only what is *in use*:
  `summary_credential` names the row.
- **A listed provider's address lives in the code, not the row.** `Credential.
  endpoint` reads `PROVIDER_ADDRESSES[provider]` and falls back to `base_url`,
  which is filled in only for a custom one. So a provider that moves its
  endpoint moves every library with it. The override follows the same bargain
  the reading pace makes with 1.0: `save_config` stores `summary_base_url`
  only where it *differs* from the chosen key's own address, so an override
  sticks and everything else stays live.
- **Typing a key and choosing one are two acts.** They were one form, so
  saving a model posted the key field too — which is why `keep_key` had to
  exist, and why a blank box could wipe a key. Two boxes, two routes:
  `/summaries/key` stores a named key and touches nothing else, `/summaries`
  sets the key in use, the model, the endpoint and the prompt and cannot reach
  a key at all. A blank key on an *update* keeps the one already stored,
  because a password box posts empty when untouched; on a new entry it is
  refused unless the endpoint is on this machine.
- **Forgetting the key in use must stop using it.** Otherwise the config names
  a row that is gone, `api_key` is empty and nothing says why.
- **A local endpoint needs no key, and still needs a name.** Ollama and LM
  Studio are not behind an account, but the model picker offers names, so they
  are named keys with an empty key. `Config.ready` asks for a key only when
  `needs_key`, which is any host not in `summarize.LOCAL_HOSTS`, and `_client`
  sends "not-needed" because the OpenAI client refuses to be built without a
  string.
- **The model is typed, and there is no list of model names anywhere.** It was
  a select of each provider's models. A list of model names kept in a file is
  a promise to chase every release, it is wrong within weeks, and the name you
  want sits behind its last option. Worse, changing provider replaced a typed
  name with the list's first entry, which silently overwrote a model chosen on
  purpose. `PROVIDERS` is addresses only — twelve, each checked against the
  live endpoint. Gemini answers 404 on `/models` and 400 on
  `/chat/completions`, the path the app uses, so it is right and so are the
  others.
- **The prompt is user-editable, so `format` is the wrong tool.** `str.format`
  reads every brace in the string: a prompt asking for JSON, with a
  `{"summary": "..."}` example in it, raised `KeyError` and every section of
  every article failed with that as the reason. `{text}` is the only
  placeholder there has ever been, so `str.replace` substitutes it and leaves
  the rest alone.
- **`.../v1` and `.../v1/` are one endpoint.** `endpoint_id` lower-cases and
  drops the trailing slash. It no longer names a key, but the migration
  matches an old key's endpoint against `PROVIDERS` with it.
- **The page carries the tail of a key, never a key.** Four characters, which
  is enough to tell two keys apart and not enough to use one. Anything in the
  page is readable by anyone at the page.
- **A summary block carries no origin.** One written by hand is the same row
  as one written by a model, so the model is recorded on the *job* —
  `service.summarize` puts it in `options` and `db.last_summary` reads back
  the last one that finished. An article with summaries and no record was
  either summarised before this existed or written by hand, and the reader
  says nothing rather than naming a model that did not write it.

## The build worker and its child

- **A sqlite3 connection belongs to the thread that opened it.** The build's
  progress callback runs on the render pool, not on the worker thread. Passing
  the worker's connection in failed *every* parallel build with
  `ProgrammingError`, and the job simply recorded itself as failed. Call
  `db.connect()`, which is thread-local, from inside any callback.
- **Mail polling stays in the parent.** `imaplib` is the standard library and
  costs nothing to keep. Only the work that drags a C extension in is worth a
  process boundary.
- **Spawn the child, never fork.** The worker runs a thread per lane,
  and forking a process with threads in it is a way to deadlock in the child's
  first allocation. `_start_job_process` asks for the `spawn` context.
- **The parent must stay clean.** The whole benefit is that the worker never
  imports torch, so nothing in `jobs.py`'s import list, or in a lane's path,
  may reach the engine. `audio.py` is safe — it imports numpy, and the engine
  import lives inside `KokoroEngine.__init__`. Check `/proc/<pid>/maps` for
  `torch/lib` if you are not sure.
- **`stop()` has to terminate the child.** Nothing else reaps it. watchfiles
  restarts the worker on every edit under `src/`, and an orphaned build would
  hold the model and go on writing progress to a job it no longer owns.
- **A killed child leaves a job marked running.** The child records its own
  failures, but not an OOM kill or a SIGKILL. The parent checks `exitcode` and
  calls `_requeue_orphans`, which is the same repair that runs at start-up.
- **The child installs the settings it was handed.** `use_settings` puts them
  in the process-wide slot, so a render reaching for `get_settings()` sees what
  the parent queued the job with, not a second reading of the environment.
- **Freeing a model does not lower RSS on its own.** Dropping the references
  returns the memory to the allocator, not to the operating system: glibc keeps
  its large arenas mapped. `jobs._trim_heap` calls `malloc_trim(0)` after the
  pool is released, and without it the idle unload showed almost nothing.
  Note the order it needs — `gc.collect()` first, because the pipelines hold
  cycles and the weights are not unreachable until the collector has run.
- **A released job must be skipped, not just released.** `claim_job` takes
  the oldest queued job, so a build process that puts back a job for the
  other engine would claim the same one again on its next turn and never
  reach the work behind it. `drain_jobs` carries a `skip` set for the life of
  the process, and `step` returns True for a released job: there is still
  work in this lane, just not this process's work.
- **Releasing a job must put the article back too.** `claim_job` sets
  `article.status` to `building` in the same transaction, and a job handed
  back with the article still building is an article that shows as building
  with nothing building it. `db.release_job` undoes both.
- **`release_shared` checks identity, not just the name.** The worker publishes
  its first pool instance into `tts._shared`. When it drops the pool it must
  take that entry back, but only if the slot still holds *its* engine — a web
  process that built its own must not lose it.

## The block cache

- **The block cache key must carry every knob that changes the audio.** Voice
  and reading pace are in it. A pace of 1.0 leaves its field empty, so
  everything cached before the setting existed is still a hit.
- **The build is when the cache is collected, not a timer.** Every orphan is
  made by a rebuild: an edited paragraph, a re-parse, a new pronunciation rule
  or a changed voice all reach the audio only through one, so the moment a
  build finishes is the moment the old keys become garbage and the new ones
  are certain. `Worker._sweep_cache` runs in the *parent*, after the child has
  exited and given its memory back. Measured over 609 blocks: about a second,
  nearly all of it re-deriving the spoken text, against a build that takes
  minutes. A killed build is not followed by a sweep — its jobs go back on the
  queue, so their renders are still wanted.
- **The sweep lives in `cache.py`, not `service.py`, for one measured reason.**
  The parent must stay small, and importing `service` beside `jobs` took it
  from **38 MB to 45 MB**: `service` drags in requests and the parsers.
  `cache.py` imports only what `jobs` already has, and the parent is still
  38 MB. `service` re-exports the names, because the reader and the tests
  reach for them there.
- **Nothing collected the cache, and 43% of it was unreachable.** A key is a
  hash, so a rule change, a text edit, a re-parse or a deleted article each
  left its old render behind and nothing ever overwrote it. Measured before
  the sweep existed: 363 of 691 files, 0.96 GB. `service.sweep_cache` computes
  what every article in the library still wants and deletes the rest — over
  the whole library at once, which is what makes it safe, because a render two
  articles share is kept while either wants it.
- **`cached_renders` asked for the wrong engine.** It hashed with the literal
  `"kokoro"`, right when there was one engine and wrong for every article
  since. Thirteen of fourteen here are kokoro-onnx, so "Delete audio" named
  files that did not exist and removed nothing — and for an article built
  under kokoro once and rebuilt under ONNX, it named the *old* files and
  deleted those, leaving the ones in use. It reads the engine off the
  article's build options now, and passes the g2p flags, because a rule
  written in IPA reaches one phonemiser and the spoken text is not the same
  string on both.
- **A cached render can belong to two articles.** Two pieces quoting the same
  paragraph, under the same engine, voice and pace, are one file.
  `cached_renders` subtracts every key another article still wants, or
  dropping one article's audio would silently cost another its cheap rebuild.
- **The cache holds int16; the caller still gets the model's floats.**
  `_speak` returns the freshly synthesised samples, not the round trip. Only
  a *later* build reads the quantised copy, which is where the −90 dBFS was
  measured.

## Data, time and export

- **A title is not a file name.** `_export_name` used the title alone, so two
  articles sharing one — a newsletter that names every issue the same is the
  ordinary case — wrote two entries of one name into the zip and the reader
  kept whichever the archiver picked. The slug is what tells two articles
  apart, so the second one carries it, and the map is keyed by slug so an
  article whose files are yielded one at a time keeps one name for all of them.
- **A form part may carry no filename.** `upload.filename.lower()` sat outside
  the batch loop's `try`, so `None` was an unhandled 500 that cost the whole
  batch — the one thing a batch promises not to do. The read and the naming
  are inside the try with the parse.
- **A LIKE wildcard in a rule's pattern is not a correctness bug.** `%` and
  `_` only ever *widen* a LIKE, and the regex has the last word, so the answer
  stays right. `db._like_literal` escapes them to keep the narrowing worth
  doing: an unescaped `%` in "100%" matches every block holding "100", and a
  rule whose pattern is a bare "%" would hand Python the whole library again.
- **A stored time is UTC and a shown time is not.** `db.now()` writes
  `datetime.now(UTC).isoformat`, which is the only sane thing to store. The
  Builds page printed it raw, so a build that started at 16:41 in Dubai read
  `2026-09-03T12:41:19+00:00`. The zone cannot be a setting either: one
  library is read from a phone abroad and a laptop at home. The `when` filter
  emits `<time datetime="...">` and a script in `base.html` formats it with
  `Intl.DateTimeFormat` in the browser's own zone and locale — measured at
  `3 Sep 2026, 04:41 PM` under `Asia/Dubai` against `3 Sept 2026, 12:41`
  under UTC, from one page. The fallback text names UTC rather than
  pretending, because a wrong-looking time is worse than an honest one.
- **A bare date must not be moved into a zone.** `published_at` often has no
  time in it, and formatting `2026-09-03` as an instant lands it a day early
  west of UTC. `when` converts only a value with a `T` in it.
- **A migration must delete what it consumed.** Left behind, it is a second
  answer to the same question. The summary-key steps in `migrate.py` run in a
  fixed order and the tests run them in that order too.

## Web, auth and the bookmarklet

- **A refused Shortcut looks exactly like a success.** iOS Shortcuts' "Get
  Contents of URL" runs, gets a 401, and the shortcut carries on. Nine
  identical 401s from a phone sat in the app's log while the phone reported
  success each time. The recipe adds **Show Result** now, and the recipe's
  credential is a `token` form field rather than an `x-textcast-token` header:
  the header name is the part you type by hand, and an iPhone rewrites a
  hyphen as a dash. The header still works, for a shortcut already built.
- **`uvicorn`'s access log is the first place to look, not the last.** "It said
  it worked and nothing arrived" was three different bugs until the log showed
  `POST /api/ingest 401`, which named the one it actually was. `docker compose
  logs -t app | grep "POST /api/ingest"` gives every attempt with a timestamp.
  Do not trust `--since`: it returned nothing for a window that plainly held
  the lines.
- **A cross-site POST does not carry the session cookie.** It is
  `SameSite=Lax`, which a browser sends on a top-level GET and never on a POST
  from another origin. So the bookmarklet — the only way in that is cross-site
  — carries the token in a hidden `token` field, and `require_auth` reads it.
  Loosening the cookie to `SameSite=None` was the one-line alternative and is
  the wrong one: every POST route, `/api/articles/<id>/delete` included, would
  then accept a request from any page on the internet with your session on it.
- **Reading the form in the auth dependency is safe, and only just.** Starlette
  caches the parsed form on the request, so FastAPI's own parse for the
  endpoint reuses it rather than finding a drained stream. Checked on
  `multipart/form-data` as well as urlencoded, because a file upload is the
  case that would have failed. The check is skipped unless the request is a
  POST with a form content type: nothing else has a body worth reading.
- **A token in the body must hand back a cookie.** `/api/ingest` answers with a
  303 to the new article, and that GET carries no credential of its own.
  Without `set_session_cookie` the bookmarklet ingested the article and then
  showed you a login page.
- **`--proxy-headers` or the cookie loses `Secure`.** Behind Caddy the app sees
  plain HTTP, and `request.url.scheme` is what decides the flag. Measured: with
  the flag the live host sets `Secure`, without it does not. The trust list is
  open because the port is not reachable from the internet.
- **The bookmarklet cannot post from https to http.** A browser upgrades a
  form POST from a secure page to an insecure one, so a paywalled article sent
  to a plain-HTTP textcast went to an https address that answers nothing —
  which read as the bookmarklet redirecting somewhere wrong. The bookmarklet
  now says so instead of failing silently, and the real fix is TLS in front of
  the app. See "Still open".
- **The bookmarklet's address is baked in, and must be the public one.** It is
  dragged to a bookmarks bar once and kept for months, so `location.origin` of
  whichever request drew the Add page is the wrong source: behind a reverse
  proxy that is the proxy's back end. `web.public_origin` prefers
  `TEXTCAST_PUBLIC_URL` and falls back to the request. The iPhone Shortcut
  reads the same value.
- **What the bookmarklet sends is not what the share sheet sends.** The
  bookmarklet posts `kind=html` with `document.documentElement.outerHTML` —
  the page *your* browser rendered, with your session applied — so the server
  parses HTML it never had to fetch. The share sheet and the Shortcut post
  `kind=url`, and the server fetches that link as a stranger. Only the first
  gets past a paywall.

## Docker and the image

- **The build does not run the model.** `docker/bake_model.py` only calls
  `snapshot_download`. It must stay that way: a build assembles an image, it
  does not do the app's work.
- **`en_core_web_sm` must stay pinned.** Kokoro pip-installs a spaCy model at
  first synthesis otherwise, which needs network and write access to the venv.
  Both are wrong in a container.
- **Named volumes inherit root ownership.** `/data` is created and chowned in
  the image *before* `VOLUME`, or an unprivileged container dies on `mkdir`.
- **Do not pin `ESPEAK_DATA_PATH` in the Dockerfile.** It was set to the arm64
  path, which is wrong on an amd64 image. `tts/kokoro.py` probes both, plus
  Homebrew's prefixes, and warns when it finds nothing.
- **One base image for both build stages.** The builder was
  `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` and the runtime
  `python:3.12-slim-bookworm`. Those are two different CPython builds: the
  venv recorded `version_info = 3.12.12` and ran on 3.12.14. It worked only
  because both put python at `/usr/local/bin` and the ABI is stable across a
  patch release — a minor-version drift on either side would have broken it
  without a word. The builder is the runtime image now, with the `uv` binary
  copied in, which is what uv's own Docker guide asks for. No size change.

## Tests

- **The player tests share one page.** A test that pauses the audio breaks a
  later one that assumed it was playing. State a test depends on, do not
  inherit it.
- **A test that reads pronunciation rules must ask for a database.**
  `test_rewrites` took no fixture, so its "vs.", "approx." and "YoY" cases read
  whatever `settings.db_path` happened to point at. It passed only while some
  earlier test had left a seeded one behind, and failed the moment the tests
  either side of it changed. It takes `conn` now.
- **Sync Playwright allows one instance per thread.** `tests/test_player.py`
  has one module-scoped `browser` fixture; a test needing isolation takes a
  context from it. A second `sync_playwright()` does not fail loudly — it
  raises inside the `chromium unavailable` guard and the test silently skips.


