# textcast — working notes

Newsletters, articles and documents turned into a private audio reader.
Self-hosted, offline-capable, deploys anywhere.

This file is read at the start of every session, so it holds only what a
session needs before it opens a file. Everything else has its own home:

| Read | When |
| --- | --- |
| [`docs/traps.md`](docs/traps.md) | **Before touching** the parsers, the player, the build worker, the service worker, the cache or the summary keys. Filed by the part of the code it bites. |
| [`docs/decisions.md`](docs/decisions.md) | Before overturning a design choice. Every entry is a measurement. |
| `PLAN.md` | What is being worked on now, and what is still open. Untracked. |
| `README.md` | What the app is, and how someone else installs it. |

Nothing here that the repository can be asked for: `git log` says what landed,
`du -sh data/*` how big it is, `uv run pytest` how many tests there are.

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

Four things that are not obvious and cost an afternoon each:

- **`--extra cpu` is not optional.** Without it torch comes from PyPI, which is
  the CUDA build and ~4 GB of NVIDIA runtime. `--extra cuda` is the other one;
  uv refuses both.
- **There is no command line.** `cli.py` was deleted — the app is the interface
  and a second one drifts. `python -m textcast` runs the build worker.
- **Docker is the deployment *and* the dev loop.** `./data` is a bind mount and
  both services run as `TEXTCAST_UID:TEXTCAST_GID` from `.env`; get that wrong
  and SQLite says "attempt to write a readonly database".
  `docker-compose.override.yml` mounts `./src` over the image's copy, so a host
  edit is the code that runs. For a server, leave the override out.
- **espeak-ng must be on the system** (`brew install espeak-ng`, or `apt
  install espeak-ng espeak-ng-data`). Missing, the failure is a bare "phontab:
  No such file or directory" at the first synthesis, not at import.

Python is 3.12, pinned: kokoro and misaki declare `requires-python <3.13`.

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
table's cells.

If you are tempted to store text anywhere else, don't.

## Layout

```
src/textcast/
├── document.py     Article / Section / Block. Block.spoken() is the seam
│                   between what is shown and what is spoken.
├── normalize.py    Structural rewrites: money, percentages, quarters, spans.
├── pronounce.py    Word-level rules, editable in the UI. Respellings and IPA.
├── summarize.py    Section summaries from any OpenAI-compatible endpoint.
├── prefs.py        Default engine, voice, quote voice and pace, in `setting`.
├── accounts.py     The one account: username, password hash, avatar, and the
│                   two secrets — the session and the ingest key.
├── pictures.py     Fetches every picture an article cites into
│                   `media/<slug>/images/`, and sweeps what nothing wants.
├── netguard.py     Checks an address is public before service.fetch or
│                   pictures._download connects to it, hop by hop through
│                   any redirect.
├── ingest/         one adapter per publication + the shared DOM walker
│   ├── dom.py      selectolax helpers (lexbor)
│   ├── base.py     the shared walker + junk pruning
│   ├── extract.py  content-density fallback (replaces readability-lxml)
│   ├── documents.py text, Markdown, PDF, DOCX
│   ├── visuals.py  what a picture, a table and a chart are, and what is
│   │               furniture wearing one. Asked once, per publication.
│   └── bloomberg / ft / economist / substack / newsletter / generic
├── tts/            engine registry and the two Kokoro wrappers; shared_engine
│                   is the one instance a process ever loads
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

Everything lives under `TEXTCAST_DATA_DIR` (`./data`) and nothing outside it
matters for a backup: `textcast.db`, `media/` (audio and stored pictures),
`sources/` (the bytes each article arrived as, which Re-parse replays),
`avatar/` and `cache/` (one raw render per block). Only `sources/` and
`media/<slug>/images/` cannot be made again. See
[`docs/decisions.md`](docs/decisions.md) for what each may safely lose.

## Where it stands

Everything is on `main`. Commit straight to it; the owner asked for no feature
branches unless they say so. For the current state ask the thing itself: the
library page, `docker compose ps`, `du -sh data/*`, and

```bash
sqlite3 -header -column data/textcast.db \
  'SELECT status, COUNT(*) FROM article GROUP BY status'
```

What is open, and what is being worked on, is in `PLAN.md`.

## Conventions

- **Comments say why, not what.** Most usefully: why an obvious alternative was
  rejected, and what broke to make the code look like this.
- **Test names are sentences** describing the behaviour; the docstring carries
  the reason where the name cannot.
- **The parse corpus is `tests/corpus`.** It sits in the tests because `data/`
  is ignored, and a test may not depend on a copy that is not in the
  repository.
- **Measure before switching engines, parsers or dependencies.** Every entry in
  `docs/decisions.md` exists because a guess was wrong at least once.
- **A trap goes in `docs/traps.md`**, under the part of the code it bites. Say
  what broke: the incident is what makes the rule believable. What goes nowhere
  is a number the repository can be asked for — each is stale the day after,
  and the command that answers it is shorter than the sentence recording it.
- **Bump `__version__` with any change under `static/`.** The service worker
  names its caches after it, so an edit inside one version is invisible to
  every installed client.
- `uv run ruff check src tests` before committing. Formatting is not enforced;
  do not reformat the tree.
