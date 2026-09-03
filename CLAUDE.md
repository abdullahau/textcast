# textcast — working notes

Newsletters, articles and documents turned into a private audio reader.
Self-hosted, offline-capable, deploys anywhere.

Decisions that are not obvious from the code, things measured rather than
assumed, and what is still open. Nothing the repository can be asked for:
`git log` says what landed, `du -sh data/*` how big it is, `uv run pytest` how
many tests there are.

**Before touching the parsers, the player, the build worker, the service
worker or the summary keys, read the matching section of
[`docs/traps.md`](docs/traps.md).** Everything in it has bitten once, filed by
the part of the code it bites. Separate because it is only wanted once you are
in that code, and this file is read at the start of every session.

---

## Run it

```bash
docker compose up -d --build   # http://127.0.0.1:8000, app + worker
docker compose logs -f worker  # builds and mail polling land here

uv sync --extra cpu --extra kokoro --extra kokoro-onnx --extra web \
        --extra documents --extra summaries --group dev
uv run pytest
uv run ruff check src tests    # before committing; formatting is not enforced
uv run playwright install chromium   # once, for the player tests
```

- **`--extra cpu` is not optional.** Without it torch comes from PyPI, which
  is the CUDA build and ~4 GB of NVIDIA runtime. `--extra cuda` is the other
  one; uv refuses both, they are declared as conflicting.
- **There is no command line.** `cli.py` was deleted — the app is the
  interface and a second one drifts. `python -m textcast` runs the build
  worker, which is all a container needs to start, and it polls the mailbox.
- **Docker is the deployment and the dev loop.** `./data` is a bind mount and
  both services run as `TEXTCAST_UID:TEXTCAST_GID` from `.env`: the image's
  uid is 10001 and the host's is not, which surfaces as "attempt to write a
  readonly database". `docker-compose.override.yml` mounts `./src` over the
  image's copy, so a host edit is the code that runs. For a server leave the
  override out: `docker compose -f docker-compose.yml up -d`.
- **Python is 3.12, pinned.** kokoro and misaki declare `requires-python
  <3.13`, kokoro-onnx `<3.14`. Running on 3.14 worked but sat outside all
  three and produced the `torch.jit.script` warning.
- **Kokoro 0.9.4 is the last release** (April 2025) and `hexgrad/Kokoro-82M`
  has not moved since. There is nothing newer.
- **Two images, one Dockerfile.** `docker build .` is CPU; `--build-arg
  ACCEL=cuda` builds `textcast:gpu`, layered by `docker-compose.gpu.yml`. The
  device cannot be an `.env` setting — torch and onnxruntime ship a different
  *distribution* per device — but the code asks the machine at load time
  (`torch.cuda.is_available()`, `onnxruntime.get_available_providers()`), so
  the GPU image still runs where no device was passed through.
- **The ONNX weights are not baked in.** Published as a GitHub release, not a
  hub repo. Put `kokoro-v1.0.onnx` and `voices-v1.0.bin` in `data/models/`;
  `./data` is bind-mounted, so no rebuild. Missing, the engine says so.
- **espeak-ng must be on the system** (`brew install espeak-ng`, or `apt
  install espeak-ng espeak-ng-data`). Missing, the failure is a bare
  "phontab: No such file or directory" at first synthesis, not at import.
- **To hear the ONNX engine**, pick it in **Engine** on the article page. The
  choice is per article and the cache is keyed on it, so switching back and
  forth costs nothing after the first build of each. `TEXTCAST_TTS_ENGINE`
  moves the default; it needs a restart.
- **The worker's healthcheck is disabled in compose.** The image's own curls
  the web port, which the worker does not serve, so it sat "unhealthy" for
  ever while working perfectly.

---

## The one idea

**The block is the unit.** A paragraph, quote, list item, footnote, summary,
picture or table is one row in `block` with a stable id (`b0-3` = section 0,
block 3).

Everything reads that table: the reader renders it, synthesis walks it, WebVTT
cue ids are block ids, search returns block ids, the player highlights by block
id. They cannot drift, because there is one list.

A picture is a block too — `BlockKind.FIGURE` and `TABLE`, same ids, same place
in the read-along. `block.text` is the cue a listener hears and what search
indexes; `block.media` carries what text cannot: the picture's address, the
table's cells. So the audio can stop on a chart and the reader shows it, where
the prose cites it.

If you are tempted to store text anywhere else, don't.

---

## Layout

```
src/textcast/
├── document.py     Article / Section / Block. Block.spoken() is the seam
│                   between what is shown and what is spoken.
├── normalize.py    Structural rewrites: money, percentages, quarters, spans.
├── pronounce.py    Word-level rules, editable in the UI. Respellings and IPA.
├── summarize.py    Section summaries from any OpenAI-compatible endpoint.
├── prefs.py        Default voice, quote voice and pace, stored in `setting`.
├── accounts.py     The one account: username, password hash, avatar, and the
│                   two secrets — the session and the ingest key.
├── pictures.py     Fetches every picture an article cites into
│                   `media/<slug>/images/`, and sweeps what nothing wants.
├── ingest/
│   ├── dom.py      selectolax helpers (lexbor)
│   ├── base.py     the shared DOM walker + junk pruning
│   ├── extract.py  content-density fallback (replaces readability-lxml)
│   ├── documents.py text, Markdown, PDF, DOCX
│   ├── visuals.py  what a picture, a table and a chart are, and what is
│   │               furniture wearing one. Asked once, per publication.
│   └── bloomberg.py / ft.py / substack.py / newsletter.py / generic.py
├── tts/            engine registry and kokoro.py; shared_engine is the one
│                   instance a process ever loads
├── audio.py        synthesis, Opus encoding, WebVTT emission
├── cache.py        what the block cache holds and may forget. Its own module
│                   because the worker's *parent* calls the sweep and must not
│                   import service to do it.
├── db.py           SQLite: articles, blocks, tags, jobs, positions, rules
├── migrate.py      runs on every start; seeding and column adds
├── jobs.py         the build worker, its engine pool, the mail poll, main()
├── service.py      ingest, store, re-parse, delete, delete_audio, summarise
├── mail.py         IMAP newsletter fetch
└── web/            FastAPI, Jinja templates, one stylesheet, the player
```

---

## Where the data lives

Everything under `TEXTCAST_DATA_DIR` (`./data`); nothing outside it matters for
a backup.

```
data/
├── textcast.db  articles, sections, blocks, tags, jobs, positions, rules,
│                settings, the account, and the FTS5 index
├── media/       the audio and the stored pictures, one directory per slug
├── sources/     the original HTML/eml/txt each article came from
├── avatar/      the profile picture
└── cache/       one raw render per block, keyed by content hash
```

**`textcast.db`** is plain SQLite in WAL mode, so `-wal` and `-shm` belong to
it. Only *text* is stored: `block.text` is what is shown, and `Block.spoken()`
derives what is said at build time, so a pronunciation rule takes effect on the
next build without rewriting anything.

**`media/<slug>/`** is what the player fetches: `section-000.opus`,
`section-000.vtt` (cue ids = block ids) and `manifest.json`. Safe to delete;
the next build writes it again.

**`media/<slug>/images/`** is the exception. Every picture an article cites is
fetched once at ingest and named for a hash of its address. A build does *not*
write it again and neither does anything else — the page it came from may be
gone. So the audio wipes take files only and step over this directory, and it
goes with the article and nothing less. `block.media["file"]` names the file;
`block.media["src"]` keeps the address, and the reader hotlinks that when the
fetch failed.

**`cache/`** is one `.i16` per rendered block — int16 PCM keyed by a hash of
the *spoken* text, the engine, the voice and the pace. Uncompressed, so roughly
an order of magnitude larger than the Opus it produced. That is the trade:
measured on a 33-minute article whose blocks were all cached, the rebuild took
**32 seconds** and added no cache file. Deleting it costs only time.

**`sources/`** keeps the bytes each article arrived as, so Re-parse can replay
a parser fix without re-fetching. Delete it and Re-parse stops working.

**A `position` row is deleted, not zeroed.** A zeroed row would still resume at
the top and still read as unfinished.

**Export** is three zips, one per thing the library holds. *Originals* is
`sources/`, which cannot be made again. *Text* is one Markdown file per
article, the displayed text with any summary in place. *Audio* is
`media/<slug>/` whole, `images/` included, so the read-along survives outside
the app. Files are named for the title, not the slug; a source whose article
was deleted is skipped, because nothing links it back to a name. Built in
memory, which is right at a few hundred megabytes and should be measured
before it is not.

**Deleting an article** takes the row and, by foreign key, its sections,
blocks, FTS entries, tag links, position and jobs; then `media/<slug>/` and
`sources/<slug>.*`; then it sweeps the cache. It does **not** touch tag names,
which are shared. `service.delete` owns that, not `db.delete_article` —
`reparse` calls the latter and must keep the source it is about to read.

---

## How the read-along works

The main feature, and the one most often wrong. Read before touching
`player.js` or the timing half of `audio.py`.

**One list, three artefacts.** `render_article` builds a `BlockTiming` per
block; the `block` rows, the WebVTT track and the JSON payload in the page all
come from it. They cannot disagree, because there is one producer.

**Where a cue starts.** Not on the first syllable: the 350 ms gap between two
blocks is split half in front, half behind, so a cue opens in silence and the
words arrive ~175 ms later. Anything slow lands in the run-up rather than
inside a word, and the highlight turns on a moment before the voice.

**What drives it.** A `requestAnimationFrame` loop reading `audio.currentTime`,
binary-searching the payload. Plus a `seeked` listener, because media-chrome
owns the skip buttons and scrub bar and moves the playhead itself. `cuechange`
is a backstop for a background tab, where frames stop and audio does not.

It was a hand-written search over a separate timing map (dropped: the map
drifted from the audio), then WebVTT alone on `cuechange` (11 ms in Chromium,
far looser elsewhere, and `activeCues[0]` is the cue that just *ended*), then
the clock: 2 ms, browser-independent, and it cannot drift because the payload
and the VTT come from one list.

**Worth improving:** word-level highlighting (needs per-word timings Kokoro
does not return — a forced aligner, or synthesising word by word and paying for
the joins); a run-up proportional to how late the browser actually is rather
than a fixed half-gap; a `waiting`/`playing` pair to resync after a stall; the
section boundary, which swaps the media element's source and leaves a gap the
timing map knows nothing about.

---

## Decisions that were measured, not guessed

Re-measure before overturning any of these. Numbers are from this box: 4-core
ARM Neoverse-N1, no GPU.

### The engines

| Decision | Evidence |
| --- | --- |
| **ONNX is the default engine** | Indistinguishable by ear from Kokoro and cheaper by every other measure: RTF **0.456 vs 0.557** over the build pool, **530 MB vs 1,512 MB** resident, 1.5 s to load four instances against 7 s, ~40 MB of wheels against torch's 1.4 GB. Its weights are baked into the image — a default whose model files are missing cannot build anything — and a copy in `data/models` still wins, which is how a new export is tried. What it does *not* share is the G2P: kokoro reaches espeak through misaki, this through phonemizer. |
| **Speed does not earn an engine its place** | Supertonic was ~2× faster (RTF 0.31 vs 0.65) and went anyway: drifting volume, glitches on long paragraphs. Out with it went a build option, a second set of weights and a licence. Not kokoro-onnx, which is the *same* weights through a different runtime. |
| **One session/model behind the whole pool** | Four onnxruntime instances each opening the 311 MB model held **1,595 MB**; four sharing one session held **530 MB** and rendered no slower (0.456 vs 0.451). Same for torch: four `KPipeline`s with their own `KModel` held **4,188 MB**, four sharing one held **1,535 MB** at RTF 0.561 vs 0.574. `Run` is thread-safe; the tokenizer and voice cache stay per instance. |
| **4 single-threaded engines, not 1 four-threaded** | RTF 0.562 vs 0.629, an 11% gain. 2×2 gives 0.633, 6×1 gives 0.593. Bound by memory bandwidth more than cores — do not expect 4× from 4 cores. |
| **Kokoro is not deterministic, so sharing is no riskier** | It uses the deprecated `weight_norm`, which mutates the module inside `forward`. Measured: one pipeline, same sentence, three times — max abs difference 0.081, 0.084. Four independent, concurrent: 0.088, 0.130, 0.129. Four sharing one model: 0.113, 0.104, 0.087. Sharing sits inside the model's own spread. Corollary: the block cache is *why a rebuild sounds like the first build*, not only why it is fast. |
| **Every job runs in a child process that then exits** | `import torch` cannot be undone — the libraries stay mapped and the allocator's arenas stay grown, so a worker that had built once held **1,006 MB for life** against **37 MB** before. The parent polls the job table and never imports torch: measured at **38.3–38.4 MB** across 66 samples, `torch/lib` never in its maps. The child holds 1,600–1,880 MB for one article, **2,892 MB** after two, and gives every byte back **1.2–2.5 s** after the last job. Start-up ~2 s. Summaries go the same way: `openai` brings httpx and pydantic and took the worker from 38 MB to 79 MB permanently. |
| **One process, one engine** | A build job for the other engine is put back and passed over; `claim_job` takes a `skip` set so it does not stall the queue behind it. Building a second pool beside the first is how you reach **7,508 MB**: measured on one queue with a job per engine, the ONNX child peaked at **717 MB** with no `torch/lib` in its maps, the kokoro child at **1,933 MB** with no `onnxruntime`, the parent at 49 MB with neither. |
| **One engine instance per process** | The web app built a new 82M-parameter model for every voice list, preview and phoneme lookup. `tts.shared_engine` keeps one; the worker publishes its first pool instance into the same slot. |

### Audio and timing

| Decision | Evidence |
| --- | --- |
| **The timing map is engine-agnostic, and was checked** | `render_article` never asks which engine made the audio. On the same three blocks Kokoro pads 299 ms in front and 467 behind, the ONNX export 65 and 284; `trim_silence` flattens both. Afterwards the timings agree to within 3 ms on the first block and 50 ms on the longest — the model's own spread — and the run-up is exactly 175 ms on both, because one line of code puts it there. |
| **The model's silence is trimmed off every block** | ~300 ms in front and ~500 ms behind. Left in, seeking gave a third of a second of dead air and a 350 ms gap played as 1150 ms. `trim_silence` cut 2% off a 20-minute issue whose blocks are long, far more where they are short. |
| **A block's cue opens half a gap before its first word** | Landing on the attack means anything slow starts you inside the first word. The audio is untouched: decoded output hashes identically, only the map moves. |
| **ffmpeg stays; opusenc was measured and refused** | It would take 376 MB off the image, but its files are 4.9% bigger at the same 32 kbps and `--music --comp 10` makes that 22%. Encode 4.83 s vs 4.19 s for 275 s of audio — irrelevant next to ~155 s of synthesis, and the size is permanent. |
| **float32 to int16 costs nothing audible** | Quantisation error peaks at −90 dBFS; after encoding the result differs from the float32 path by −47 dBFS while the encoder's own loss is −45 dBFS. The file is 0.17% *smaller*. So the 4.9% above is opusenc, not the integers. |
| **Ogg page size was ruled out first** | The 1.02 s page grid looked like the cause of a seeking bug. A tone-per-block probe in Chromium showed seeking is sample accurate at that grid. Re-encoding finer would have cost ~10% in size for nothing. |
| **One Opus file per section** | Playback starts before the whole article is built and a failed block re-renders in seconds. 22 min is 4.6 MB at 28 kbps. |
| **The highlight reads the clock; WebVTT is the backstop** | `cuechange` fires when the browser gets round to it, and a seek from media-chrome's transport changes no cue set at all. The frame loop is 2 ms and browser-independent. The track stays: it is the file's own record of the timings, and it keeps a background tab roughly right. |
| **media-chrome for transport** | MIT, vendored as one 41 KB gzipped IIFE, no build step. It does *not* touch Media Session — the lock screen is wired by hand in `player.js`. |

### Parsing, pronunciation and visuals

| Decision | Evidence |
| --- | --- |
| **selectolax, not BeautifulSoup** | 4.8× faster on the corpus (66 ms vs 314 ms for 6.3 MB), one wheel, no lxml. `[class*="Footnotes_base"]` also beats a regex over the class list. |
| **A visual is a block, not a second table** | A `figure` table keyed by the block it follows would need its own ids, its own ordering and its own answer to "what does the player highlight" — and the read-along cannot afford two lists. One `media` column of JSON instead, empty on every prose row. |
| **The page shows the caption, the audio says the label** | `block.text` is `Table: Ker-CHING`, which the synthesiser reads and search indexes; `media["caption"]` is the caption alone, which the page prints. A picture with no caption gets `Figure.` for the listener and nothing on the page. Where the FT lifts a title out of the header row, `caption` is left off — printed again below, it read as a second title. |
| **A charting frame is stored as a picture of that chart** | The frame drew nothing: it needs the provider's own script, which the sandbox refuses. It is also a third party the reader never agreed to, and it vanishes offline. Flourish publishes the same chart as a still at 1020px with title, legend, axis and source. An allowlist of providers whose still has actually been fetched and looked at; a frame from anywhere else is dropped, because a figure pointing at a 404 is worse than no figure. |
| **A picture is fetched, not hotlinked** | Hotlinking cost three things: an article kept offline showed nothing, a paywalled image answered 403, and the publication learned the reader's address. The fetch runs inside the ingest request in a pool of four — that request already goes to the network for `kind=url` — and is never fatal: a picture that will not download keeps its remote address. Named for a hash of that address, so a re-parse writes nothing and two blocks quoting one chart share a file. |
| **Visuals are opt-in per adapter; the newsletter walk stays text-only** | `blocks_from_dom` defaults to `NO_VISUALS`, so a walk not told what a publication's furniture looks like behaves as before. `newsletter.py` opts out: it reads leaf table cells and cannot tell a two-by-two of prose from data. |
| **Ask misaki before writing a spell-out rule** | Its notation is not IPA — capital `A` is the /eɪ/ of "day", `I` the /aɪ/ of "eye". Of 45 `SPELL_OUT` entries, 41 sounded identical with the rule and without: misaki already spells acronyms out, better. "CEO" alone is `sˌiˌiˈO`; the rule's "C E O" is `sˈi ˈi ˈO`, every letter stressed and 120 ms longer. Two remain, for words misaki says instead — `ROE` as "roe", `ETH` as the letter. |
| **Respellings, not IPA** | Every acronym checked against Kokoro; `GAAP`→`gap` works and anyone can edit it. Exactly one rule needs IPA: `LIBOR`, where Kokoro is right and every respelling is worse. NASDAQ, SPAC and NAV are already correct and kept as explicit rules anyway, so they stay correct if the voice changes. |
| **One sound, two spellings, both measured** | espeak's notation for LIBOR was measured, not converted from misaki's: espeak's own phonemisation of "lie bore" is `lˈaɪ bˈɔːɹ`. Left alone it says `lˈɪbɚ`, "LIB-er". A library that already holds the rule gets the second spelling from a migration, not from seeding — seeding skips anything it has offered before, which keeps a deleted built-in deleted. |
| **`401(k)`, `INmune` and Shein are respellings, measured first** | `401(k)` was "four hundred one k"; `four oh one k` is `fˈɔɹ ˈO wˈʌn kˈA`. `INmune` was "I EN-mune" because a leading `IN` looks like an initialism, while `INMune's` already came out right — one changed capital puts every form on the path misaki got right. Shein was "Shane" on both engines (`ʃˈAn`, `ʃˈeɪn`); "Sheein" is right on both. All are `regex` rules with `(?!\w)`, not `word` rules: a word rule's trailing `(?![\w'])` refuses to match before an apostrophe and would miss the possessive. |
| **"refund" is a house preference, not a correction** | Both engines read the verb correctly. The rule puts the noun's stress on every form. The inflections are named (`s\|ed\|ing`) rather than left to a bare prefix, because "refundable" keeps the verb's stress and "reefundable" wrecks it. Delete the rule on the Voice page to go back. |
| **Emphasis markers are stripped for speech, not for the page** | `*before*` reached the engine intact and it said "before asterisk". Markdown strips them at parse time; a newsletter arrives as HTML and carries them straight through. Paired markers only — a lone asterisk is a footnote marker or a bullet. Found by grepping the worker's log for phonemizer's "words count mismatch": 47 of 430 blocks tripped it, almost all CamelCase names espeak was right about, six were this. |
| **A time on the hour loses its zero minutes** | `8:00am` was read "eight zero zero a m". `8 a.m.` is `ˈAt ˌAˈɛm`. A time that is *not* on the hour already works, so only the o'clock case and the missing space are touched. |
| **Smart punctuation is flattened before the rules run** | Web prose is full of curly apostrophes and a rule written for `who'll` never matched `who’ll`. |
| **A rule is skipped by a substring test before the regex engine sees it** | A word or phrase rule is a literal inside guards, so the literal must be in the text: `needle in lowered` is the same scan at C speed and skips 96% of the rules. Deciding whether a rule fires moved to `_prepared`, an `lru_cache` keyed on the rules — 86 answers were being recomputed 206,400 times. Over 2,400 blocks: **1.014 s → 0.633 s**. Byte-identical output over every block of `tests/corpus`, both phonemisers. |
| **A rule's literal narrows the rebuild scan in SQL, not in Python** | `b.text LIKE ?` is a superset of what the pattern would match, so SQLite reads only the rows that pass. Over 12,000 blocks across 200 articles: **48 ms → 5 ms**, growing with the hits rather than the library. A regex rule with no literal still reads them all. |

### The app

| Decision | Evidence |
| --- | --- |
| **Sign-in is a username and a password, seeded once from `.env`** | `TEXTCAST_USERNAME` and `TEXTCAST_AUTH_TOKEN` write the `account` row on an empty table and are never read again — a password you can change in the app cannot also live in a file the container started with. scrypt from the standard library, so no argon2 or bcrypt wheel. A wrong username reads exactly like a wrong password. |
| **The cookie carries a session, not the credential** | It held the token itself, so the secret travelled on every request and changing it could not end a session. Changing the password rotates `account.session`: every other browser is signed out and the one doing it carries on. |
| **The bookmarklet's key reaches one route** | It was the session token — in clear in a bookmarks bar, able to delete the library. Now a second secret that opens `POST /api/ingest` and nothing else, with the scope checked in `require_auth` rather than trusted to the caller, and regenerable without touching the password. |
| **Re-parse replaces nothing when the parse has not changed** | Seven of nine sources re-parse to exactly what is stored, and each was being deleted and rewritten — taking the article out of `ready` and orphaning correct audio. Over a library that reads as "re-parsing broke everything". `_same_article` compares section titles, every block as it would be stored, and the metadata, ignoring `media["file"]` which the picture fetch writes *after* the store. |
| **Re-parse queues nothing, and keeps the summaries** | Queueing a build per article is the machine for the rest of the day. And a summary is a block no source ever held, so re-parsing silently deleted every one in the library — 35 across seven articles, each a call to a model. They are filed by section title and put back at the head of the section they belonged to. |
| **`article.status` describes the audio, nothing else** | A summary leaves it alone and a failed summary does not mark the article failed. **`completed` is the exception that proves it**: a status you can filter for that no row ever holds — `db._article_filter` answers it as `a.status = 'ready' AND p.finished = 1`. The ready check is not redundant; it keeps a stale position on an article whose audio was dropped out of the answer. |
| **A summary is a block, written before the audio** | Not a column on `section`. Inserting a block moves every id after it, so `summarise` is its own job kind and it stops when the blocks are written. Nothing builds audio on your behalf. |
| **A summary lands the moment it does, and a refusal is named** | The pass gathered every section with `pool.map` and read the results as a dict, so the first refusal threw away the summaries already beside it — and the only record was a line in the log. Each call is caught on its own now and each summary stored as it arrives. The usual failure is a free tier's rate limit, which refuses part of a burst and answers the rest. |
| **Summaries speak the OpenAI protocol, not a router** | One `openai` dependency reaches 14 providers, listed in `summarize.PROVIDERS`. litellm was measured and refused: 183 MB across 114 packages against 21 MB, and all it adds is providers with no OpenAI endpoint at all. |
| **Summary settings live in the database, not the environment** | The environment is the default; a value saved on the page wins. The other way round, editing the model appeared to do nothing whenever the container set one. |
| **Two lanes, not one queue** | A build is the CPU for minutes; a summary is the network for seconds. `claim_job` only keeps them off the *same* article, where a summary would rewrite the blocks a build is rendering. |
| **Adding text and choosing how to read it are two jobs** | The Add page asked for voice, pace, footnote and summary switches before you had seen what the parser made of it. It takes the text in and nothing else; the article page decides. |
| **torch from the CPU index** | The default pulls CUDA: 15 nvidia packages, 5.2 GB venv, 9.3 GB image, on a box with no GPU. Pinned in `[tool.uv.sources]`. Now 1.4 GB and 3.3 GB. |

---

## Where it stands

Everything is on `main`. Commit straight to it; the owner asked for no feature
branches unless they say so. For the current state ask the thing itself: the
library page, `docker compose ps`, `du -sh data/*`, and

```bash
sqlite3 -header -column data/textcast.db \
  'SELECT status, COUNT(*) FROM article GROUP BY status'
```

## Still open

1. **Judge the voice.** The only thing that matters and the only thing code
   cannot settle. Speed is measured; quality is not.
2. **Summaries are per article, not per section.** A pass stores each section
   as it lands and reports what failed, so a rate limit costs only the refused
   calls. Missing: a control per section, and pacing — four calls go at once
   (`MAX_PARALLEL`), which trips a free tier allowing five in five minutes. No
   cost estimate, no cache. "Summarise all" makes one call per section across
   the library behind a single confirm box.
3. **Trimming uses a fixed threshold.** `SILENCE_LEVEL` is 0.01, above
   Kokoro's noise floor and below speech. Another engine, or a voice that
   trails off quietly, would want it measured again.
4. **The pronunciation rebuild is offered, not automatic.** It names the built
   articles using the word and rebuilds on one click, but does not queue them
   and does not notice a rule that has *stopped* matching.
5. **The GPU path is untested.** Wheels resolve, extras conflict as they
   should, both engines ask the machine — but this box has no device. Every
   RTF here is CPU.
6. **`db.articles_matching` still reads every block for a *regex* rule.** Word
   and phrase rules are narrowed by `LIKE` in SQL first, which is most of them.
   Measured over 12,000 blocks: 48 ms against 5 ms.
7. **The offline test covers one article, not eviction.** Nothing exercises
   the browser evicting under storage pressure, or `drop-article`. Worth
   deciding before it bites: `mediaResponse` stores *every* audio file it
   fetches, so listening online fills the offline cache with articles nobody
   asked to keep, in the same cache as the ones they did — and the browser
   evicts without knowing the difference. Two caches, or only storing what
   `cache-article` asked for, would separate them.
8. **Nothing rate-limits `/api/ingest`**, the one route that takes a
   credential in a body from anywhere on the internet.
9. **Mail polling has no test.** `mail.py` talks IMAP and nothing stands in
   for a server.
10. **Article hits and block hits are ranked separately.** `search` puts
    metadata matches first and FTS after, rather than in one order. Right at
    this size; revisit past a few thousand articles.
11. **A picture that failed to download is never tried again.** The block
    keeps its remote address and the reader hotlinks it, which is a fallback
    and not a repair. Nothing re-runs `pictures.fetch_for` except a re-parse
    and nothing says which articles still point outside. `to_markdown` writes
    the remote address too, so a text export opened offline shows nothing —
    the audio export does carry them. The service worker does not cache them
    either: a lazy-loaded picture below the fold is never asked for while the
    article is being saved.
12. **Re-parsing an article loses your place in it.** The `position` row goes
    by foreign key and `reparse` carries tags and build options but not that.
    Defensible — an unchanged re-parse no longer replaces the row, and a
    changed one has invalidated the audio anyway — but it happens silently.
13. **Only the audio's own cue says a chart is there.** Nothing tells you
    before you start that an article has visuals, and the library shows no
    mark.
14. **Newsletters get no visuals.** A Substack issue arriving by email is fine
    — `SubstackAdapter` sits before it in the registry — but anything else
    loses its charts.
15. **A hand-written summary is not protected.** Nothing marks a summary block
    as yours, so "Summarise again" replaces it and "Delete summaries" removes
    it. The reader says which model wrote them and stays silent where it does
    not know, which makes the hazard visible but does not stop it.

## Conventions

- **Comments say why, not what.** Most usefully: why an obvious alternative
  was rejected, and what broke to make the code look like this.
- **Test names are sentences** describing the behaviour; the docstring carries
  the reason where the name cannot.
- **The parse corpus is `tests/corpus`.** It sits in the tests because `data/`
  is ignored, and a test may not depend on a copy that is not in the
  repository. The Substack page is a hand-written fixture: nothing in the
  library was one, and a fixture can carry the subscribe widget, the avatar
  and the Cloudinary srcset the filters exist to refuse.
- **Measure before switching engines, parsers or dependencies.** Every entry
  in the tables above exists because a guess was wrong at least once.
- **A trap goes in `docs/traps.md`, under the part of the code it bites.** Say
  what broke: the incident is what makes the rule believable. What goes
  nowhere is a number the repository can be asked for — a test count, a
  directory size, what the library holds today. Each is stale the day after,
  and the command that answers it is shorter than the sentence recording it.
- **Bump `__version__` with any change under `static/`.** The service worker
  names its caches after it, so an edit inside one version is invisible to
  every installed client.
- `uv run ruff check src tests` before committing. Formatting is not enforced;
  do not reformat the tree.
