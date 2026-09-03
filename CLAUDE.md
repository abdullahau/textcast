# textcast — working notes

Newsletters, articles and documents turned into a private audio reader.
Self-hosted, offline-capable, deploys anywhere.

This file is for whoever picks the project up next. It records the decisions
that are not obvious from the code, the things that were measured rather than
assumed, and what is still open.

---

## Run it

```bash
docker compose up -d --build   # http://127.0.0.1:8000, app + worker
docker compose logs -f worker  # builds and mail polling land here

uv sync --extra cpu --extra kokoro --extra kokoro-onnx --extra web \
        --extra documents --extra summaries --group dev
uv run pytest                  # 439 tests
uv run ruff check src tests    # before committing; formatting is not enforced
```

**`--extra cpu` is not optional.** It picks the CPU wheels of torch and
onnxruntime. Leave it out and torch comes from PyPI, which is the CUDA build
and ~4 GB of NVIDIA runtime. `--extra cuda` is the other one, and the two are
declared as conflicting extras so uv refuses both.

**There is no command line.** `cli.py` was deleted: the app is the interface,
and a second one drifts. `python -m textcast` runs the build worker, which is
the only thing a container needs to start, and it polls the mailbox too.

**Docker is the deployment and the dev loop.** `./data` is a bind mount, and
both services run as `TEXTCAST_UID:TEXTCAST_GID` from `.env` — the image's own
uid is 10001 and the host's is not, which shows up as "attempt to write a
readonly database". `docker-compose.override.yml` mounts `./src` over the
image's copy, so an edit on the host is the code that runs.

**Kokoro 0.9.4 is the last release**, from April 2025, and `hexgrad/Kokoro-82M`
still holds `kokoro-v1_0.pth`, untouched since April 2025. There is nothing
newer to move to.

**Python is 3.12, and pinned there.** kokoro and misaki both declare
`requires-python <3.13`, and kokoro-onnx declares `<3.14`; 3.12 is the newest
interpreter every one of them supports. The app ran on 3.14 for a while, which
worked but was outside all three declarations and was where the
`torch.jit.script` warning came from. Moving back cost three lines —
`.python-version`, `requires-python` and two image tags — and a re-resolve.

**To hear the ONNX engine**, pick it in **Engine** on the article page and
build. The choice is stored per article, so one article can be ONNX and the
rest Kokoro, and the block cache is keyed on the engine so switching back and
forth costs nothing after the first build of each. `TEXTCAST_TTS_ENGINE` in
`.env` moves the default for everything, and needs a restart.

**Two images, one Dockerfile.** `docker build .` builds the CPU image;
`--build-arg ACCEL=cuda` builds `textcast:gpu`, and
`docker-compose.gpu.yml` layers both on top of the base compose file. The
device cannot be an `.env` setting: torch and onnxruntime each ship a
different *distribution* per device, so it is decided when the image is
built. The code then asks the machine — `torch.cuda.is_available()` in
`tts/kokoro.shared_model`, `onnxruntime.get_available_providers()` in
`tts/kokoro_onnx.providers` — so the GPU image still runs where no device was
passed through. Neither has been measured on a GPU: this box has none.

**The ONNX engine's weights are not baked into the image.** They are published
as a GitHub release, not a hub repo. Put `kokoro-v1.0.onnx` and
`voices-v1.0.bin` in `data/models/` and the engine appears; leave them out and
it says exactly what is missing. `./data` is bind-mounted, so the container
sees them without a rebuild.

Kokoro needs **espeak-ng** on the system (`brew install espeak-ng`, or
`apt install espeak-ng espeak-ng-data`). `tts/kokoro.py` finds it in the usual
places; if it cannot, the failure is a bare "phontab: No such file or
directory" on the first synthesis, not at import.

Playwright drives the player tests: `uv run playwright install chromium` once.

---

## The one idea

**The block is the unit.** A paragraph, quote, list item, footnote or summary
is one row in `block` with a stable id (`b0-3` = section 0, block 3).

Everything reads that same table: the reader renders it, synthesis walks it,
the WebVTT cue ids are block ids, search returns block ids, and the player
highlights by block id. They cannot drift apart because there is only one list.

**A picture is a block too.** A figure, a table and a live chart are
`BlockKind.FIGURE`, `TABLE` and `EMBED`: same ids, same place in the
read-along, same row. `block.text` is the cue a listener hears and the string
search indexes; `block.media` is the one thing text cannot carry — the
picture's address, the table's cells, the frame's link. So the audio can stop
on a chart and the reader shows it, at the point the prose cites it.

If you are tempted to store text anywhere else, don't.

---

## Layout

```
src/textcast/
├── document.py     Article / Section / Block. Block.spoken() is the seam
│                   between what is shown and what is spoken.
├── normalize.py    Structural rewrites: money, percentages, quarters, spans.
│                   Anything needing arithmetic rather than a lookup.
├── summarize.py    Section summaries from any OpenAI-compatible endpoint.
│                   Model, endpoint, key and prompt are all editable in the UI.
├── pronounce.py    Word-level rules, editable in the UI. Respellings and IPA.
├── prefs.py        Default voice, quote voice and pace, stored in `setting`
│                   so they change without a restart and queue nothing.
├── pictures.py     Fetches every picture an article cites into
│                   `media/<slug>/images/`, and sweeps what nothing wants.
├── ingest/
│   ├── dom.py      selectolax helpers (lexbor)
│   ├── base.py     the shared DOM walker + junk pruning
│   ├── extract.py  content-density fallback (replaces readability-lxml)
│   ├── documents.py text, Markdown, PDF, DOCX
│   ├── visuals.py  what a picture, a table and a live chart are, and what
│   │               is furniture wearing one. Asked once, per publication.
│   └── bloomberg.py / ft.py / substack.py / newsletter.py / generic.py
├── tts/            engine registry and kokoro.py; shared_engine is the
│                   one instance a process ever loads
├── audio.py        synthesis, Opus encoding, WebVTT emission
├── cache.py        what the block cache holds and what it may forget.
│                   Its own module because the worker's *parent* calls
│                   the sweep, and must not import service to do it.
├── db.py           SQLite: articles, blocks, tags, jobs, positions, rules
├── migrate.py      runs on every start; seeding only, the schema repairs
│                   have all run and were removed
├── jobs.py         the build worker, its engine pool, the mail poll, and
│                   `main()` behind `python -m textcast`
├── service.py      ingest, store, re-parse, delete, delete_audio,
│                   delete_summaries, queue a summary pass
├── mail.py         IMAP newsletter fetch
└── web/            FastAPI, Jinja templates, one stylesheet, the player
```

---

## Where the data lives

Everything is under `TEXTCAST_DATA_DIR` (`./data`), and nothing outside it
matters for a backup. Sizes are from this machine with 9 articles.

```
data/
├── textcast.db    488 KB   the library: articles, sections, blocks, tags,
│                           jobs, positions, pronunciation rules, settings
│                           (plus the FTS5 index over block text)
├── media/         9.7 MB   the audio, one directory per article slug
├── sources/       6.1 MB   the original HTML/eml/txt each article came from
└── cache/         260 MB   one raw render per block, keyed by content hash
```

**`textcast.db`** is plain SQLite in WAL mode, so `textcast.db-wal` and
`-shm` sit beside it and belong to it. Inspect it with the `sqlite3` CLI:

```bash
sqlite3 data/textcast.db '.tables'
sqlite3 data/textcast.db '.schema block'
sqlite3 -header -column data/textcast.db \
  'SELECT kind, COUNT(*) FROM block GROUP BY kind'
```

Only *text* is stored: no audio, no images, no binaries. `block.text` is what
is shown, and `Block.spoken()` derives what is said at build time — the spoken
form is never stored, so a pronunciation rule takes effect on the next build
without rewriting anything.

**`media/<slug>/`** is what the player fetches: `section-000.opus` (the audio),
`section-000.vtt` (the timing map, cue ids = block ids), and `manifest.json`
(the same timings, for reading outside the app). Safe to delete an article's
directory; the next build writes it again.

**`media/<slug>/images/`** is the exception to that last sentence. Every
picture the article cites is fetched once at ingest and stored here, named for
a hash of the address it came from. A build does *not* write it again, and
neither does anything else: the page it came from may be gone. So the audio
wipes — **Delete audio**, and the one an edited block triggers — take files
only and step over this directory, and it goes with the article and with
nothing less. `block.media["file"]` names the file; `block.media["src"]` keeps
the address it came from, and the reader falls back to hotlinking it when the
fetch failed.

**`cache/`** is one `.i16` per rendered block — int16 PCM, keyed by a hash of
the *spoken* text, the engine, the voice and the pace. Still uncompressed, so
it is large next to the audio it produced: 658 MB behind 76 MB of Opus. That
is the trade it exists to make. Measured on a 33-minute article whose blocks
were all cached: the rebuild took **32 seconds**, wrote the same `audio_ms` to
the millisecond, and added no cache file — an encode, with nothing going back
to the model. Deleting it costs only time.

It was float32 and 2.23 GB. int16 halves every file and the quantisation
error peaks at −90 dBFS, under the encoder's own −45 dBFS. A one-shot
`compact_cache` converted 338 files in place and swept the rest: **2.23 GB to
658 MB**, no engine loaded, nothing re-rendered. It has run, everything is
`.i16`, and it is deleted — git still has it. A stray `.f32` is now simply
unreachable and the sweep takes it.

**`sources/`** keeps the bytes each article arrived as, so Re-parse can replay
a parser fix without re-fetching. Delete it and Re-parse stops working.

**A `position` row is deleted, not zeroed.** "Stop and forget my place" in the
player sheet calls `db.clear_position`, which removes the row. A zeroed row
would still resume the reader at the top and still read as unfinished.

**Export** is three zips at the foot of the library, one per thing the library
holds, because they are wanted for different reasons. *Originals* is
`sources/`, which cannot be made again. *Text* is one Markdown file per
article — `document.to_markdown`, the displayed text with any summary in
place, not the spoken form. *Audio* is `media/<slug>/` per article, the timing
map and the manifest with the Opus, so the read-along survives outside the
app. Every file is named for the article's title, not its slug, and a source
whose article has been deleted is skipped: nothing links it back to a name.
The zips are built in memory, which is right at a few hundred megabytes and
should be measured before it is not.

Deleting an article from the reader takes the row and, by foreign key, its
sections, blocks, FTS entries, tag links, saved position and jobs; then
`media/<slug>/` and `sources/<slug>.*` as files; then it sweeps the cache. It
does **not** touch the tag names, which are shared. `service.delete` owns
that, not `db.delete_article` — `reparse` calls the latter and must keep the
source it is about to read.

## Running it in Docker

`docker compose up -d --build`. Two services off one image, sharing `./data`.
A named volume was the earlier default and would have started empty, with none
of the library in it.

The worker's healthcheck is disabled in compose: the image's own curls the web
port, which the worker does not serve, so it sat "unhealthy" for ever while
working perfectly.

For a server, leave the override out: `docker compose -f docker-compose.yml
up -d`.

One trap: the service worker caches `/static/` hard, so a CSS or JS edit can
be invisible in the browser while being live on disk. Hard-reload, or tick
"Bypass for network" in DevTools.

## How the read-along works

The main feature, and the one that has been wrong most often. Worth reading
before touching `player.js` or the timing half of `audio.py`.

**One list, three artefacts.** `render_article` builds a `BlockTiming` per
block, and everything downstream comes from that same list: the `block` rows
(`start_ms`, `dur_ms`), the WebVTT track beside the audio, and the JSON payload
in the page. They cannot disagree, because there is one producer.

**Where a cue starts.** Not on the first syllable. The gap between two blocks
is split — half in front, half behind — so a cue opens in silence and the words
arrive about 175 ms later. Anything slow lands in the run-up instead of inside
a word, and the highlight turns on a moment before the voice, which is the
right way round to read along.

**What drives the highlight.** A `requestAnimationFrame` loop while playing,
which reads `audio.currentTime` and finds the block by binary search over the
payload. Plus a `seeked` listener on the element, because media-chrome owns the
skip buttons and the scrub bar and moves the playhead itself. `cuechange` is
kept only as a backstop for a background tab, where frames stop and audio does
not.

**How it used to work, and why that changed:**

1. *A hand-written binary search over a separate timing map.* Dropped: the map
   was maintained apart from the audio and drifted from it.
2. *WebVTT alone, reacting to `cuechange`.* Better, and correct in Chromium
   (measured 11 ms). Two problems: at a boundary the browser reports both the
   outgoing and incoming cue and the code took `activeCues[0]`, the one that
   had just ended; and how promptly the event fires at all is the browser's
   business, with Safari far looser than Chromium.
3. *The clock, as above.* Measured 2 ms. The drift that killed (1) cannot
   happen now, because the payload and the VTT are emitted from one list.

**Worth improving, in rough order:**

- **Word-level highlighting.** The block is the unit and should stay the unit,
  but a cursor inside it would read far better. Needs per-word timings, which
  Kokoro does not return — either a forced aligner over the rendered audio, or
  synthesising word by word and paying for the joins.
- **The run-up is a fixed half-gap.** It could be proportional to how late the
  browser actually is, measured once at load.
- **Nothing re-syncs after a stall.** If the audio buffers mid-block the clock
  and the audio can part company; a `waiting`/`playing` pair would say so.
- **The section boundary is a hard cut.** Moving between sections swaps the
  media element's source, so there is a gap the timing map knows nothing about.

## Decisions that were measured, not guessed

Re-measure before overturning any of these. The numbers are from this box:
4-core ARM Neoverse-N1, no GPU.

| Decision | Evidence |
| --- | --- |
| **The timing map is engine-agnostic, and was checked** | `render_article` is the only producer of the block timings, the WebVTT cues and the manifest, and it never asks which engine made the audio. Measured on the same three blocks: Kokoro pads 299 ms in front and 467 ms behind, the ONNX export 65 and 284 — it trims some itself — and `trim_silence` flattens both. Afterwards the timings agree to within 3 ms on the first block and 50 ms on the longest, which is the model's own spread. The run-up is exactly 175 ms on both, because the same line of code puts it there. Same sample rate, same cue ids, same layout. |
| **ONNX is the default engine** | Judged indistinguishable by ear against Kokoro, and cheaper by every other measure: RTF 0.456 against 0.557 over the build pool, 530 MB against 1,512 MB, 1.5 s to load four instances against 7 s. Its weights are baked into the image, because a default whose model files are missing cannot build anything. A copy in `data/models` still wins over the baked one, which is how a new export is tried without a rebuild. |
| **kokoro-onnx renders faster on less memory, and phonemises differently** | The same v1.0 weights through onnxruntime instead of torch. Measured on this box against the pool the worker actually uses — four instances, one thread each: **RTF 0.456 against 0.557**, 18% faster, and **530 MB resident against 1,512 MB** after a render. Four instances load in 1.5 s against 7 s, and the wheels are ~40 MB against torch's 1.4 GB. What it does *not* share is the G2P: kokoro reaches espeak through misaki, this reaches it through phonemizer. Every pronunciation rule here still lands — checked word by word — except the one written in IPA. See the trap below. |
| **One onnxruntime session behind the whole pool** | The same trade as `KModel`, and the same size. Four instances each opening the 311 MB model held **1,595 MB** after a render; four sharing one session held **530 MB** and rendered no slower (RTF 0.456 against 0.451). onnxruntime's `Run` is thread-safe, so the session is the thing to share and the tokenizer and voice cache are the things to keep per instance. |
| **Kokoro is the only engine** | A second, ONNX engine was ~2× faster (RTF 0.31 vs 0.65) but its delivery drifted in volume and glitched on long paragraphs. It was never the one worth listening to, so it went, and with it a build option, a second set of weights and a second licence. |
| **A rule is skipped by a substring test before the regex engine sees it** | `pronounce.apply` ran all 86 rules against every block, and a `sub` that cannot match is a full scan with a regex engine on top. A word or a phrase rule is a literal inside guards, so the literal has to be in the text: `needle in lowered` is the same scan at C speed and skips 96% of the rules. Deciding whether a rule fires, fetching its pattern and building its replacement moved to `_prepared`, an `lru_cache` keyed on the rules themselves — 86 distinct answers were being recomputed 206,400 times. Measured over 2,400 blocks against the seeded rules: **1.014 s to 0.633 s**, and the sweep that derives the same spoken text went 1.074 s to 0.636 s. Byte-identical output over every block of `tests/corpus` on both phonemisers, with and without phonemes. |
| **A rule's literal narrows the rebuild scan in SQL, not in Python** | `articles_matching` read every block of every built article into Python because a rule may be a regular expression. Most are not: a word or a phrase rule is a literal inside guards, so `b.text LIKE ?` is a superset of what the pattern would match and SQLite reads only the rows that pass it. Measured over 12,000 blocks across 200 ready articles: **48 ms to 5 ms**, and it now grows with the hits rather than with the library. Identical answers against the exhaustive scan across case, substrings of longer words, LIKE wildcards and a regex with no literal. A regex rule still reads them all. |
| **A visual is a block, not a second table** | The alternative was a `figure` table beside `block`, keyed by the block it follows. It would have needed its own ids, its own ordering and its own answer to "what does the player highlight" — and the read-along already has one list and cannot afford two. One `media` column of JSON instead, empty on every prose row. The three visual kinds hold different things, so a column per kind would have been three mostly-empty columns. |
| **The page shows the caption, the audio says the label** | `block.text` is `Table: Ker-CHING 💰`, which is what the synthesiser reads, what search indexes and what the block editor shows. `media["caption"]` is `Ker-CHING 💰` alone, and is what the page prints — a reader can see it is a table. A picture with no caption gets `Figure.` for the listener and nothing under it on the page. Where the FT lifts the title out of the header row, `caption` is left off entirely: printed again below, it read as a second title. |
| **Visuals are opt-in per adapter, and the newsletter walk stays text-only** | `blocks_from_dom` takes `visuals=NO_VISUALS` by default, so a walk that has not been told what a publication's furniture looks like behaves exactly as it did before. FT, Bloomberg, Substack and the generic extractor pass rules; `newsletter.py` does not, because a newsletter is built out of layout tables and `_table_block` cannot tell a two-by-two of prose from data. |
| **An `iframe` is kept by allowlist, never by blocklist** | `EMBED_HOSTS` names fifteen chart tools. Everything else is refused, because an `iframe` is far more often an advert, a consent shim or a beacon — the SpaceX page carries three and not one is a graphic. A new advert network appears more often than a new chart tool does. And the frame is not fetched until the reader presses **Load the chart**: a live chart is a third party the reader has not agreed to. |
| **A picture is fetched, not hotlinked** | Pointing an `<img>` at the publication's own CDN cost three things at once: an article kept offline showed nothing, a paywalled image answered 403 to a reader not signed in, and the publication learned the reader's address. Storage is the cheapest of the four. The fetch runs inside the ingest request, in a pool of four, because that request already fetches the page for `kind=url` — and it is never fatal: a picture that will not download keeps its remote address and the reader hotlinks that one. Named for a hash of its address, so a re-parse writes nothing, two blocks quoting the same chart share a file, and the response can be cached for ever. |
| **selectolax, not BeautifulSoup** | 4.8× faster on the corpus (66 ms vs 314 ms for 6.3 MB), one wheel, no lxml. Its `[class*="Footnotes_base"]` also beats a regex over the class list. |
| **torch from the CPU index** | The default pulls CUDA: 15 nvidia packages, 5.2 GB venv, 9.3 GB image, on a box with no GPU. Pinned in `[tool.uv.sources]`. Now 1.4 GB and 3.3 GB. |
| **4 single-threaded engines, not 1 four-threaded** | RTF 0.562 vs 0.629, an 11% gain. 2×2 gives 0.633 (no gain); 6×1 gives 0.593 (worse). The model is bound more by memory bandwidth than cores — do not expect 4× from 4 cores. |
| **One KModel behind the whole pool** | `KPipeline` builds its own `KModel` unless you hand it one, and the pool built four. Kokoro's own docstring asks for the opposite: "For multiple KPipelines, you should reuse one KModel instance across all of them." Measured on this box: four pipelines each with their own held **4,188 MB** after a build; four sharing one held **1,535 MB**, and rendered no slower — RTF 0.561 against 0.574. The weights are 312 MB of the ~650 MB each instance cost; the rest is a second G2P and torch's slack. |
| **Kokoro is not deterministic, so a shared model is no riskier** | Sharing worried me, because kokoro uses the deprecated `torch.nn.utils.weight_norm`, which mutates the module inside `forward`. Measured instead of assumed. One pipeline, same sentence, three times in a row: max abs difference 0.081 and 0.084. Four independent pipelines, concurrent: 0.088, 0.130, 0.129. Four sharing one model, concurrent: 0.113, 0.104, 0.087. The spread is the model's own; sharing sits inside it. A useful corollary: the block cache is *why a rebuild sounds like the first build*, not only why it is fast. |
| **Every job runs in a child process that then exits** | `import torch` cannot be undone. Deleting it from `sys.modules` leaves the shared libraries mapped and the allocator's arenas grown, so a worker that had built once held **1,006 MB for the rest of its life** against **37 MB** before its first build. The only way to get it back is to leave the process. The parent polls the job table and never imports torch; `drain_jobs` is spawned, takes the whole queue, and dies. Re-measured over three builds: parent **38.3–38.4 MB** in all 66 samples, idle and building, and `torch/lib` never in its maps. The child holds 1,600–1,880 MB on ONNX for one article and **2,892 MB** after two, and gives every byte back **1.2–2.5 s** after the last job. Start-up is **~2 s**, not the ~7 s measured when torch was the default — four ONNX instances load in 1–2 s. Summaries go the same way for the same reason, one size down: `openai` brings httpx and pydantic, and took the worker from 38 MB to 79 MB permanently. |
| **A process's exit is the only way the pool is dropped** | Keeping the pool between jobs was measured against a reload, and a reload is 6.1 s. It was never measured against an idle process holding gigabytes for the rest of the day. There were two answers to that: an idle timer, and the child process's exit. Only one of them ran. The timer needed `JOB_SUBPROCESS=false`, which nobody sets, and returned on its first line under the default for ever. Both it and the mode it served are gone — `_release_idle_engines`, `_drop_engines`, `_trim_heap`, `TTS_IDLE_MINUTES` and `JOB_SUBPROCESS`. A lane hands its queue to a child, the child drains it and exits, and the exit gives every byte back. |
| **One process, one engine** | The pool is built once per child and never replaced. A build job for the other engine is put back on the queue and passed over — `step` resolves the engine before it commits the process to the job, and `claim_job` takes a `skip` set so the released job does not stall the ones behind it. `engines_for` still raises if a second engine is ever asked for, which is a bug rather than a case. This replaces the drop-and-rebuild that used to happen mid-drain: dropping was correct and worked, but it still meant one process importing both torch and onnxruntime, and the failure it guarded against cost 7.5 GB. Measured on one queue holding a job per engine, one instance each: the ONNX child peaked at **717 MB with no `torch/lib` in its maps**, the kokoro child at **1,933 MB with no `onnxruntime` in its maps**, and the parent at 49 MB with neither. |
| **One engine instance per process** | Building a Kokoro engine loads an 82M-parameter model. The web app built a new one for every voice list, preview and phoneme lookup. `tts.shared_engine` keeps one; the worker publishes its first pool instance into the same slot. |
| **A block's cue opens half a gap before its first word** | Landing the cue exactly on the attack means anything slow — a decoder spinning up, a browser a frame behind — starts you inside the first word. The 350 ms between two blocks is split between them. The audio is untouched: decoded output hashes identically, only the map moves. |
| **Ask misaki before writing a spell-out rule** | Its notation is not IPA — capital `A` is the /eɪ/ of "day", capital `I` the /aɪ/ of "eye". Of 45 `SPELL_OUT` entries, 41 produced identical sounds with the rule and without: misaki already spells acronyms out, and better. "CEO" alone is `sˌiˌiˈO`, the natural contour with the stress on the last letter; the rule's "C E O" is `sˈi ˈi ˈO`, every letter its own stressed word and 120 ms longer. Two rules remain, for the words misaki says instead — `ROE` as "roe", `ETH` as the letter eth. Check `engine.phonemes()` before adding another. |
| **Emphasis markers are stripped for speech, not for the page** | `*before*` reached the engine intact and it said "before **asterisk**". The Markdown reader strips inline markers at parse time; a newsletter arrives as HTML and carries the characters straight through. `normalize.EMPHASIS` handles it on the spoken side only, so the page keeps the author's punctuation. Paired markers only — a lone asterisk is a footnote marker or a bullet. Found by reading the worker's log: phonemizer warns "words count mismatch" when espeak splits one token into two, and 47 of 430 blocks tripped it. Almost all were CamelCase names espeak was right about (`JPMorgan` → "JP Morgan"); six were this. |
| **A time on the hour loses its zero minutes** | `8:00am` was read "eight zero zero a m": espeak says the zeroes, and without a space `8am` is "eight" and then "am" the verb. Measured: `8 a.m.` is `ˈAt ˌAˈɛm`, which is right. A time that is *not* on the hour already works — `10:47` is "ten forty seven" — so only the o'clock case and the missing space are touched. |
| **`401(k)` and `INmune` are respellings, measured first** | `401(k)` was "four hundred one k"; `four oh one k` is `fˈɔɹ ˈO wˈʌn kˈA`. `INmune` was "I EN-mune", because a leading `IN` looks like an initialism — while the company's own possessive, `INMune's`, already came out `ɪn mjˈunz`. `InMune` gives exactly that for one changed capital, so every form takes the path misaki already got right. Both are `regex` rules with a `(?!\w)` guard rather than `word` rules: a word rule's trailing `(?![\w'])` refuses to match before an apostrophe, and would have missed the possessive. |
| **Smart punctuation is flattened before the rules run** | Web prose is full of curly apostrophes, and a rule written for `who'll` never matched `who’ll`. |
| **The model's silence is trimmed off every block** | Kokoro pads each clip with ~300 ms of silence in front and ~500 ms behind. Left in, seeking to a block gave a third of a second of dead air, and a 350 ms gap played as 1150 ms. Measured, not guessed: `trim_silence` cut 2% off a 20-minute issue whose blocks are long, and far more where they are short. |
| **ffmpeg stays; opusenc was measured and refused** | It would take 376 MB off the image (ffmpeg drags 168 packages). But its files are 4.9% bigger at the same 32 kbps, and `--music --comp 10` makes that 22%; ffmpeg's libopus tuning simply wins. Encode is 4.83 s against 4.19 s for 275 s of audio — irrelevant next to ~155 s of synthesis, but the size is permanent and the saving is one-off. |
| **float32 to int16 costs nothing audible** | Measured, because it is the conversion opusenc would force: the quantisation error peaks at −90 dBFS, and after encoding the result differs from the float32 path by −47 dBFS while the encoder's own loss is −45 dBFS. It is quieter than the codec's own noise. The file is 0.17% *smaller*. So the 4.9% above is opusenc, not the integers. |
| **Ogg page size was ruled out first** | The files are on a 1.02 s page grid, which looked like the cause. A tone-per-block probe in Chromium showed seeking is sample accurate at that grid, so nothing was changed. Re-encoding finer would have cost ~10% in size for nothing. |
| **One sound, two spellings, both measured** | The espeak notation for LIBOR was not converted from misaki's — it was measured the same way misaki's was. espeak's own phonemisation of "lie bore" is `lˈaɪ bˈɔːɹ`, which is where `lˈaɪbɔːɹ` comes from. Left alone espeak says `lˈɪbɚ`, "LIB-er", so the rule is worth as much there as here. A library that already holds the rule gets the second spelling from a migration, not from seeding: seeding skips anything it has offered before, which is what keeps a deleted built-in deleted. |
| **Shein is "SHEE-in", and both engines said "Shane"** | `ʃˈAn` on misaki, `ʃˈeɪn` on espeak. "Sheein" is `ʃˈiɪn` and `ʃˈiːɪn`, right on both. A regex with `(?!\w)` rather than a word rule, so the possessive comes with it, and case-insensitive because the brand writes itself SHEIN — and misaki spells an all-capital SHEEIN out letter by letter. |
| **"refund" is a house preference, not a correction** | Both engines already read the verb correctly: `ɹəfˈʌnd` on misaki, `ɹᵻfˈʌnd` on espeak, which is the textbook "to refund". The rule puts the noun's stress on every form — "reefund" is `ɹˈifʌnd` and `ɹˈiːfʌnd`. The inflections are named (`s|ed|ing`) rather than left to a bare prefix, because "refundable" keeps the verb's stress and "reefundable" wrecks it: `ɹˈifəndəbᵊl`, "REE-fund-a-bull". Delete the rule on the Voice page to go back to the engines' answer. |
| **Respellings, not IPA** | Every acronym was checked against Kokoro. `GAAP`→`gap` works and anyone can edit it. Exactly one rule needs IPA: `LIBOR`, where Kokoro is already right and every respelling is worse. |
| **NASDAQ, LIBOR, SPAC, NAV already correct** | Kept as explicit rules anyway, so they stay correct if the voice or model changes. |
| **Summaries speak the OpenAI protocol, not a router** | Every provider now offers an OpenAI-compatible endpoint, so one `openai` dependency reaches 14 of them, listed in `summarize.PROVIDERS`. litellm was measured and refused: 183 MB across 114 packages against 21 MB, and the only thing it adds is a provider with no OpenAI endpoint at all — Bedrock, Vertex, SageMaker, which need a signed cloud SDK. |
| **Adding text and choosing how to read it are two jobs** | The Add page carried a voice, a pace, footnote and summary switches, and a "build now" box, all answered before you had seen what the parser made of it. It takes the text in and nothing else now; the article page decides. |
| **Two lanes, not one queue** | A build is the CPU for minutes; a summary is the network for seconds. They run side by side, and `claim_job` only keeps them off the *same* article, where a summary would rewrite the blocks a build is rendering. |
| **A summary lands the moment it does, and a refusal is named** | The pass gathered every section with `pool.map` and read the results as a dict, so the first refusal raised and threw away the summaries that had already arrived beside it. The only record was a line in the worker's log: a summary leaves `article.status` alone, and the reader's status card was tied to that, so the page showed nothing at all. Each call is caught on its own now, each summary is stored as it arrives, and the job carries what failed and why. The usual failure is a free tier's rate limit, which refuses part of a burst and answers the rest. |
| **`article.status` describes the audio, nothing else** | Queueing or running a summary leaves it alone, and a failed summary does not mark the article failed. The job carries its own state; the article's is about whether there is audio. **`completed` is the exception that proves it**: it is a status you can filter for that no article row ever holds. "Listened to the end" lives on the position row, and `db._article_filter` answers `status=completed` as `a.status = 'ready' AND p.finished = 1`. The ready check is not redundant — it keeps a stale position on an article whose audio was dropped out of the answer. |
| **A summary is a block, and it is written before the audio** | Not a column on `section`, which is where it lived in the notebook. Inserting a block moves every id after it, so `summarise` is its own job kind — and it stops when the blocks are written. Nothing in the app builds audio on your behalf. |
| **The summary settings live in the database, not the environment** | The environment is the default; a value saved on the page wins. The other way round, editing the model in the app appeared to do nothing whenever the container set one. |
| **One Opus file per section** | Playback starts before the whole article is built; a failed block re-renders in seconds. 22 min of audio is 4.6 MB at 28 kbps. |
| **The highlight reads the clock; WebVTT is the backstop** | `cuechange` fires when the browser gets round to it — Chromium within ~10 ms, others far looser — and a seek made by media-chrome's own transport changes no cue set at all. A frame loop over the timing map already in the page is 2 ms and browser-independent. The track stays: it is the file's own record of the timings, and it keeps the highlight roughly right in a background tab, where frames stop and audio does not. |
| **media-chrome for transport** | MIT, vendored as one 41 KB gzipped IIFE, no build step. It does *not* touch Media Session — the lock screen is wired by hand in `player.js`. |

---

## Traps

Things that have already bitten once.

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
- **`is_phonemes` is derived, not stored.** It was a column and a checkbox
  beside the fields, which meant it could disagree with them. `Rule.is_phonemes`
  is `bool(misaki or espeak)` and the column has been dropped —
  `migrate._drop_is_phonemes`, which had to run after the step that moved each
  rule's IPA out of `replacement` and into `replacement_misaki`. That move has
  run and was removed; git still has it.
- **The ONNX engine does misaki's job itself.** It phonemises the prose with
  espeak, splices the rule's phonemes in between, and hands the whole string
  to the model as phonemes. That works because both engines share one phoneme
  vocabulary — 114 symbols, checked, misaki's capitals included. Measured:
  `The LIBOR rate rose` is `lˈɪbɚ` untouched and `lˈaɪbɔːɹ` with the rule.
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
- **The two engines offer the same voices under the same names.** They are
  the same voices: same ids, same names, same order. Nothing in a picker
  distinguishes them and nothing needs to — every picker shows one engine's
  voices at a time and the engine select above it says which. The ONNX names
  carried an `(ONNX)` suffix for a while; it repeated on twenty lines what one
  control already said. Four pickers show voices — Default voice and Test the
  voice on the Voice page, voice and quote voice on the article — and all four
  read from the same `_voices_by_engine` payload, so they cannot drift.
- **The build does not run the model.** `docker/bake_model.py` only calls
  `snapshot_download`. It must stay that way: a build assembles an image, it
  does not do the app's work.
- **`en_core_web_sm` must stay pinned.** Kokoro pip-installs a spaCy model at
  first synthesis otherwise, which needs network and write access to the venv.
  Both are wrong in a container.
- **A centred flex row does not centre the thing in the middle of it.**
  "Previous" is wider than "Next", so the pager's page number sat right of
  centre. `grid-template-columns: 1fr auto 1fr` puts it on the true centre.
- **The menu's own rule lost to the bar's.** `.bar-links a:not(.btn)` is more
  specific than `.nav-group a`, so `padding: .5rem .6rem` never reached the
  links folded into the phone menu: they sat 21px tall with no vertical
  padding, against the 37px the rule asks for. Sign out was the only item
  getting it, because `button.link` is less specific still. Invisible until
  Sign out took an icon and the four marks failed to line up. The selector
  names the ancestor now. A sign-out button also needs `height: auto` and
  `justify-content: flex-start`: the shared `button` rule pins it to the
  bar's 2.1rem and centres its content, which is right in the bar and wrong
  in the menu.
- **A nav rule must not repaint a button's label.** `.bar-links a` excluded
  `.cta`, a class nothing carried, so the Add button's text went grey against
  its own dark fill. The exclusion names `.btn` now.
- **Blocks are `<p>`, never `<button>`.** They were buttons once, and every
  attempt to select a paragraph started playback. Seeking is the gutter handle
  and nothing else: tapping the text was an option for a while and it fought
  with selecting a sentence for no gain, since every block has a play button.
- **Named volumes inherit root ownership.** `/data` is created and chowned in
  the image *before* `VOLUME`, or an unprivileged container dies on `mkdir`.
- **Do not pin `ESPEAK_DATA_PATH` in the Dockerfile.** It was set to the arm64
  path, which is wrong on an amd64 image. `tts/kokoro.py` probes both, plus
  Homebrew's prefixes, and warns when it finds nothing.
- **Engine availability probes the dependency, not the wrapper.** The heavy
  import lives inside `__init__`, so `import textcast.tts.kokoro` always
  succeeds. `is_installed` checks `spec.requires`.
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
- **The player tests share one page.** A test that pauses the audio breaks a
  later one that assumed it was playing. State a test depends on, do not
  inherit it.
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
- **A publication puts its byline in the head, not the body.** Bloomberg names
  the writer in `parsely-author`, `sailthru.author` and `author`, and again in
  a byline whose class carries a build hash. The meta tags are the stable
  read; the byline is the fallback. The author is editable on the article, for
  anything pasted.
- **Editing text is cheap; removing a block is not.** Text edits leave the ids
  alone, so the audio and the timing map stay valid and a rebuild re-reads only
  what changed. Removing one moves every id after it, so the audio has to go —
  `replace_blocks` clears it and sets the status, and the media files are
  deleted. The block cache is deliberately kept: it is keyed by the text, so
  the rebuild is an encode and nothing goes back to the model.
- **A reader library raises its own errors, not yours.** A file named `.pdf`
  that is not one made pypdf throw `PdfStreamError` straight through to the
  browser as a 500 — on single uploads as well as batches. The readers turn
  anything they cannot open into `UnsupportedDocument`, and the batch loop
  catches broadly on purpose: one bad file must not cost you the other
  nineteen.
- **A hint button needs somewhere to open on a phone.** `.tip-body` is
  absolutely positioned under its button, which on a 390 px screen put it off
  the right edge with no way to scroll to the rest. Under 44rem it is `fixed`
  to the bottom of the viewport instead, full width less a margin, and scrolls
  inside itself. Every field hint on the Voice page lives in one of these now;
  as paragraphs under the fields they made the form twice as tall.
- **A hint button is sized against its label, not against a button.** `.q` was
  1.15rem beside a .78rem label — half again the height of the words, so it
  read as a control rather than a marker. It is .95rem now, and `.q::after`
  pads the hit area back out to a finger without moving the circle. A tip near
  the foot of the page takes `.tip.up`, which opens it upwards; there is
  nothing below to open into.
- **A checkbox in a `.row` must not stretch.** `.row > *` is `flex: 1 1 9rem`,
  so two checkboxes took half the width each and sat a third of a row apart,
  lined up with nothing above them. `.row > .check` is `flex: 0 0 auto`.
- **A checkbox beside a labelled field centres on the field, not the row.**
  A row is a label plus a box; the checkbox is neither, so `align-self: center`
  hung "Ignore case" above the Note box it belongs beside. Given the height of
  a field and the same bottom edge, it lines up. Only in a row that holds a
  field: a row of nothing but checkboxes still centres.
- **A file input needs a fixed height, not a minimum.** Its own button pushed
  the box taller than the control beside it, and `.row` aligns on the bottom
  edge, so the two sat with their centres a few pixels apart — visible on a
  phone, where there is no room to hide it.
- **`margin-left: auto` strands a control once the bar wraps.** The library's
  filter count sat at the right of whichever row it landed on, level with a
  select it had nothing to do with. Under 44rem it takes a row of its own,
  lined up with the selects. It also names what it counts — "10 articles",
  not "10": a bare number on its own line reads as a stray.
- **A position at the end is the end, whatever the flag says.** The player
  carries `finished`, and it is the only thing that knows the last section
  ended rather than the user having skipped there. But a lost flag left a
  fully played article in "Continue listening" for ever, showing
  **"-1:59:57 left"** — `duration()` given minus three seconds, because
  divmod on a negative renders it that way. The three seconds are real and
  are not a bug: the player's clock runs on the *decoded* audio while
  `article.audio_ms` comes from the build manifest, and Opus files sit on a
  ~1.02 s page grid, so each section decodes a little longer than the
  manifest says. `save_position` now marks a position within `end_slack` of
  the total as finished, `continue_listening` drops such a row anyway, and
  `duration()` refuses to render a negative. The slack is a tenth of the
  article capped at five seconds — flat five seconds is most of a
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
- **A pager needs one filter, not two.** The rows and the count that divides
  them read the same `db._article_filter`. Written apart, a filter added to
  one and not the other is a library that says "26-50 of 300" and shows a
  different 25.
- **The library is ordered by when it was added, not when it was published.**
  A newsletter from last March, read today, belongs at the top with everything
  else that arrived today. The old order fell back to `added_at` whenever
  there was no publication date, so the two were already mixed.
- **The pager shows at one page of one.** Both ends are spent and inert, and
  it still earns its line: where it sits is then something you already know,
  and a control that appears only sometimes is one you have to look for.
- **The filter form carries no page.** Changing a filter starts at page 1,
  which is where the answer to a new filter is. The pager's own links carry
  the filters, or paging would clear them. A page number past the end is
  clamped to the last page: a bookmark outlives the filter that made it.
- **A page that polls `/api/jobs` must filter to itself.** The reader painted
  whatever job came back last over its own progress, so opening an article
  that was building showed someone else's summary a second later.
- **Nothing hidden may sit above a heading.** `.section-head:first-child` drops
  the heading's top margin, and the Voice page put a `<script type="application/
  json">` first in the content block. The script draws nothing, but it is a
  child, so the heading kept a margin no other page had and the page started
  lower than the rest. The payload lives in the scripts block now.
- **A message you cannot close is one you learn to read past.** A failed build
  or a part-refused summary pass keeps its card until another job replaces it.
  Every `.notice` and any box marked `data-dismissible` gets a cross, added by
  one script in `base.html` rather than in six templates. A box with a
  `data-dismiss-key` remembers the dismissal in `localStorage`; the key is the
  job id, so the *next* failure is a new message and shows. A running card is
  not dismissible: it is about to change.
- **A menu with no way out is a trap.** The player sheet had only Escape,
  which a phone does not have: it now closes on its own button and on a tap
  outside. The nav menu does the same.
- **Dark needs its own accent, not just darker greys.** `--tick` is near-black
  in light and blue in dark, and it colours the read-along edge, its ring and
  every checkbox. A grey wash on a black page cannot be tracked by eye.
- **Both dark palettes must carry the same tokens.** Dark is defined twice,
  once for `prefers-color-scheme` and once for `[data-theme="dark"]`. A token
  added to one and not the other is missing depending on how you got there.
  A test compares them.
- **The block cache key must carry every knob that changes the audio.** Voice
  and reading pace are in it. A pace of 1.0 leaves its field empty, so
  everything cached before the setting existed is still a hit.
- **A sqlite3 connection belongs to the thread that opened it.** The build's
  progress callback runs on the render pool, not on the worker thread. Passing
  the worker's connection in failed *every* parallel build with
  `ProgrammingError`, and the job simply recorded itself as failed. Call
  `db.connect()`, which is thread-local, from inside any callback.
- **A summary must be written before the audio, never after.** It is a block;
  inserting one at the head of a section moves every id after it, and the
  timing map is keyed by id. `_summarise` writes the blocks and puts the
  article back to `new`; `claim_job` had marked it `building`, and nothing
  else would have cleared that.
- **Markdown headings were never detected.** `looks_like_markdown` searched
  with `_MD_HEADING`, which is anchored to the ends of the *string*, so a
  pasted document with headings but no list and no link was read as plain
  prose and every title kept its hashes. Detection uses a `re.M` pattern now.
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
- **Three warnings fire on every engine start and none is ours.**
  `weight_norm` is deprecated and kokoro is built on it; kokoro's config asks a
  one-layer LSTM for dropout, which torch ignores and says so; and thinc, under
  spaCy, under misaki, calls `torch.jit.script`, which torch says "may break"
  on Python 3.14. `tts/kokoro._quiet_known_warnings` drops those three by their
  exact text. Do not widen it: `phonemizer  words count mismatch` is a warning
  worth reading, and it is how the stray emphasis markers were found.
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
- **One base image for both build stages.** The builder was
  `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` and the runtime
  `python:3.12-slim-bookworm`. Those are two different CPython builds: the
  venv recorded `version_info = 3.12.12` and ran on 3.12.14. It worked only
  because both put python at `/usr/local/bin` and the ABI is stable across a
  patch release — a minor-version drift on either side would have broken it
  without a word. The builder is the runtime image now, with the `uv` binary
  copied in, which is what uv's own Docker guide asks for. No size change.
- **`vertical-align: middle` is not the middle of the box.** It puts an
  inline box's centre half an x-height above the baseline, which is a font
  metric, not a measurement of the field it sits in. The Add page's "Choose
  Files" button had 5px of box above it and 2px below for exactly that reason.
  `vertical-align: top` is the top of the *line box*, which the input's own
  `line-height: 2.1rem` fixes, so a `margin-top` of `(2.1rem - 2px of border -
  1.55rem) / 2` is the whole answer. Measured 3px above and 3px below, and the
  "no file chosen" text does not move: it is still placed by the same
  line-height as before.
- **A service worker must not answer a byte range from `caches.match`.** An
  `<audio>` element does not fetch a file, it asks for ranges, and on iOS it
  does so for every play, pause and seek. Cache matching is by URL, so the
  Range header is ignored: a request for bytes 500000-600000 came back as
  status 200 with the whole 2,437,998-byte file. The element read the first
  byte it got as the byte it had asked for, so the clock and the audio parted
  company and every seek landed a few blocks late. `cache.put` refuses a 206
  as well, so the write silently rejected and nothing ranged was ever stored.
  `sw.js` now stores whole files and slices one into a real 206.
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
- **The prompt is user-editable, so `format` is the wrong tool.** `str.format`
  reads every brace in the string: a prompt asking for JSON, with a
  `{"summary": "..."}` example in it, raised `KeyError` and every section of
  every article failed with that as the reason. `{text}` is the only
  placeholder there has ever been, so `str.replace` substitutes it and leaves
  the rest alone.
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
- **Deleting summaries must not call `delete_audio`.** It did, and
  `delete_audio` also takes every render only this article wants — so dropping
  one summary sent every *other* block back to the model on the next build,
  minutes of synthesis to undo a paragraph the article was keeping. Removing a
  block is a hand edit by another name, and `edit_blocks` had always kept the
  cache for exactly this reason. `_drop_media` is now the shared piece: the
  files go, and keeping the cache is the separate decision it always was.
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
- **`search_articles` asked for tags a row at a time.** `tags_for_many` was
  written for the library page and does it in one query; the search loop was
  calling `tags_for` per hit beside it. Reach for the one that takes a list
  whenever the rows are already in hand.
- **A cached render can belong to two articles.** Two pieces quoting the same
  paragraph, under the same engine, voice and pace, are one file.
  `cached_renders` subtracts every key another article still wants, or
  dropping one article's audio would silently cost another its cheap rebuild.
- **The cache holds int16; the caller still gets the model's floats.**
  `_speak` returns the freshly synthesised samples, not the round trip. Only
  a *later* build reads the quantised copy, which is where the −90 dBFS was
  measured.
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
- **Equal box gaps are not equal ink gaps.** The theme toggle sat between a
  run of text links and the Add button with an identical 6.39px flex gap on
  both sides, and still looked nearer to Add: `.bar-links a:not(.btn)` carries
  .3rem of its own padding, so the visible gap on that side was .3rem wider,
  while the button's fill is its edge and reserves nothing. The toggle takes
  that .3rem back as a right margin. Measure the boxes *and* look at it.
- **Tables centre their cells.** `table.plain td` was `vertical-align: top`
  and `table.rules td` overrode it to `middle` — which is why the rules table
  looked even and the jobs and keys tables did not. Middle is the default now
  and the override is gone. Every table here puts short cells beside a badge
  or a button, including the one with a wrapping regex in it.
- **`hidden` loses to any rule that sets `display`.** The key box's Endpoint
  row is a `.row`, which is `display: flex`, so it went on showing while
  `element.hidden` read true — and a Playwright check of the property passed
  while the screenshot showed the field. `app.css` now has
  `[hidden] { display: none !important; }`.
- **Two migrations run in sequence for an old library**, and the tests run
  them in that order. `_scope_summary_key` moves a flat `summary_api_key`
  under its endpoint; `_adopt_env_summary_key` takes `TEXTCAST_SUMMARY_API_KEY`
  in, once, where nothing is stored; `_name_summary_keys` turns every
  endpoint-scoped key into a named row and marks the selected one as in use.
  Each deletes what it consumed: left behind, they are a second answer to the
  same question.
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
- **Nothing may load a model inside a request.** The voice picker broke this
  rule for months: every article page called `_voices()`, which built an
  engine, so the first page load after a restart waited for the weights. It
  also made the player tests flaky. `tts.catalogue` reads the voice table from
  the wrapper module instead, and loads nothing. `_phonemes_for` uses
  `tts.loaded_engine`, which never builds one — a phoneme line is a nicety and
  is not worth stalling a web-only process for seconds. `/api/say` may build,
  because the user asked to hear something.
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
- **A test that reads pronunciation rules must ask for a database.**
  `test_rewrites` took no fixture, so its "vs.", "approx." and "YoY" cases read
  whatever `settings.db_path` happened to point at. It passed only while some
  earlier test had left a seeded one behind, and failed the moment the tests
  either side of it changed. It takes `conn` now.
- **Sync Playwright allows one instance per thread.** `tests/test_player.py`
  has one module-scoped `browser` fixture; a test needing isolation takes a
  context from it. A second `sync_playwright()` does not fail loudly — it
  raises inside the `chromium unavailable` guard and the test silently skips.

Fixed, so they can no longer bite:

- **The service worker's `BUILD`.** It is stamped from `__version__` as
  `/sw.js` is served, so the page's `?v=` suffix and the worker's cache names
  cannot drift. Forgetting to bump it by hand once left a stale stylesheet
  alive through a deploy.
- **Tests reaching past `settings.db_path`.** `tests/conftest.py` owns the data
  directory now. A test that opened its own database file watched `normalize()`
  read a different one and silently apply no rules.
- **Re-parse deleting first.** `service.reparse` parses the stored source
  before it touches the library, so a parse error leaves the article, its tags
  and its build options exactly as they were.

## Where it stands

Everything below is done, deployed and on `main`.

**Work by hand commits straight to `main`.** No feature branches unless the
owner says so.

**A background job never does.** It works in a branch and stops there: no
commit to `main`, no merge, no force-push. It reports the branch name and the
owner merges when they have read it. The owner does not watch background jobs
while they run, so anything landing on `main` unwatched is landing unread.

- **Kokoro is the only engine.** Supertonic is gone, with its extra, its
  weights and its licence.
- **Summaries work** against any OpenAI-compatible endpoint, chosen from a
  dropdown of twelve. 30 summaries imported from the old notebook are attached
  to seven articles; they average 358 words against the 150 the prompt now
  asks for, so they read long until made again.
- **The library is nine articles**, most of them `new`: the silence trim and
  the imported summaries both need a rebuild, which the owner is doing by
  hand. Only ~99 of 388 blocks are cached, so it is roughly two hours of
  synthesis, not seconds.
- **The read-along is correct.** The two bugs that made it wrong — the cue at
  a boundary, and the missing highlight on load — are fixed and tested.
- **It is on the public internet**, at `https://textcast.abdullah.run`, behind
  the Caddy container in `~/Developer/home/deploy`. textcast's compose file
  joins that stack's network as `proxy`, so Caddy reaches the app by container
  name and nothing crosses the host — whose firewall rejects 8000 from
  everywhere but the tailnet. Access control is on.
- **Access control, offline caching, the iOS Shortcut, batched rebuilds after
  a rule change, and import/export of the rules** all exist.
- **Pictures, tables and live charts are parsed and shown.** FT, Bloomberg,
  Substack and the generic extractor all keep them, and the reader draws them
  where the prose cites them. Nothing in the library has been re-parsed yet:
  `block.media` is empty for every article stored before this, and **Re-parse**
  on the article page is what fills it in from the saved source.

## Still open

1. **Judge the voice.** Still the only thing that matters and the only thing
   code cannot settle. Speed is measured; quality is not. Listen to a Money
   Stuff issue with `af_heart` and decide whether the quote voice and the
   footnote handling are right. Everything else is downstream of this.
2. **Summaries are still per article, not per section.** A pass stores each
   section as it lands and reports the ones that failed, so a rate limit costs
   only the calls it refused and "Summarise the other N" asks again for just
   those. What is missing is a control on each section, and any pacing: four
   calls go out at once (`MAX_PARALLEL`), which is what trips a free tier
   allowing five in five minutes. There is also no cost estimate and no cache.
   "Summarise all" makes one call per section across the whole library, and a
   confirm box is the entire guard. The 30 imported from the notebook average
   358 words against the 150 the prompt now asks for, so they read long until
   they are made again.
3. **Trimming uses a fixed threshold.** `SILENCE_LEVEL` is 0.01, well above
   Kokoro's noise floor and well below speech. A different engine, or a voice
   that trails off very quietly, would want it measured again.
4. **The pronunciation rebuild is offered, not automatic.** Changing a rule
   names the built articles that use the word and rebuilds them on one click.
   It does not queue them for you, and it does not notice a rule that has
   *stopped* matching text it used to change.
5. **The GPU path is untested.** The wheels resolve, the extras conflict as
   they should and both engines ask the machine at load time, but this box
   has no device, so nothing here has run on one. Nor is there a number:
   every RTF in this file is CPU.
6. **`/login` is a token box, not accounts.** One shared token, one cookie. It
   is the right size for one person behind a tailnet, and the wrong size for
   anything with more than one reader.
7. **`db.articles_matching` still reads every block for a *regex* rule.** A
   word or a phrase rule is narrowed by `LIKE` in SQL first, which is most of
   them. A regular expression has no literal to narrow on and SQLite cannot
   match one, so those still read every ready article's blocks. Measured over
   12,000 blocks: 48 ms against 5 ms. Fine at a few hundred articles; measure
   before it is thousands.
8. **The offline test covers one article, not eviction.** It caches, drops the
   network and reloads, and it now covers a suffix range and a malformed one.
   Nothing exercises the browser evicting a cache under storage pressure, or
   `drop-article`.
   Worth deciding before it bites: `mediaResponse` stores *every* audio file it
   fetches, so listening online quietly fills the offline cache with articles
   nobody asked to keep — in the same cache as the ones they did. Under storage
   pressure the browser evicts without knowing the difference, so an article
   marked for the commute can be thrown away to make room for one played once.
   Two caches, or only storing what `cache-article` asked for, would separate
   them.
9. **Mail polling has no test.** `mail.py` talks IMAP and nothing stands in
   for a server, and it now runs inside the worker rather than on a timer.
10. **Article hits and block hits are ranked separately.** `search` puts
   metadata matches first and FTS matches after, rather than in one order.
   Right at this size; revisit past a few thousand articles.
11. **The bookmarklet's token is the session token.** It is baked into the
    bookmarklet in clear, it sits in the bookmarks bar, and it can delete the
    whole library. A scoped ingest-only token would cap what a stolen one is
    worth. Nor does anything rate-limit `/api/ingest`, which is the one route
    that takes a credential in a body from anywhere on the internet.
12. **A picture that failed to download is never tried again.** The block
    keeps its remote address and the reader hotlinks it, which is the right
    fallback and not a repair. Nothing re-runs `pictures.fetch_for` except a
    re-parse, and nothing in the app says which articles are still pointing
    outside. The Markdown export has the same gap: `to_markdown` writes the
    address the picture came from, not the copy on disk, so a text export
    opened offline shows nothing. The audio export does carry them —
    `media/<slug>/` goes into the zip whole, `images/` included.
    The service worker does not cache them either: it caches what the page
    asks for, and a lazy-loaded picture below the fold is never asked for
    while the article is being saved.
13. **Only the audio's own cue says a chart is there.** The build speaks
    "Table: Ker-CHING", and the player can stop on the block if **Pause at a
    chart or table** is ticked in the sheet. Nothing tells you *before* you
    start that an article has visuals in it, and the library shows no mark.
14. **Newsletters get no visuals.** `newsletter.py` walks with
    `NO_VISUALS`, because it reads leaf table cells and a layout table is not
    distinguishable from a small data table there. A Substack issue arriving
    by email is fine — `SubstackAdapter` sits before it in the registry — but
    anything else loses its charts.
15. **A hand-written summary is not protected.** Nothing marks a summary block
    as yours, so "Summarise again" replaces it with the model's, and "Delete
    summaries" removes it with the rest. The reader now says which model wrote
    the summaries and stays silent where it does not know, which makes the
    hazard visible but does not stop it. Marking a summary block that was
    edited by hand would; it was judged not worth a column yet.

## Conventions

- **Comments say why, not what.** Most usefully: why an obvious alternative was
  rejected, and what broke to make the code look like this.
- **Test names are sentences** describing the behaviour, and the docstring
  carries the reason where the name cannot.
- **The parse corpus is `tests/corpus`.** Ten saved pages that
  `test_ingest.py` parses, and that `test_visuals.py` reads again for the
  pictures and tables in them. It sits in the tests because `data/` is
  ignored, and a test may not depend on a copy that is not in the repository.
  Two were added for the visuals: the Alphaville post whose argument rests on
  a ready reckoner, and a Substack fixture written by hand — nothing in the
  library was one, and a fixture can carry the subscribe widget, the avatar
  and the Cloudinary srcset that the filters exist to handle.
- **Measure before switching engines, parsers or dependencies.** Every table
  entry above exists because a guess was wrong at least once.
- `uv run ruff check src tests` before committing. Formatting is not enforced;
  do not reformat the tree.
