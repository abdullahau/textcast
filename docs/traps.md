# Traps

Things that have bitten once. Each says what broke, so the rule is believable
and nobody spends that afternoon twice.

Not in `CLAUDE.md` because that file is read at the start of every session and
this one is only wanted once you are in the code it describes. **Read the
section you are about to touch**, and add to it when something bites you.

## Parsing: the DOM, the adapters and the visuals

- **`drop` used to beat `keep`, and beat it destructively.** The drop rules ran
  first, as CSS over the whole container, with no depth limit, and
  `decompose()`d what they matched. `keep` is asked per node afterwards and
  only reaches six ancestors — so by then the picture was off the tree and
  could not be rescued. Every adapter writes a short keep list and a long drop
  one *because* keep is meant to be the authority. Substack showed it:
  `.pencraft img` was written for an author's face, Substack now wraps the
  whole post body in a `pencraft` layout div ten levels above every picture,
  and so every post came out as unbroken prose. `visuals.drop_furniture` skips
  a node any keep rule claims. Changing it moved nothing in `tests/corpus` —
  check that before touching it again.
- **A corpus file does not prove the live page still parses.** The one fixture
  carrying `pencraft` keeps its figures, because it was saved before Substack
  moved the class up the tree. The fixtures are a guard against regression,
  not evidence that a publication's markup has held.
- **`requests` reads a page as ISO-8859-1 whenever the server names no
  charset.** That is the old HTTP/1.1 default and it is almost never true;
  Semafor sends a bare `text/html`, so every curly quote arrived as the three
  characters its UTF-8 bytes spell in Latin-1 — an opening quote read back as
  "a-circumflex, euro, oe". `service.fetch` asks the body instead, and only
  where the server declined to say: a charset that *was* declared is believed,
  or a page of real Latin-1 would be re-read as UTF-8.
- **`zipfile` never hands back more than the directory declared.** A `.docx`
  is a zip, and the plan against a bomb was to sum `info.file_size` — the
  file's own numbers. Rewriting them down does not buy a way past the cap:
  `ZipExtFile` stops at the declared size and then fails the CRC, so a lying
  directory is refused rather than believed. `_unpacks_over` counts what
  really comes out anyway, so the guarantee is the code's and not CPython's.
- **`node.css()` searches the node *and* its subtree.** So a `<table>` asked
  whether it holds another table says yes about itself, and `blocks_from_dom`
  turned a whole article into one figure the moment
  `article-grid--no-full-width-graphics` matched `[class*="graphic"]`.
  `css_matches` and `any_css_matches` are no help — both are true when a
  *descendant* matches, which is how every wrapper on an FT page "matched"
  `.o-table`. The only self-only test is `node.css_first(sel) == node`.
- **Two lexbor nodes for one element are not `is` each other.** lexbor hands
  out a fresh wrapper per lookup; they compare equal by `mem_id`. Every
  ancestor walk and every `stop=container` guard uses `==`. `ancestor_tags`
  had the identity bug from the start and walked past its stop to the root.
- **A srcset is not quite comma-separated.** Substack serves pictures through
  Cloudinary, whose path is `w_1456,c_limit,f_webp`, so `split(",")` cut one
  candidate into three. The separating comma has whitespace after it; a comma
  inside a URL never does. Reading the srcset is worth it twice over: the
  widest candidate is the best copy, and on a saved page it is the only
  *absolute* address — the browser rewrote `src` to the `_files` directory.
- **`figure` was in both noise lists, and that dropped every chart.** FT and
  Bloomberg each opened with `"figure"` as a blunt way of saying "teasers,
  promos and headshots". Those are named directly now.
- **A `keep` rule must not outrank the junk filter from six levels up.**
  Matched loosely, `VisualRules.keep` rescued the FT's event promo, because an
  ancestor of the promo also contained the article's `o-table`. Both walks are
  depth-capped and stop at the container.
- **`©\b` never matches.** Neither the symbol nor the space after it is a word
  character. `BARE_CREDIT` puts the `\b` on the words only.
- **A cell may claim any span it likes.** The FT's table footer is
  `colspan="1000"`, meaning "the whole row". Capped at 12, and `tfoot` goes to
  `media["foot"]` rather than into the rows: it is a credit line.
- **`extract.STRIP` removed the visuals before the walk saw them.** `figure`,
  `figcaption` and `iframe` were on it, so giving the catch-all adapter a
  `VisualRules` did nothing, silently. `visuals.py` refuses the furniture.
- **A publication's recirculation is real headings with real text under it.**
  The Economist ends every leader with "Explore more", "From the <date>
  edition" and the week's other headlines; the FT ends with "FT alerts".
  Nothing about them looks like junk except where they sit, so the Economist
  walk *stops* there rather than pruning afterwards, and `JUNK_SECTIONS` knows
  the headings by name for everyone else.
- **Small capitals split the word they are inside.** The Economist sets them
  with `<small>`, mid-word: `Chat<small>GPT’</small>s` came out "Chat GPT’ s"
  and a drop cap `<span data-caps>N</span><small>VIDIA</small>` came out
  "N VIDIA" — read aloud that way. Unwrapping is not enough on its own: it
  leaves two text nodes side by side, and the separator goes between text
  *nodes* rather than between elements. `merge_text_nodes()` is what rejoins
  the word.
- **A dek can be a heading in the markup.** The Economist's is an `<h2>`
  straight after the `<h1>`, so a density walk reads it as the first section's
  title and the article's own title is never used for one. Read it into
  `subtitle` and drop the element before walking.
- **Not every publication prints a byline.** The Economist does not sign its
  leaders. `test_every_page_in_the_corpus_has_a_byline` exempts it and asserts
  the opposite, because a name found there came from somewhere it should not.
- **A publication puts its byline in the head, not the body.** Bloomberg names
  the writer in `parsely-author`, `sailthru.author` and `author`, and again in
  a byline whose class carries a build hash. The meta tags are the stable read.
- **A reader library raises its own errors, not yours.** A file named `.pdf`
  that is not one made pypdf throw `PdfStreamError` through to the browser as
  a 500. The readers raise `UnsupportedDocument`, and the batch loop catches
  broadly on purpose: one bad file must not cost you the other nineteen.

## Re-parsing, and editing an article

- **The slug is an address, and a re-parse must not move it.** It is derived
  from the title, and `reparse` deletes the row and stores a new one — so a
  better title meant a new slug, and `media/<old-slug>/`, its `images/` and
  `sources/<old-slug>.*` were left with nothing able to reach them. Nothing
  sweeps by slug. `save_article` takes the old slug back, and the audio that
  the moved block ids invalidated is dropped rather than orphaned. What a slug
  *reads* is of no interest to anyone; what it points at is.

- **Re-parse used to delete every summary in the library.** A summary is a
  block and no source ever held one — 35 of them, seven articles, each a call
  to a model, and the only sign was ~500 words vanishing per article. They are
  filed by section title and put back at the head of the section. The title is
  the only handle: a section the parser fix renames loses its summary, and
  `Ingested.summaries_lost` says how many rather than staying silent.
- **`media["file"]` is bookkeeping and must not be compared.** The picture
  fetch writes it after `store`, so the stored copy always has one and a fresh
  parse never does. Anything diffing the two must drop it first, or every
  article with a picture looks changed for ever.
- **An audio wipe must ask what it is deleting.** Both wipes did
  `for child in media.glob("*"): child.unlink()`, which is fine until
  `images/` is beside them — `unlink` raises on a directory, and the pictures
  would have gone with audio that can simply be rebuilt. Files only now;
  `service.delete` is the one that takes the lot.
- **A re-parse remembers nothing, so the disk has to.** No rebuilt block
  carries the `file` the last parse stored, so without a check against the
  directory re-parsing the library re-downloads every picture to write bytes
  already there. `already_stored` globs for the address's hash.
- **`replace_blocks` must rewrite the sections too.** It rewrote the blocks
  and left the `section` rows alone, and `load_article` seeds sections from
  that table without asking whether any block points at one — so editing the
  last block out of a section left its heading standing over nothing.
  "Explore more" and "FT alerts" survived that way on two articles.
- **Editing text is cheap; removing a block is not.** Text edits leave the ids
  alone, so the audio and the timing map stay valid. Removing one moves every
  id after it, so the audio goes. The block cache is deliberately kept: keyed
  by text, so the rebuild is an encode.
- **Deleting summaries must not call `delete_audio`.** It did, and that also
  takes every render only this article wants — so dropping one summary sent
  every *other* block back to the model, minutes of synthesis to undo a
  paragraph. `_drop_media` is the shared piece; keeping the cache is the
  separate decision it always was.

## Pronunciation, phonemes and the rules

- **A rule has three replacements, all optional.** `replacement` is plain text
  and reaches every engine. `misaki` and `espeak` are IPA, one field per
  phonemiser. Each engine takes the IPA written for *its* phonemiser if there
  is any and the plain replacement otherwise; a rule with neither does not fire
  there at all.
- **Why two IPA fields.** `pronounce.py` emits `[word](/ipa/)`, which is
  *misaki's* markup — misaki reads and removes it, espeak reads it aloud:
  `[LIBOR](/lˈIbɔɹ/)` became "libber slash el stress eye bee open-or turned-ar
  slash", 3.71 s of audio for two words. The notations differ too: misaki's
  capital `I` is the /aɪ/ of "eye", which espeak spells `aɪ`.
- **`is_phonemes` is derived, not stored.** `bool(misaki or espeak)`. As a
  column beside the fields it describes it could disagree with them.
- **An engine taking no phonemes gets only the other rules.**
  `accepts_phonemes = False` makes the normaliser drop phoneme rules rather
  than hand over markup something would read aloud. Nothing ships with it; the
  test sets it, because that is the failure it exists to stop.
- **`TTSEngine` is `runtime_checkable`, so its body is a contract.** `g2p` and
  `accepts_phonemes` are deliberately *not* declared: naming an attribute in a
  runtime-checkable Protocol makes `isinstance` demand it, and every engine and
  test double would have failed. `tts.g2p_of` reads them off whatever it is
  given and answers "misaki" for anything silent.
- **A respelling cannot be aimed at one engine.** It is ordinary text, so it
  reaches both, and every rule added for one must be measured on the other.
- **A migration that raises takes the whole deployment down.** `migrate.run` is
  called from `db.init`, which the app's lifespan and the worker's `start` both
  await, so an exception there is a crash loop in two containers at once and
  the site answers Cloudflare 524 rather than anything you can read. That is
  the shape of the failure: not a broken page, not a 500, but nothing at all
  from either service, and no log past the traceback.
  `_fix_case_sensitive_respellings` did it — it renamed a stored pattern onto
  one a seed pass had already added beside it, and broke
  `UNIQUE (kind, pattern)`. The library that hit it was a dev box that had run
  an intermediate tree: the new patterns were seeded at 19:28, the migration
  written at 19:35. **The migration was deliberately left simple**, and that
  one database repaired in place instead — a rename that assumes the target is
  free is right for every library that came from a release. Restart an
  upgraded system and read the app's log before assuming the network.
  The useful cases are where the good engine does not move: "Solamon" leaves
  misaki's `sˈɑləmən` alone and fixes espeak's doubled vowel. When no spelling
  does both, a phoneme rule with two spellings is the tool.
- **The pre-filter tests the running text, not what `apply` was given.** A rule
  may write a word a later rule matches, so the lower-cased haystack is
  refreshed whenever a rule fires — rare, so it costs nothing. Lower-cased on
  both sides whatever the rule says about case: it only has to be a *superset*
  of what the pattern would match, and a rule past it still meets its guards.
- **`_prepared` is keyed on the rules, not on the list.** Keyed on the list, a
  rule edited on the Voice page would go on speaking with the wording it had at
  process start. `Rule` is frozen, so the tuple is the key.
- **Seeding is recorded, not inferred.** Skipping whenever the table had
  anything in it kept a deleted rule deleted but also meant a rule added in a
  later release never reached an existing library. `db.SEEDED_KEY` holds every
  built-in ever offered, so both hold at once. A library from before the record
  takes its current rules as the baseline, so a built-in deleted back then
  comes back once — nothing can tell it apart from one never shown.
- **Built-in patterns must be unique per kind.** The seed's `ON CONFLICT`
  silently overwrites, which once turned `REIT` into "R E I T".
- **`preview()` applies rules in sequence**, not each against the original, or
  `vs.` and `vs` both appear to fire when only the first does.
- **A month rule needs a number beside it.** `Jul 2` and `2 Jul` are dates;
  `Julian`, `March` and a colleague called Jan are not. The leading form may
  swallow its abbreviation dot; the trailing form may not, or it eats the
  sentence's full stop.
- **`phonemizer words count mismatch` is information, not an error.** espeak
  returned a different word count than the token it was given, and it is
  usually right (`JPMorgan`, `DeFi`, `McKinsey`). Still worth grepping for: it
  is the only signal something reached the engine that should not have, and it
  is how the stray emphasis markers were found.

- **A stored replacement is stripped.** `db.add_pronunciation` calls
  `.strip()` on its way into the table, so a rule whose replacement is `" dot "`
  comes back as `"dot"` and "Pets.com" is read "Petsdotcom". Capture the
  characters either side and write them back — `([A-Za-z0-9])\.(com|org)` to
  `\1 dot \2` — rather than relying on space in the replacement.
- **espeak keeps a full stop as a clause break, wherever it is.** The phonemes
  for "Pets.com" are literally `pˈɛts.kˈɑːm`, so the reader stopped
  mid-sentence as though the sentence had ended. misaki does the opposite and
  runs the two together into `pˈɛtskˌɑm`, "PETS-kom". Neither is a
  pronunciation problem in the ordinary sense and no respelling of the *word*
  reaches it; the dot itself has to go.
- **A rule that eats a dot must prove it cannot eat a full stop.** The domain
  rule is safe only because of its trailing `(?![A-Za-z0-9])`: without it,
  "closed in 1999.Companies rushed in" — a missing space, which is common in
  scraped text — is read as a website. Keep the guard if you add an ending on
  the Voice page.

## The TTS engines

- **The ONNX engine does misaki's job itself.** It phonemises with espeak,
  splices the rule's phonemes in, and hands the whole string to the model as
  phonemes. That works because both engines share one 114-symbol vocabulary,
  misaki's capitals included. `The LIBOR rate rose` is `lˈɪbɚ` untouched and
  `lˈaɪbɔːɹ` with the rule.
- **Both engines offer the same voices under the same names** — same ids, same
  order. Every picker shows one engine's voices at a time and the select above
  says which; an `(ONNX)` suffix repeated on twenty lines what one control
  already said. All four pickers read one `_voices_by_engine` payload.
- **Engine availability probes the dependency, not the wrapper.** The heavy
  import is inside `__init__`, so `import textcast.tts.kokoro` always succeeds.
  `is_installed` checks `spec.requires`.
- **Three warnings fire on every engine start and none is ours.**
  `weight_norm` is deprecated and kokoro is built on it; kokoro's config asks a
  one-layer LSTM for dropout; thinc, under spaCy, under misaki, calls
  `torch.jit.script`. `_quiet_known_warnings` drops those three by exact text.
  Do not widen it — see the phonemizer warning above.
- **Nothing may load a model inside a request.** Every article page called
  `_voices()`, which built an engine, so the first load after a restart waited
  for the weights and the player tests were flaky. `tts.catalogue` reads the
  voice table from the wrapper module; `_phonemes_for` uses `tts.loaded_engine`,
  which never builds one. `/api/say` may build — the user asked to hear it.
- **The KModel registry is weak on purpose.** A strong `tts.kokoro._models`
  would be the single thing keeping 312 MB of weights alive after the last
  pipeline had gone.
- **Building a second pool beside the first is how you reach 7.5 GB.**
  `tts._shared` is a *strong* dict, so the old pool's first instance pinned its
  session for the life of the child. One queue that switched engine mid-drain:
  1.8 GB on ONNX, 3.5 GB once the kokoro pool loaded, peak **7,508 MB**. No
  process loads two engines now, and `engines_for` raises rather than building
  a second pool.

## The player and the read-along

- **A block is a `<p>` or a `<figure>`, and never a `<button>`.** They were
  buttons once and every attempt to select a paragraph started playback.
  Seeking is the gutter handle. The old test asserted every block was a `P`,
  which was a proxy for the thing that matters: a block that is itself a
  control swallows the click that would have selected a sentence.
- **`seeked` is not "ready to play".** It means the playhead moved, not that
  anything is decoded there — `play()` on it runs the clock in silence and the
  first word or two is gone. Wait for `readyState >= 3`, with a timeout so a
  missing event never blocks playback. `preload="metadata"` makes this likely
  on the first deep seek.
- **Not every seek comes from this file.** media-chrome's skip buttons and
  scrub bar set `currentTime` themselves; only the `seeked` listener on the
  element sees them.
- **`activeCues[0]` is the cue that is *ending*.** At a boundary the browser
  reports both, ordered by start time, so the highlight sat a block behind
  after every seek. Take the last. And `cuechange` fires only on a *change*, so
  a track loading with a cue already active highlights nothing until the next
  boundary — `syncHighlight` runs on track load and after every seek.
- **Never resume playback on a bare `seeked` event.** It is asynchronous, so an
  armed listener fires on whatever seek happens next, including the user's.
  `seekWithin` checks the playhead landed where it asked.
- **Removing a `<track>` does not cancel its pending `load`.** Two quick taps
  on next leave the first section's handler still queued; it fires afterwards
  and binds `track` to a TextTrack no longer on the element, so the next
  section's cleanup can never reach it and the `cuechange` backstop dies for a
  background tab. `loadSection` checks `el.isConnected` before it binds.
- **A section's place in the payload is not its index.** `build_payload` drops
  a section with no audio, so the array position and `section.idx` diverge.
- **The player must not stop on a block you asked it to jump to.** Seeking to a
  figure is already a decision to look at it. `stopToLook` fires only while
  playing, and remembers the block so pressing play carries on past it.
- **A position at the end is the end, whatever the flag says.** A decoded
  section runs a little longer than the manifest — the clock reads decoded
  audio and Opus sits on a ~1.02 s page grid — so a finished article reports a
  few seconds past its total and `duration()` rendered **"-1:59:57 left"** for
  ever. `save_position` marks anything within `end_slack` as finished, and
  `duration()` refuses a negative. The slack is a tenth of the article capped
  at five seconds: a flat five is most of a twenty-second note.
- **The player must carry `finished`, not recompute it.** Every ordinary save
  sent `finished: false`, so opening a completed article and leaving without
  playing threw the state away. Only the end of the last section sets it, and
  only pressing play clears it.
- **Clearing the position must silence the saves after it.** "Stop and forget
  my place" pauses and seeks, and both a `pause` and a `timeupdate` write the
  row — so it came straight back. A flag stops the writes.
- **A menu with no way out is a trap.** The sheet had only Escape, which a
  phone does not have. It closes on its own button and on a tap outside; the
  nav and profile menus do the same.
- **`store` takes the value third, and one call passed it second.** The
  "pause at a chart" toggle wrote the boolean `true` where the "1"/"0" belonged,
  so it was stored as the string "true" and the read compared that against
  "1". The setting saved and never came back, and the box was empty on every
  reload. The two other `store` calls in the file had it right, which is why
  nothing else lost its setting — and why no test caught it: every test that
  touched the box set it and used it in the same page load.

- **`audio.currentTime` is the decoder's clock, not the speaker's.** Whatever
  sits after the decoder — the output buffer, and over Bluetooth the codec and
  the radio — is delay. On a laptop it is around 25 ms and invisible; over
  Bluetooth on a phone it is 150–300 ms, which is a clause. That, and not
  anything in the timing map, is why the read-along looked right on a desktop
  and ran ahead of the voice on a phone. **The timing map was measured and it
  is not the fault**: over a nine-minute section, cue starts matched the
  decoded audio to within the measurement's own resolution, and the total to
  the millisecond. Do not go looking there again.
- **The browser will tell you the output delay: `AudioContext.outputLatency`.**
  It is a property of the output *device*, not of any graph, so a context with
  nothing connected to it reports the number and the audio element keeps its
  own path to the speaker. Three things it will catch you on. It reads **0**
  until the device's stream is open, so ask after playback has started and
  treat a zero as "not yet" rather than as an answer. It moves when the device
  moves, so re-ask on `devicechange` and when the page comes back from hidden —
  earbuds go in mid-article. And the context is opened, read and **closed
  again**: held open it keeps a second output stream alive for the whole
  article, which on iOS means owning the audio session the element is playing
  through. Chrome 102, Firefox 70 and Safari 18.4; older browsers fall through
  to the manual trim, which is what the slider in the sheet now is.
- **Following the audio is not the same as scrolling to it.**
  `scrollIntoView({block: "center"})` centres in the *layout* viewport, which
  on a phone is not what can be seen: the header covers the top, the player the
  bottom, and the URL bar slides in and out under both. It also ran on every
  block whatever the reader was doing, so a thumb-scroll to look ahead was
  undone by the next paragraph. `keepInView` measures the band between the two
  bars off `visualViewport`, does nothing at all while the block is inside it,
  and gives the page to a `wheel` or `touchmove` for four seconds. It is also
  called from the frame loop, not only on a block change — a block can run for
  a minute, and a scroll away from it used to leave the reader lost until the
  next one.
- **Smooth scrolling is not free on a phone.** A smooth scroll of several
  thousand pixels — one seek across a long article — runs for seconds, and the
  highlight is wrong for every frame of it. Over 2000 px, jump.
- **Space belonged to whatever had focus.** media-chrome binds its own keys,
  but only while something inside the `media-controller` has focus. So Space
  did one of two wrong things: fired again the block play button that was last
  clicked, or scrolled the page. A capturing `keydown` on the document takes
  it, `preventDefault` stops the focused button (a `<button>` fires its click
  on *keyup*, and cancelling the keydown is what cancels it), and the gutter
  handle blurs itself on click. Text fields, `<select>`, contenteditable and
  anything inside the sheet keep the key, because typing a space and ticking a
  checkbox are what Space is for there.
- **A hold with no length is a guess.** Hold-to-unlock was 550 ms and looked
  broken: it took several goes. Two reasons, and the second is the real one.
  Nothing on screen said a hold was happening or how long it had to last — so
  the bar above the player fills over exactly the time the timer waits, and the
  script writes both durations so they cannot disagree. And a thumb that drifts
  a few pixels off a 2 rem button fires `pointerleave`, which silently
  cancelled it: `setPointerCapture` on `pointerdown` is what fixed that.
  A hold ends in a `click` too, and that click must not re-lock.
- **The transport is the width of the screen, and a pocket is not.** Listening
  while doing something else on the phone lands taps wherever a thumb falls,
  and the scrub bar took every one of them. The padlock in the bar sets
  `pointer-events: none` on everything but itself, and on the play handles
  beside each block. Tap to lock, **hold** to unlock: a tap would undo it, and
  a stray tap is the thing being guarded against. A hold also ends in a click,
  so the click handler has to swallow the one that follows an unlock.

- **A `:hover` rule is a touch bug.** iOS paints the hover state on the first
  tap and activates on the second, and the hover state then sticks until you
  tap something else. Two faults from one cause: the padlock took two taps on a
  phone and one on a desktop, and "scroll with the audio" looked armed after
  any tap — its hover background is the *same* `--sunk` its armed state uses,
  so the only difference left was the arrow's brightness. Every `:hover` rule
  in `app.css` is behind `@media (hover: hover)`; a test walks the live
  stylesheet and fails on any that is not. Split a selector list that mixes
  hover with `:focus-visible` — focus has to keep working on a phone with a
  keyboard. `touch-action: manipulation` on the controls gives up
  double-tap-to-zoom, which can eat the tap outright.
- **Reading a computed style straight after a click reads the transition.**
  `button` moves `background-color` over 120 ms, so an immediate
  `getComputedStyle` returns an interpolated `rgba(244, 244, 245, 0.53)` —
  an alpha nobody authored, which reads exactly like a stuck hover and sent an
  hour after the wrong bug. Wait for the transition, or ask CDP
  `CSS.getMatchedStylesForNode` which rules actually matched.
- **A regex that merges adjacent CSS blocks will eat rules.** Merging
  neighbouring `@media (hover: hover) { … }` blocks with a non-greedy body
  pattern silently dropped twenty rules whose bodies happened to end at the
  same indent. Any bulk edit of `app.css` gets checked by loading both copies
  into a browser and comparing the flattened `selector -> declarations` pairs;
  `braces balanced` proves nothing.

- **media-chrome gives a button no width.** The height comes from
  `--media-control-height` plus the padding and the width from whatever the
  glyph happens to be, so `border-radius: 50%` drew an oval. Size the host and
  zero `--media-control-padding` under it.

## The library, search and the pager

- **A `<select>` default must match an option string exactly.** The pace was
  formatted with `%g`, writing 1.0 as "1", matching none of "0.8".."1.3", so
  the browser chose the first option and every build defaulted to 0.8x.
- **What someone types is not an FTS5 expression.** Raw input to `MATCH` made a
  hyphen or a trailing `OR` a syntax error — "Drug-Trial" and "roll-up", words
  in the library's own titles, returned a 500. `db.fts_query` quotes every term
  as a phrase.
- **Blocks are not the whole article.** `block_fts` covers summaries, quotes
  and footnotes because those are blocks, and can never cover the title,
  byline, publication or tags, which are columns on `article`. `search`
  substring-matches those too and puts the article hits first.
- **A pager needs one filter, not two.** The rows and the count read the same
  `db._article_filter`. Written apart, a library says "26-50 of 300" and shows
  a different 25.
- **The library is ordered by when it was added, not published.** A newsletter
  from last March, read today, belongs at the top with everything else that
  arrived today.
- **The filter form carries no page.** Changing a filter starts at page 1. The
  pager's own links carry the filters, or paging would clear them. A page past
  the end is clamped: a bookmark outlives the filter that made it.
- **A page polling `/api/jobs` must filter to itself.** The reader painted
  whatever job came back last over its own progress.
- **A message you cannot close is one you learn to read past.** Every `.notice`
  and any `data-dismissible` box gets a cross from one script in `base.html`.
  A `data-dismiss-key` box remembers the dismissal in `localStorage`, keyed by
  job id so the *next* failure shows. A running card is not dismissible.

## Layout and CSS

Each cost an afternoon once; the incident is in git, the rule is here.

- *Centring is `grid`, not `flex`.* A centred flex row centres the row, not the
  thing in the middle of it. `grid-template-columns: 1fr auto 1fr`.
- *Alignment in a `.row` is on the bottom edge*, so a taller intrinsic box
  drifts: a file input needs a fixed height, not a minimum. `.row > *` is
  `flex: 1 1 9rem`, so anything that must not stretch says so.
- *`hidden` loses to any rule that sets `display`.* A `.row` is `display:
  flex`, so `element.hidden` read true while the field showed — and a
  Playwright check of the property passed while the screenshot did not.
  `app.css` carries `[hidden] { display: none !important; }`.
- *Equal boxes are not equal ink.* A text link reserves its own padding, a
  filled button reserves nothing. Measure the boxes *and* look at it.
- *`vertical-align: middle` is a font metric*, half an x-height above the
  baseline. `vertical-align: top` plus the input's `line-height` is the answer.
- *Specificity beats intent.* `.bar-links a:not(.btn)` outranks `.nav-group a`,
  so a menu rule never reached its links; an exclusion naming a class nothing
  carries excludes nothing. Name the ancestor.
- *A phone has no room for an absolutely positioned popover.* Under 44rem a
  hint is `fixed` to the bottom of the viewport and scrolls inside itself.
- *A control that appears only sometimes is one you have to look for.* The
  pager shows at one page of one, both ends spent and inert.
- *Dark needs its own accent, not darker greys.* `--tick` is near-black in
  light and blue in dark. Both dark palettes must carry every token — a test
  compares them.
- *State the browser can work out belongs in CSS, not in a script at the foot
  of the body.* The theme knob hung off `aria-checked`, which ships "false", so
  every navigation in dark mode painted it in the light half and slid it
  across. It is placed by the same three branches as the palette now.
- *An SVG with no `width`/`height` falls back to 300×150*, which shoves a
  layout about rather than merely leaving it unstyled. Same reasoning as the
  dimensions on an article's pictures.

## Offline and the service worker

- **The cache name only moves when `__version__` does.** It is stamped
  automatically, which fixed forgetting to bump it by hand — but an edit under
  `static/` *inside* one version is invisible to every installed client. Two
  bugs off a phone were correct in the code and measured so; then the profile
  mark shipped unstyled the same way. **Bump `__version__` with any change
  under `static/`**, and keep `pyproject.toml` in step.
- **A versioned asset must be matched exactly.** `ignoreSearch: true` threw
  away the `?v=` that exists to bust the cache, and the bare `caches.match`
  searches *every* cache, so a previous build's stylesheet could answer.
  Exact first, then the network — which is what a version never seen before
  should do, once. `ignoreSearch` survives only as the offline fallback.
- **A CDN will invent cache headers if the origin does not send them.**
  `StaticFiles` sends an ETag and no `Cache-Control`, so Cloudflare applied its
  four-hour default *to the browser* and overrode the app's `no-cache` on
  `/sw.js` — and a four-hour-old worker keeps serving the last release's CSS.
  That is the whole difference between the tailnet address and the public one.
  `/sw.js` also sends `CDN-Cache-Control: no-store`, which Cloudflare reads
  first. A hard refresh "fixing" a stale page is the tell: it goes round the
  worker entirely.
- **A service worker must not answer a byte range from `caches.match`.** An
  `<audio>` element asks for ranges, and on iOS for every play, pause and seek.
  Cache matching is by URL, so a request for bytes 500000-600000 came back 200
  with the whole 2,437,998-byte file, and the element read the first byte it
  got as the byte it asked for. `cache.put` refuses a 206 too, so nothing
  ranged was ever stored. `sw.js` stores whole files and slices a real 206.
- **Parse the Range before reading the body.** `sliceRange` read the whole
  response to measure it and *then* looked at the header, so both give-up paths
  returned a Response whose body was already consumed.
- **Slice a Blob, not an ArrayBuffer.** `arrayBuffer()` reads a whole section
  into memory to hand back a slice: scrubbing a 22-minute section copied 4.6 MB
  per drag. `blob.slice` is lazy.
- **Clone a Response synchronously or not at all.** The page starts reading it
  the moment the handler returns. Deferring the clone into the `caches.match`
  callback, to avoid cloning pages not held offline, does not work.
- **The shell must precache what the reader needs.** media-chrome owns the
  transport and was left to be picked up on the first reader page — so
  installing the app and marking an article offline before opening one gave an
  offline article with no controls, the one case the feature exists for.
- **An offline navigation must answer with something.** `respondWith` rejects
  on `undefined`, and `caches.match` resolves to that for anything never
  stored. There is a 503 page that says which it is.

- **Do not name the offline cache after the build.** `SHELL` must be
  versioned — it is this release's own files. `OFFLINE` must not: `activate`
  deletes every cache that is not the current build's, so a versioned name
  threw away everything the reader had marked to keep, on every deploy,
  silently, and they found out on a train. It is only safe to keep across
  releases because every media URL now carries `?b=<built_at>`.
- **The build has to be in the media URL, or `immutable` is a lie.**
  `/media/<slug>/section-000.opus` is rewritten by every build and the path
  does not change. Weakening the header instead was tried and reverted: the
  audio element asks for byte ranges, so the browser's HTTP cache holds a
  *partial* entry, and without a long-lived header Chromium answers the
  worker's own plain GET as a ranged one — `Cache.addAll` refuses the batch
  with "Partial response (status code 206) is unsupported" and marking an
  article offline stores nothing at all. `article.built_at` is the stamp,
  written by `save_manifest` and appended by `build_payload`. Anything that
  builds a media address by hand needs it too, and a test that asserts on a
  bare `/media/<slug>/section-000.opus` is asserting on an address nothing
  uses.
- **`postMessage` to a worker is a shout into a room.** The download either
  happened or it did not, and the page had no way to know: `cache.addAll` is
  all-or-nothing and swallowed its own failure, so the box stayed ticked over
  a cache holding nothing. Every message carries a `MessagePort` now and the
  worker answers on it.
- **Nothing else can collect the cache, so the page has to.** The tick boxes
  in `localStorage` are the only record of what was asked for; the worker's
  own memory does not survive being stopped. Every page load sends
  `reconcile` with that list, which is what finally collects an article
  deleted from the library, one unticked in another tab, and every file left
  at a previous build's `?b=`.
- **"Keep offline" used to keep everything.** `mediaResponse` stored every
  200 it saw, so every section of every article anyone ever played went into
  the OFFLINE cache. Two things came of that. The cache grew without limit and
  the checkbox meant nothing, since the audio was there either way. Worse, the
  copy outlived the article: `/media/<slug>/section-000.opus` is rewritten by
  every build and the URL does not change, so a **rebuilt article played its
  old audio against its new timing map** — the read-along drifting further
  behind with every paragraph, on whichever device happened to have a service
  worker and nowhere else. A marker at `/__offline__/<slug>` now records what
  was actually asked for, and only those are stored. It is deliberately kept
  as well as `cache.addAll`, because `addAll` is all-or-nothing and swallows
  its own failures.
- **A slug is a prefix of other slugs.** `drop-article` matched on
  `url.includes(slug)`, so unticking "ai" also dropped "ai-and-the-law",
  silently, and the reader found out on a train. Match the whole path segment:
  `/media/<slug>/`.

## Summaries and their keys

- **A summary must be written before the audio.** Inserting a block at the head
  of a section moves every id after it, and the timing map is keyed by id.
  `_summarise` writes the blocks and puts the article back to `new` —
  `claim_job` had marked it `building` and nothing else would have cleared it.
- **A failing section must not take the ones that worked.** Each call is caught
  on its own and called back per section. With `replace`, a section whose new
  call fails keeps the summary it had, or a rate limit would delete text the
  model wrote earlier and not replace it.
- **A summary job's progress needs the job, not the article status.**
  Summarising leaves `article.status` alone by design, and both the status card
  and its poller keyed off it — so a pass showed no progress, no result and no
  error, and never even loaded `progress.js`.
- **The key field posts blank when untouched.** The form carries `keep_key`, or
  saving a new model name would wipe the stored key.
- **A key is a named thing, not an endpoint's.** Keyed by endpoint there was
  room for one Gemini key. `summary_key` holds one row per key — `name` (the
  primary key, and what the picker offers), `provider`, `base_url`, `api_key`,
  and the model last used. Two rows may share a provider. `setting` holds only
  what is *in use*.
- **A listed provider's address lives in the code, not the row.**
  `Credential.endpoint` reads `PROVIDER_ADDRESSES` and falls back to
  `base_url`, filled in only for a custom one, so a provider that moves its
  endpoint moves every library with it. `save_config` stores `summary_base_url`
  only where it *differs* from the chosen key's address.
- **Typing a key and choosing one are two acts.** One form meant saving a model
  posted the key field too — which is why `keep_key` existed and why a blank
  box could wipe a key. Two routes: `/summaries/key` stores a named key and
  touches nothing else; `/summaries` sets what is in use and cannot reach a key
  at all. A blank key on an update keeps the stored one; on a new entry it is
  refused unless the endpoint is local.
- **Forgetting the key in use must stop using it**, or the config names a row
  that is gone, `api_key` is empty, and nothing says why.
- **A local endpoint needs no key and still needs a name.** Ollama and LM
  Studio are named keys with an empty key. `Config.ready` asks for a key only
  when `needs_key`, and `_client` sends "not-needed" because the OpenAI client
  refuses to be built without a string.
- **The model is typed, and there is no list of model names anywhere.** A list
  in a file is a promise to chase every release, wrong within weeks, and the
  name you want sits behind its last option — and changing provider silently
  overwrote a name chosen on purpose. `PROVIDERS` is addresses only, twelve,
  each checked against the live endpoint.
- **The prompt is user-editable, so `format` is the wrong tool.** `str.format`
  reads every brace: a prompt with a `{"summary": "..."}` example raised
  `KeyError` and every section failed with that as the reason. `{text}` is the
  only placeholder there has ever been, so `str.replace` does it.
- **`.../v1` and `.../v1/` are one endpoint.** `endpoint_id` lower-cases and
  drops the trailing slash.
- **`immutable` is a promise that the bytes at a URL never change.** The
  avatar was served from a bare `/avatar` with a year's `max-age`, and that
  promise broke the moment a second photograph was uploaded: one cache went on
  serving the old picture while another had the new one, and the mark appeared
  to change as you walked between pages. The URL carries the picture's name,
  which is a hash of its bytes. Measured while chasing it: five navigations
  made five requests and none reached the server — the caching was right, the
  URL was wrong.
- **The page carries the tail of a key, never a key.** Four characters: enough
  to tell two apart, not enough to use one.
- **A summary block carries no origin.** One written by hand is the same row as
  one written by a model, so the model is recorded on the *job*. An article
  with summaries and no record was summarised before this existed or written by
  hand, and the reader says nothing rather than naming a model that did not
  write it.

## The build worker and its child

- **A sqlite3 connection belongs to the thread that opened it.** The progress
  callback runs on the render pool, so passing the worker's connection failed
  *every* parallel build with `ProgrammingError` and the job recorded itself as
  failed. Call `db.connect()`, which is thread-local, inside any callback.
- **Mail polling stays in the parent.** `imaplib` is the standard library.
  Only work that drags a C extension in is worth a process boundary.
- **Spawn the child, never fork.** The worker runs a thread per lane, and
  forking a process with threads in it deadlocks in the child's first
  allocation.
- **The parent must stay clean.** Nothing in `jobs.py`'s import list, or a
  lane's path, may reach the engine. `audio.py` is safe — it imports numpy and
  the engine import lives inside `KokoroEngine.__init__`. Check
  `/proc/<pid>/maps` for `torch/lib` if unsure.
- **`stop()` has to terminate the child.** Nothing else reaps it, and
  watchfiles restarts the worker on every edit under `src/` — an orphaned build
  would hold the model and go on writing progress to a job it no longer owns.
- **A killed child leaves a job marked running.** The child records its own
  failures but not an OOM kill. The parent checks `exitcode` and calls
  `_requeue_orphans`, the same repair that runs at start-up.
- **The child installs the settings it was handed**, so a render reaching for
  `get_settings()` sees what the parent queued the job with, not a second
  reading of the environment.
- **Freeing a model does not lower RSS on its own.** Dropping references
  returns memory to the allocator, not the OS: glibc keeps its arenas mapped.
  `malloc_trim(0)` after the pool is released, and `gc.collect()` first,
  because the pipelines hold cycles.
- **A released job must be skipped, not just released.** `claim_job` takes the
  oldest queued job, so a process putting back a job for the other engine would
  claim it again and never reach the work behind it. `drain_jobs` carries a
  `skip` set, and `step` returns True: there is work in this lane, just not
  this process's.
- **Releasing a job must put the article back too**, or it shows as building
  with nothing building it.
- **`release_shared` checks identity, not just the name.** The worker publishes
  its first pool instance into `tts._shared` and must take that entry back only
  if the slot still holds *its* engine — a web process that built its own must
  not lose it.
- **Requeueing orphans swept both lanes.** A build child killed by the OOM
  killer put *every* running job back, including the summary running beside it
  in the other lane — which then started again from the top and paid for its
  calls twice. It also stamped that article `queued`, claiming audio was coming
  for an article nobody had asked to build. `_requeue_orphans` takes the lane's
  kinds, and only a build may move `article.status`. Only `start` sweeps both,
  and there nothing is running.

## The block cache

- **The key must carry every knob that changes the audio.** Voice and pace are
  in it. A pace of 1.0 leaves its field empty, so everything cached before the
  setting existed is still a hit.
- **The build is when the cache is collected, not a timer.** Every orphan is
  made by a rebuild, so the moment a build finishes is the moment the old keys
  become garbage and the new ones are certain. `Worker._sweep_cache` runs in
  the *parent*, after the child has exited. Over 609 blocks: about a second,
  nearly all of it re-deriving the spoken text, against a build of minutes. A
  killed build is not swept — its jobs go back on the queue.
- **The sweep lives in `cache.py`, not `service.py`, for one measured reason.**
  Importing `service` beside `jobs` took the parent from **38 MB to 45 MB**:
  it drags in requests and the parsers. `cache.py` imports only what `jobs`
  already has. `service` re-exports the names.
- **Nothing collected the cache, and 43% of it was unreachable.** A key is a
  hash, so a rule change, a text edit, a re-parse or a deleted article each
  left its render behind: 363 of 691 files, 0.96 GB. The sweep computes what
  every article still wants and deletes the rest — over the whole library at
  once, which is what makes it safe, because a render two articles share is
  kept while either wants it.
- **`cached_renders` asked for the wrong engine.** It hashed with the literal
  `"kokoro"`, right when there was one engine. "Delete audio" named files that
  did not exist, and for an article rebuilt under ONNX it named the *old* files
  and deleted those, leaving the ones in use. It reads the engine off the
  build options now, and passes the g2p flags, because the spoken text is not
  the same string on both phonemisers.
- **A cached render can belong to two articles.** Two pieces quoting one
  paragraph under the same engine, voice and pace are one file.
  `cached_renders` subtracts every key another article still wants.
- **The cache holds int16; the caller still gets the model's floats.** `_speak`
  returns the fresh samples, not the round trip. Only a *later* build reads the
  quantised copy, which is where the −90 dBFS was measured.
- **`cache_keys` must read all three layers of the engine, not two.** It took
  the article's own build option and then jumped to `settings.engine`, skipping
  the default saved on the Voice page. Choose an engine there while
  `TEXTCAST_TTS_ENGINE` still names the other one and every key was computed
  for an engine no build had used — so the sweep that runs *after every build*
  deleted the renders that build had just written. The cache emptied itself,
  each rebuild went back to the model, and nothing said so: a sweep reports how
  much it freed, not whether it should have. Read `voice_defaults` once and use
  it for the engine as it already did for voice, quote voice and pace.
- **A `.part` file is nobody's, and stepping over it is not the same as
  collecting it.** The sweep skipped every `.part`, so the half-written renders
  a killed build leaves behind accumulated for the life of the library. They go
  now, but only once cold: `service.delete` sweeps from a web request, and a
  build in another process may be part-way through writing one.
- **The temporary file was named from the key alone.** The key is a hash of the
  spoken text, so two blocks with the same words are one key — a repeated
  stand-first, an identical list item — and the pool renders them at the same
  moment. Both threads had one `.part` between them: the first `replace` moved
  it, and the second raised `FileNotFoundError` and failed the whole build. The
  name carries the pid and the thread now.

- **A section was held whole three times to encode it.** `np.concatenate`
  joined the block list, `.tobytes()` copied that, and `subprocess.run` held
  the copy as well. Measured on a 30-minute section, 178 MB of float32: the
  process grew **339 MB** on the old path and **0 MB** streaming the blocks to
  ffmpeg one at a time. An article that parses into a single section is the
  ordinary case, so this was a build's peak and not a corner. The decoded audio
  is byte-identical either way — the *files* never are, because Ogg carries a
  random stream serial, so two encodes of one buffer differ too.
- **ffmpeg's stderr goes to a file, not a pipe.** Draining a pipe while still
  writing to stdin needs a second thread or a select loop; without one a chatty
  ffmpeg fills the 64 KB buffer and both processes stop for ever. A file has no
  such limit and is read back after the exit code.

## Data, time and export

- **A title is not a file name.** Two articles sharing one — a newsletter that
  names every issue the same — wrote two entries of one name into the zip and
  the reader kept whichever the archiver picked. The second carries its slug,
  and the map is keyed by slug so an article's files all keep one name.
- **A form part may carry no filename.** `upload.filename.lower()` sat outside
  the batch loop's `try`, so `None` was an unhandled 500 that cost the whole
  batch — the one thing a batch promises not to do.
- **A LIKE wildcard in a rule's pattern is not a correctness bug.** `%` and `_`
  only *widen* a LIKE and the regex has the last word. `db._like_literal`
  escapes them to keep the narrowing worth doing: an unescaped `%` in "100%"
  matches every block holding "100".
- **A stored time is UTC and a shown time is not.** The Builds page printed it
  raw, so a build started at 16:41 in Dubai read `12:41:19+00:00`. The zone
  cannot be a setting: one library is read from a phone abroad and a laptop at
  home. `when` emits `<time datetime="...">` and a script formats it with
  `Intl.DateTimeFormat` in the browser's own zone. The fallback names UTC
  rather than pretending — a wrong-looking time is worse than an honest one.
- **A bare date must not be moved into a zone.** `published_at` often has no
  time, and formatting `2026-09-03` as an instant lands it a day early west of
  UTC. `when` converts only a value with a `T` in it.
- **A migration must delete what it consumed.** Left behind it is a second
  answer to the same question. The summary-key steps run in a fixed order and
  the tests run them in that order too.

## Mail

- **A newsletter that will not parse was fetched whole on every poll, for
  ever.** `mark_seen` ran only after a successful ingest, so a message that
  parsed to nothing stayed unread and was downloaded again every cycle. An
  `IngestError` is a verdict on the *message* — it parsed to nothing, or to
  less than `MIN_WORDS` — so the next attempt fails identically; it is marked
  seen and its subject named in the log. Anything else that can be raised
  there is transient, says nothing about the newsletter, and deliberately
  leaves it unread for the next poll.
- **A failure that names no message leaves you with a mailbox.** "could not
  ingest a message" was the whole of it. `_subject` reads the header that was
  already peeked at.

## Web, auth and the bookmarklet

- **Two ports are cross-origin and same-*site*.** Same-site is decided by the
  registrable domain and ignores the port entirely, so a test that posts from
  `127.0.0.1:9000` to `127.0.0.1:8000` proves nothing about `SameSite=Lax` —
  the cookie is sent, and the assertion that it was refused passes for a
  different reason or fails for a confusing one. `tests/test_crosssite.py`
  starts Chromium with `--host-resolver-rules=MAP *.test 127.0.0.1` and uses
  `app.test` and `reader.test`, which are two registrable domains.
- **`/api/ingest` is the only route the internet can reach with a credential
  in a body, and it does real work per call.** Two budgets, and they exist for
  different reasons: failed attempts (guessing the key was free) and accepted
  calls (a leaked key should not be an open tap). The failure budget is
  checked *before* the secret is compared, so the comparison cannot be used as
  a timing oracle, and a key that works clears it — otherwise a run of real
  adds would lock the owner out of their own bookmarklet.

- **A refused Shortcut looks exactly like a success.** iOS Shortcuts' "Get
  Contents of URL" gets a 401 and carries on: nine identical 401s sat in the
  log while the phone reported success each time. The recipe adds **Show
  Result**, and carries its credential in a `token` form field rather than an
  `x-textcast-token` header — the header name is what you type by hand, and an
  iPhone rewrites a hyphen as a dash. The header still works.
- **`uvicorn`'s access log is the first place to look.** "It said it worked and
  nothing arrived" was three different bugs until the log showed
  `POST /api/ingest 401`. Do not trust `--since`: it returned nothing for a
  window that plainly held the lines.
- **A cross-site POST does not carry the session cookie.** It is
  `SameSite=Lax`, sent on a top-level GET and never on a POST from another
  origin. So the bookmarklet carries its key in a hidden `token` field.
  Loosening the cookie to `SameSite=None` was the one-line alternative and is
  the wrong one: every POST route, `/delete` included, would then accept a
  request from any page on the internet with your session on it.
- **The bookmarklet's key is scoped, and the scope is checked centrally.**
  `require_auth` allows it on `/api/ingest` alone. Trusting the endpoint to
  check would mean a key sitting in clear in a bookmarks bar is one forgotten
  `dependencies=[Auth]` away from deleting the library.
- **Reading the form in the auth dependency is safe, and only just.** Starlette
  caches the parsed form, so FastAPI's own parse reuses it rather than finding
  a drained stream. Checked on `multipart/form-data` too, because a file upload
  is the case that would have failed. Skipped unless the request is a POST with
  a form content type.
- **A key in the body must hand back a cookie.** `/api/ingest` answers with a
  303 to the new article, and that GET carries no credential — without
  `set_session_cookie` the bookmarklet ingested and then showed a login page.
- **`--proxy-headers` or the cookie loses `Secure`.** Behind Caddy the app sees
  plain HTTP, and `request.url.scheme` decides the flag. The trust list is open
  because the port is not reachable from the internet.
- **The bookmarklet cannot post from https to http.** A browser upgrades a form
  POST from a secure page, so a paywalled article sent to a plain-HTTP textcast
  went to an https address that answers nothing. It says so now; the real fix
  is TLS in front of the app.
- **The bookmarklet's address is baked in and must be the public one.** It is
  dragged to a bookmarks bar once and kept for months, so `location.origin` of
  whichever request drew the Add page is wrong behind a proxy.
  `web.public_origin` prefers `TEXTCAST_PUBLIC_URL`.
- **What the bookmarklet sends is not what the share sheet sends.** The
  bookmarklet posts `kind=html` with the page *your* browser rendered, session
  applied. The share sheet and Shortcut post `kind=url` and the server fetches
  it as a stranger. Only the first gets past a paywall.
- **`/media/` promises `immutable` and cannot keep it — and you cannot simply
  stop promising.** A picture's name is a hash of its address, so that URL
  never changes what it holds. A section's is `section-000.opus`, which every
  build of the article rewrites, so a rebuilt article held offline goes on
  playing the audio it had before, against the *new* timing map. The obvious
  fix — weaken the header to `no-cache` — was tried, and it breaks the offline
  feature outright. The audio element asks for byte ranges, so the browser's
  HTTP cache holds a *partial* entry for that URL; without a long-lived header
  Chromium serves the service worker's own plain GET from it as a ranged
  request, and `Cache.addAll` refuses the whole batch: "Partial response
  (status code 206) is unsupported". Marking an article offline then stored
  nothing — not the audio, not even the page — and the `.catch(() => {})`
  around `addAll` said so to nobody. Measured: the OFFLINE cache came back
  empty for every variant that dropped the year (`private`, `no-cache`,
  `max-age=0, must-revalidate`) and full for the one that kept it. The fix is
  to put the build in the URL so the promise becomes true; see `PLAN.md`.
- **`public` on a route behind `Auth` invites a CDN to keep the library.**
  Both media routes say `private` now, as the avatar already did.
- **A picture's size cap was checked after the picture was bought.**
  `_download` passed `stream=True` and then read `response.content`, which
  pulls the whole body regardless — so a host answering with a gigabyte put a
  gigabyte in memory before `MAX_BYTES` was consulted, inside the ingest
  request. It reads in 64 KB chunks and stops at the cap.
- **Signing out reached one browser, and there was no way to reach the rest.**
  `/logout` deletes the cookie and leaves `account.session` alone, which is
  right — a phone should not be signed out because a laptop was — but a cookie
  copied off a machine went on working and only changing the password stopped
  it. `/settings/sign-out-everywhere` rotates the session on its own and writes
  the browser that asked a fresh cookie, as changing the password already did.
- **A size checked off `UploadFile` bounds what is stored, not what saying no
  costs.** By the time the endpoint runs, the multipart parser has spooled the
  whole part to a temp file, and `await upload.read()` then puts all of it in
  memory to measure it — so a 2 GB post was fully received and fully bought
  before the 40 MB limit was consulted. `BodySizeLimit` reads Content-Length
  off the scope instead, which is known before the body is streamed and before
  `Auth` runs. `_read_capped` counts as it reads, for a chunked body that
  declared no length at all.
- **An SVG is a document wearing a picture's clothes.** A newsletter's own can
  carry a `<script>`, and served same-origin as `image/svg+xml` it ran against
  the signed-in session — but only when the address was opened *directly*, in
  a new tab. Inside an `<img>` it never could: browsers do not script an SVG
  loaded as an image. Dropping the type was the first answer and it cost more
  than it bought — the file was still fetched and still stored, now as `.img`
  and served as `application/octet-stream`, so the reader got a broken picture
  instead of a chart. `INERT` fixes the response rather than the allow-list:
  `sandbox` gives it an origin of its own, so there is no cookie and no DOM to
  reach, and a browser ignores the header when the file is loaded as an image.
  It is on every picture, not only the SVGs — a rule with no exception cannot
  be applied to the wrong file.
- **It is plain ASGI, not `@app.middleware("http")`.** That decorator is
  `BaseHTTPMiddleware`, which wraps every response in a stream; `/media` serves
  range requests straight off disk and should not be wrapped to check a header.

## Docker and the image

- **The build does not run the model.** `docker/bake_model.py` only calls
  `snapshot_download`. A build assembles an image; it does not do the app's
  work.
- **`en_core_web_sm` must stay pinned**, or Kokoro pip-installs a spaCy model
  at first synthesis, needing network and write access to the venv. Both are
  wrong in a container.
- **Named volumes inherit root ownership.** `/data` is created and chowned in
  the image *before* `VOLUME`, or an unprivileged container dies on `mkdir`.
- **Do not pin `ESPEAK_DATA_PATH` in the Dockerfile.** It was set to the arm64
  path, wrong on an amd64 image. `tts/kokoro.py` probes both plus Homebrew's
  prefixes and warns when it finds nothing.
- **One base image for both build stages.** The builder and the runtime were
  two different CPython builds: the venv recorded 3.12.12 and ran on 3.12.14.
  It worked only because both put python at `/usr/local/bin` and the ABI is
  stable across a patch release — a minor drift would have broken it without a
  word. The builder is the runtime image now, with `uv` copied in.

## Tests

- **The player tests share one page.** A test that pauses the audio breaks a
  later one that assumed it was playing. State what a test depends on; do not
  inherit it.
- **A test that reads pronunciation rules must ask for a database.**
  `test_rewrites` took no fixture, so it read whatever `settings.db_path`
  pointed at. It passed only while an earlier test had left a seeded one
  behind.
- **Sync Playwright allows one instance per thread.** One module-scoped
  `browser` fixture; a test needing isolation takes a context from it. A second
  `sync_playwright()` does not fail loudly — it raises inside the `chromium
  unavailable` guard and the test silently skips.
- **`test_seeking_to_a_block_highlights_that_block` is the flaky one.** It
  shares the module's page, so a slow frame in a test before it leaves the
  playhead somewhere else. It passes on its own and on a re-run; check that
  before believing it found something.
- **Playwright's page routes do not intercept the service worker script.** A
  delay on `/sw.js` did nothing, so a test meant to reproduce a stale-worker
  race passed with and without the fix. Reproduce that one against a real
  network, or not at all.
- **The player's own toggles are inside the sheet, which is hidden.**
  `check()` waits for a visible element and times out. A test about the
  *setting* sets the property and dispatches `change`; opening the sheet is a
  different test's business.
- **`page.wait_for_function` does not await a promise it gets back.** Proved
  by handing it `"async () => { await new Promise(r => setTimeout(r, 100));
  return false; }"` and watching it resolve in under 150 ms — a predicate
  that can never be true. `page.evaluate` awaits correctly; a test that must
  wait for an async condition (a service worker's cache, in particular) polls
  from Python with `evaluate` in a loop instead of trusting
  `wait_for_function` to do it. Existing `wait_for_function` calls built on
  `caches.match` were not re-audited; treat one as unproven until it has been
  made to fail first, the way every fixed bug here was.
