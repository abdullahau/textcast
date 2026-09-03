# textcast — working notes

Newsletters, articles and documents turned into a private audio reader.
Self-hosted, offline-capable, deploys anywhere.

This file is for whoever picks the project up next. It records the decisions
that are not obvious from the code, the things that were measured rather than
assumed, and what is still open. It does not record anything you can ask the
repository for: `git log` says what landed, `du -sh data/*` says how big the
library has got, and `uv run pytest` says how many tests there are.

**Before you touch the parsers, the player, the build worker, the service
worker or the summary keys, read the matching section of
[`docs/traps.md`](docs/traps.md).** Everything in it has already bitten once,
filed by the part of the code it bites. It is a separate file because it is
only wanted once you are in the code it describes, and this one is read at the
start of every session.

---

## Run it

```bash
docker compose up -d --build   # http://127.0.0.1:8000, app + worker
docker compose logs -f worker  # builds and mail polling land here

uv sync --extra cpu --extra kokoro --extra kokoro-onnx --extra web \
        --extra documents --extra summaries --group dev
uv run pytest
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

`docs/traps.md` sits beside this file and holds everything that has already
bitten once, under the part of the code it bites.

---

## Where the data lives

Everything is under `TEXTCAST_DATA_DIR` (`./data`), and nothing outside it
matters for a backup. `du -sh data/*` says how big each part has got.

```
data/
├── textcast.db  the library: articles, sections, blocks, tags, jobs,
│                positions, pronunciation rules, settings, and the FTS5 index
├── media/       the audio and the stored pictures, one directory per slug
├── sources/     the original HTML/eml/txt each article came from
└── cache/       one raw render per block, keyed by content hash
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
the *spoken* text, the engine, the voice and the pace. Uncompressed, so it
runs roughly an order of magnitude larger than the Opus it produced. That is
the trade it exists to make: measured on a 33-minute article whose blocks were
all cached, the rebuild took **32 seconds** and added no cache file — an
encode, with nothing going back to the model. Deleting it costs only time.

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
| **kokoro-onnx renders faster on less memory, and phonemises differently** | The same v1.0 weights through onnxruntime instead of torch. Measured on this box against the pool the worker actually uses — four instances, one thread each: **RTF 0.456 against 0.557**, 18% faster, and **530 MB resident against 1,512 MB** after a render. Four instances load in 1.5 s against 7 s, and the wheels are ~40 MB against torch's 1.4 GB. What it does *not* share is the G2P: kokoro reaches espeak through misaki, this reaches it through phonemizer. Every pronunciation rule here still lands — checked word by word — except the one written in IPA. See *Pronunciation, phonemes and the rules* in [`docs/traps.md`](docs/traps.md). |
| **One onnxruntime session behind the whole pool** | The same trade as `KModel`, and the same size. Four instances each opening the 311 MB model held **1,595 MB** after a render; four sharing one session held **530 MB** and rendered no slower (RTF 0.456 against 0.451). onnxruntime's `Run` is thread-safe, so the session is the thing to share and the tokenizer and voice cache are the things to keep per instance. |
| **Speed does not earn an engine its place** | Supertonic was ~2× faster than kokoro (RTF 0.31 vs 0.65) and went anyway: its delivery drifted in volume and glitched on long paragraphs, so it was never the one worth listening to. Out with it went a build option, a second set of weights and a second licence. Not to be confused with kokoro-onnx, which is the *same* weights through a different runtime and is the default. |
| **A rule is skipped by a substring test before the regex engine sees it** | `pronounce.apply` ran all 86 rules against every block, and a `sub` that cannot match is a full scan with a regex engine on top. A word or a phrase rule is a literal inside guards, so the literal has to be in the text: `needle in lowered` is the same scan at C speed and skips 96% of the rules. Deciding whether a rule fires, fetching its pattern and building its replacement moved to `_prepared`, an `lru_cache` keyed on the rules themselves — 86 distinct answers were being recomputed 206,400 times. Measured over 2,400 blocks against the seeded rules: **1.014 s to 0.633 s**, and the sweep that derives the same spoken text went 1.074 s to 0.636 s. Byte-identical output over every block of `tests/corpus` on both phonemisers, with and without phonemes. |
| **A rule's literal narrows the rebuild scan in SQL, not in Python** | `articles_matching` read every block of every built article into Python because a rule may be a regular expression. Most are not: a word or a phrase rule is a literal inside guards, so `b.text LIKE ?` is a superset of what the pattern would match and SQLite reads only the rows that pass it. Measured over 12,000 blocks across 200 ready articles: **48 ms to 5 ms**, and it now grows with the hits rather than with the library. Identical answers against the exhaustive scan across case, substrings of longer words, LIKE wildcards and a regex with no literal. A regex rule still reads them all. |
| **A visual is a block, not a second table** | The alternative was a `figure` table beside `block`, keyed by the block it follows. It would have needed its own ids, its own ordering and its own answer to "what does the player highlight" — and the read-along already has one list and cannot afford two. One `media` column of JSON instead, empty on every prose row. The three visual kinds hold different things, so a column per kind would have been three mostly-empty columns. |
| **The page shows the caption, the audio says the label** | `block.text` is `Table: Ker-CHING 💰`, which is what the synthesiser reads, what search indexes and what the block editor shows. `media["caption"]` is `Ker-CHING 💰` alone, and is what the page prints — a reader can see it is a table. A picture with no caption gets `Figure.` for the listener and nothing under it on the page. Where the FT lifts the title out of the header row, `caption` is left off entirely: printed again below, it read as a second title. |
| **Visuals are opt-in per adapter, and the newsletter walk stays text-only** | `blocks_from_dom` takes `visuals=NO_VISUALS` by default, so a walk that has not been told what a publication's furniture looks like behaves exactly as it did before. FT, Bloomberg, Substack and the generic extractor pass rules; `newsletter.py` does not, because a newsletter is built out of layout tables and `_table_block` cannot tell a two-by-two of prose from data. |
| **An `iframe` is kept by allowlist, never by blocklist** | `EMBED_HOSTS` names fifteen chart tools. Everything else is refused, because an `iframe` is far more often an advert, a consent shim or a beacon — the SpaceX page carries three and not one is a graphic. A new advert network appears more often than a new chart tool does. And the frame is not fetched until the reader presses **Load the chart**: a live chart is a third party the reader has not agreed to. |
| **A picture is fetched, not hotlinked** | Pointing an `<img>` at the publication's own CDN cost three things at once: an article kept offline showed nothing, a paywalled image answered 403 to a reader not signed in, and the publication learned the reader's address. Storage is the cheapest of the four. The fetch runs inside the ingest request, in a pool of four, because that request already fetches the page for `kind=url` — and it is never fatal: a picture that will not download keeps its remote address and the reader hotlinks that one. Named for a hash of its address, so a re-parse writes nothing, two blocks quoting the same chart share a file, and the response can be cached for ever. |
| **Re-parse replaces nothing when the parse has not changed** | Seven of the nine sources here re-parse to exactly what is already stored, and each was being deleted and rewritten — which takes the article out of `ready` and orphans audio that is still correct. Over a library that reads as "re-parsing broke everything". `_same_article` compares the section titles, every block as it would be stored, and the metadata. It ignores `media["file"]`, which the picture fetch writes *after* the store: compared, no article with a picture could ever be found unchanged. |
| **Re-parse queues nothing** | It used to queue a build per article, which over a library is the machine for the rest of the day and nobody asked for it. The audio is invalid either way once the ids move; *when* to spend the machine is the owner's call, and the page says the ids moved rather than acting on it. |
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

## Where it stands

Everything here is on `main`. Commit straight to it; the owner asked for no
feature branches unless they say so. `git log` is the record of what landed
and when — this file does not repeat it, and a list of "what the library holds
today" is stale the day after it is written. For the current state ask the
thing itself: the library page, `docker compose ps`, `du -sh data/*`, and

```bash
sqlite3 -header -column data/textcast.db \
  'SELECT status, COUNT(*) FROM article GROUP BY status'
```

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
13. **Re-parsing an article loses your place in it.** The `position` row goes
    with the article by foreign key, and `reparse` carries the tags and the
    build options across but not that. It is defensible as it stands, because
    a re-parse that changes nothing no longer replaces the row at all, and one
    that does change something has moved the block ids and invalidated the
    audio the position pointed into. It is still a thing that happens without
    being said.
14. **Only the audio's own cue says a chart is there.** The build speaks
    "Table: Ker-CHING", and the player can stop on the block if **Pause at a
    chart or table** is ticked in the sheet. Nothing tells you *before* you
    start that an article has visuals in it, and the library shows no mark.
15. **Newsletters get no visuals.** `newsletter.py` walks with
    `NO_VISUALS`, because it reads leaf table cells and a layout table is not
    distinguishable from a small data table there. A Substack issue arriving
    by email is fine — `SubstackAdapter` sits before it in the registry — but
    anything else loses its charts.
16. **A hand-written summary is not protected.** Nothing marks a summary block
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
- **The parse corpus is `tests/corpus`.** Saved pages that `test_ingest.py`
  parses and `test_visuals.py` reads again for the pictures and tables in
  them. It sits in the tests because `data/` is ignored, and a test may not
  depend on a copy that is not in the repository. The Substack page is a
  fixture written by hand: nothing in the library was one, and a fixture can
  carry the subscribe widget, the avatar and the Cloudinary srcset that the
  filters exist to refuse.
- **Measure before switching engines, parsers or dependencies.** Every entry
  in the decisions table exists because a guess was wrong at least once.
- **A trap goes in `docs/traps.md`, under the part of the code it bites.**
  Say what broke and why the code looks the way it does — the incident is the
  reason the rule is believable. What does *not* go anywhere is a number the
  repository can be asked for: a test count, a directory size, a list of what
  the library holds today. Each is stale the day after it is written, and the
  command that answers it is shorter than the sentence recording it.
- `uv run ruff check src tests` before committing. Formatting is not enforced;
  do not reformat the tree.
