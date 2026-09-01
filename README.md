# textcast

Turn newsletters, articles, notes and documents into a private audio reader you
can open on your phone. Self-hosted, offline-capable, deployable anywhere.

It reads footnotes **where they are cited**, marks block quotes, rewrites
`$72mm` and `£5bn` so they are spoken as money, and highlights each paragraph as
it is read — with a handle beside every paragraph to jump the audio there.

---

## What it does

1. **Take anything in.** Pasted text or Markdown, a URL, a PDF, a Word file, a
   saved `.html` page, a `.eml` newsletter, a bookmarklet click on a paywalled
   article, or the Android share sheet.
2. **Parse it properly.** Per-publication adapters (Bloomberg, FT) with a
   content-density fallback for everything else. Footnotes are inlined into the
   sentence that cites them.
3. **Speak it.** Kokoro by default, one block at a time, encoded to Opus — one
   file per section, so playback starts before the whole article is built.
4. **Read along.** The player highlights the current paragraph and follows it
   down the page. The text stays selectable, so you can still copy from it.
5. **Take it with you.** Install to the home screen, keep articles offline,
   control it from the lock screen.

## Quick start

```bash
uv sync --extra kokoro --extra web --extra documents
uv run textcast add https://example.com/an-article --tag Reading
uv run textcast worker --once          # build the audio
uv run textcast serve                  # http://127.0.0.1:8000
```

Kokoro needs **espeak-ng** on the system (`brew install espeak-ng`, or
`apt install espeak-ng espeak-ng-data`). textcast finds it automatically in the
usual places. The first build downloads the model into `data/`.

## Commands

| Command | What it does |
| --- | --- |
| `textcast add <file\|url\|.eml>` | Ingest into the library and queue the audio |
| `textcast build <file\|url>` | One-shot: parse and synthesise, no database |
| `textcast parse <file\|url>` | Show what the parser found, without building |
| `textcast library` | List what you have; `--tags` lists tags, `--tag X` filters |
| `textcast search <query>` | Full-text search every article, located in the audio |
| `textcast worker` | Process queued builds; `--once` runs a single job |
| `textcast mail` | Fetch unread newsletters over IMAP |
| `textcast engines` / `voices` | What is installed, and what it can sound like |
| `textcast serve` | Run the web app |

## Tags

Tags are the only way things are grouped. There is no separate notion of a
newsletter or a folder: a detected newsletter simply becomes a tag, alongside
any you make yourself. Add them while adding an article, or on the article page,
and filter the library by one from the dropdown.

## Newsletters by mailbox

Point `textcast mail` at a dedicated address or a filtered folder and every
unread issue becomes a queued article, tagged with its publication. IMAP only:
the app connects outwards, so there is no mail server to run and no spam
surface. It checks the `List-Id` headers first, so personal mail stays unread.

```bash
export TEXTCAST_IMAP_HOST=imap.fastmail.com
export TEXTCAST_IMAP_USER=you@example.com
export TEXTCAST_IMAP_PASSWORD=an-app-password
uv run textcast mail
```

`deploy/textcast-mail.timer` runs that every 30 minutes under systemd.

## Reading short forms aloud

Financial writing is full of things a speech model mangles. Everything is
rewritten on the way to the engine, and never on screen:

| Written | Spoken |
| --- | --- |
| `$72mm`, `£5bn`, `€300k` | 72 million dollars, 5 billion pounds, 300 thousand euros |
| `150bps`, `12x`, `2.5%` | 150 basis points, 12 times, 2.5 percent |
| `Q3`, `FY2024`, `2019-21` | quarter 3, fiscal year 2024, 2019 to 2021 |
| `SEC`, `S&P`, `M&A` | S E C, S and P, M and A |
| `EBITDA`, `NASDAQ` | left alone — these are words |

`src/textcast/normalize.py` holds the tables; add to them freely.

## Choosing a voice engine

Both engines implement the same three-method interface, so switching is one
environment variable. Measured here on 4 ARM cores with no GPU, against a real
659-character paragraph:

| Engine | RTF | Speed | Rate | Install |
| --- | --- | --- | --- | --- |
| **Kokoro-82M** (default, `af_heart`) | 0.65 | 1.5× real time | 24 kHz | 113 packages, 4.9 GB |
| Supertonic 3 (4 steps) | 0.31 | 3.2× | 44.1 kHz | 25 packages, 144 MB |
| Supertonic 3 (8 steps) | 0.49 | 2.0× | 44.1 kHz | " |

Supertonic is roughly twice as fast and needs only ONNX Runtime, but its
delivery is thinner — volume drifts and long paragraphs can glitch. Kokoro is
the default because it sounds steadier, and it offers 54 voices against
Supertonic's 10.

```bash
uv sync --extra supertonic
TEXTCAST_TTS_ENGINE=supertonic TEXTCAST_TTS_VOICE=M1 uv run textcast serve
```

Voice, quote voice, engine and footnote handling are set **per article** — when
you add it, or on its page afterwards. Nothing is inherited from a folder or a
feed.

`TEXTCAST_TTS_STEPS` trades quality against speed on Supertonic: 2 is fastest,
8 is the vendor default, 4 is the best trade on a small CPU.

## The player

The two hard parts are not hand-written:

- **Sync is WebVTT.** Each section ships a metadata track whose cue ids are
  block ids, so the browser runs its own timing algorithm and fires `cuechange`
  as each block starts. No timing map to search, no drift.
- **Transport is [media-chrome](https://github.com/muxinc/media-chrome)** (MIT,
  vendored as one 41 KB gzipped file, no build step) — play/pause, seek bar,
  time display, playback rate, keyboard, ARIA.

What is custom is what no library provides: highlighting, the per-paragraph
seek handle, section advance, and Media Session for the lock screen.

Paragraphs are plain `<p>` elements, so selecting and copying works normally.
Seeking is the small handle in the gutter, or — if you switch it on in the
player sheet — a tap on the text itself, which still yields to a selection.

Both are covered by browser tests in `tests/test_player.py`, driven through
Chromium rather than asserted by inspection.

## Deploying with Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Two services, `app` and `worker` (CPU limited so synthesis cannot starve the web
process), and one volume `/data` holding the database, the model, the audio and
the saved sources.

textcast takes no view on how you reach it. Put a reverse proxy, a VPN or a
tailnet in front, or nothing at all on a private LAN. Auth is off by default,
which suits a private network; set `TEXTCAST_REQUIRE_AUTH=1` and a token for
anything internet-facing. `TEXTCAST_HOST` and `TEXTCAST_PORT` control where it
listens.

## Data model

The **block** is the unit — a paragraph, quote, list item, footnote or summary
is one row with a stable id. Reading, listening, highlighting, seeking and
search all read the same table, so they cannot drift apart. SQLite in WAL mode,
with an FTS5 index over block text.

Original sources are kept, so a parser fix can be replayed with **Re-parse**
without re-fetching. Every synthesised block is cached by content hash, so a
rebuild after an edit takes seconds rather than minutes.

## Adding a publication

One file and one line:

```python
# src/textcast/ingest/economist.py
class EconomistAdapter:
    name = "economist"

    def matches(self, url, tree):
        return "economist.com" in url

    def parse(self, tree, url=""):
        ...
```

Register it in `ingest/__init__.py` ahead of `GenericAdapter`, which always
matches and so must stay last.

## Tests

```bash
uv run pytest                        # 56 tests
uv run playwright install chromium   # once, for the browser tests
```

## Layout

```
src/textcast/
├── document.py     the block model
├── normalize.py    money, abbreviations and initialisms for speech
├── ingest/         adapters + the shared DOM walker
│   ├── dom.py      selectolax helpers
│   ├── extract.py  content-density fallback
│   └── documents.py text, Markdown, PDF and DOCX readers
├── tts/            engine registry; supertonic.py, kokoro.py
├── audio.py        synthesis, Opus encoding, WebVTT
├── db.py           SQLite, search, tags, jobs, positions
├── migrate.py      schema migrations, run on every start
├── jobs.py         the build worker
├── service.py      ingestion shared by the CLI and the web app
├── mail.py         IMAP newsletter fetch
└── web/            FastAPI, templates, one stylesheet, the player
```

## Licence

MIT. Kokoro's weights are Apache-2.0; Supertonic 3's are OpenRAIL-M.
Bundles [media-chrome](https://github.com/muxinc/media-chrome) (MIT).
