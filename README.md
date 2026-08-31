# textcast

Turn newsletters and articles into a private audio reader you can open on your
phone. Self-hosted, offline-capable, and reachable only on your own tailnet.

It reads footnotes **where they are cited**, marks block quotes, groups
newsletter issues into series, and highlights each paragraph as it is spoken —
so you can tap any paragraph to jump the audio there.

---

## What it does

1. **Take anything in.** A URL, a saved `.html` page, a `.eml` newsletter, a
   bookmarklet click on a paywalled article, or the Android share sheet.
2. **Parse it properly.** Per-publication adapters (Bloomberg, FT) with a
   content-density fallback for everything else. Footnotes are inlined into the
   sentence that cites them.
3. **Speak it.** Supertonic 3 on ONNX, one block at a time, encoded to Opus —
   one file per section, so playback starts before the whole article is built.
4. **Read along.** The player highlights the current paragraph and follows it
   down the page. Tap a paragraph to hear it.
5. **Take it with you.** Install to the home screen, keep articles offline,
   control it from the lock screen.

## Quick start

```bash
uv sync --extra supertonic --extra web
uv run textcast add https://example.com/an-article
uv run textcast worker --once          # build the audio
uv run textcast serve                  # http://127.0.0.1:8000
```

The first build downloads the model (~400 MB) into `data/`.

## Commands

| Command | What it does |
| --- | --- |
| `textcast add <file\|url\|.eml>` | Ingest into the library and queue the audio |
| `textcast build <file\|url>` | One-shot: parse and synthesise, no database |
| `textcast parse <file\|url>` | Show what the parser found, without building |
| `textcast library` | List what you have; `--series` lists newsletters |
| `textcast search <query>` | Full-text search every article, located in the audio |
| `textcast worker` | Process queued builds; `--once` runs a single job |
| `textcast mail` | Fetch unread newsletters over IMAP |
| `textcast engines` / `voices` | What is installed, and what it can sound like |
| `textcast serve` | Run the web app |

## Newsletters

Newsletters are the main input, so they get their own handling.

- **Series.** Issues are grouped automatically — Bloomberg's own analytics name
  the newsletter, and `.eml` messages carry a `List-Id`. Each series has its own
  voice and auto-build settings at `/series/<name>`.
- **Chrome removal.** "View this email in your browser", tracking pixels,
  unsubscribe footers and social rows never reach the audio.
- **By mailbox.** Point `textcast mail` at a dedicated address or a filtered
  folder and every unread issue becomes a queued article. IMAP only: the app
  connects outwards, so there is no mail server to run and no spam surface.

```bash
export TEXTCAST_IMAP_HOST=imap.fastmail.com
export TEXTCAST_IMAP_USER=you@example.com
export TEXTCAST_IMAP_PASSWORD=an-app-password
uv run textcast mail
```

`deploy/textcast-mail.timer` runs that every 30 minutes under systemd.

## Choosing a voice engine

Both engines implement the same three-method interface, so switching is one
environment variable. Measured here on 4 ARM cores with no GPU, against a real
659-character paragraph:

| Engine | RTF | Speed | Rate | Install |
| --- | --- | --- | --- | --- |
| **Supertonic 3** (default, 4 steps) | 0.31 | 3.2× real time | 44.1 kHz | 25 packages, 144 MB |
| Supertonic 3 (8 steps) | 0.49 | 2.0× | 44.1 kHz | " |
| Kokoro-82M | 0.65 | 1.5× | 24 kHz | 113 packages, 4.9 GB |

Supertonic needs only ONNX Runtime. Kokoro needs PyTorch, spaCy and the
espeak-ng shared library, and offers 54 voices against Supertonic's 10.

```bash
uv sync --extra kokoro
TEXTCAST_TTS_ENGINE=kokoro TEXTCAST_TTS_VOICE=af_heart uv run textcast serve
```

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

What is custom is what no library provides: highlighting, tap-to-seek, section
advance, and Media Session for the lock screen.

Both are covered by browser tests in `tests/test_player.py`, driven through
Chromium rather than asserted by inspection.

## Deploying with Docker

The app never binds a host port. It shares the Tailscale container's network
namespace, so your tailnet is the only way in.

```bash
cp .env.example .env      # set TS_AUTHKEY, and IMAP details if you want them
docker compose up -d --build
```

Then open `https://textcast.<your-tailnet>.ts.net` from any of your devices and
add it to the home screen.

Three services: `tailscale` (the network), `app` (web), `worker` (synthesis, CPU
limited so the web process stays responsive). One volume, `/data`, holds the
database, the model, the audio and the saved sources.

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
uv run pytest                    # 50 tests
uv run playwright install chromium   # once, for the browser tests
```

## Layout

```
src/textcast/
├── document.py     the block model
├── ingest/         adapters + the shared DOM walker
│   ├── dom.py      selectolax helpers
│   └── extract.py  content-density fallback
├── tts/            engine registry; supertonic.py, kokoro.py
├── audio.py        synthesis, Opus encoding, WebVTT
├── db.py           SQLite, search, jobs, positions
├── jobs.py         the build worker
├── service.py      ingestion shared by the CLI and the web app
├── mail.py         IMAP newsletter fetch
└── web/            FastAPI, templates, one stylesheet, the player
```

## Licence

MIT. Supertonic 3's weights are OpenRAIL-M; Kokoro's are Apache-2.0.
