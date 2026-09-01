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
   article, the Android share sheet, or an iOS Shortcut.
2. **Parse it properly.** Per-publication adapters (Bloomberg, FT) with a
   content-density fallback for everything else. Footnotes are inlined into the
   sentence that cites them.
3. **Speak it.** Optionally summarise each section first, then Kokoro reads
   it one block at a time, encoded to Opus — one
   file per section, so playback starts before the whole article is built.
4. **Read along.** The player highlights the current paragraph and follows it
   down the page. The text stays selectable, so you can still copy from it.
5. **Take it with you.** Install to the home screen, keep articles offline,
   control it from the lock screen.

## Quick start

```bash
uv sync --extra kokoro --extra web --extra documents --extra summaries
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

## Sharing to it from a phone

Android reads the web app manifest, so installing textcast to the home screen
puts it in the share sheet. iOS has no Web Share Target, so it needs a Shortcut.
Build it once:

1. Open Shortcuts, make a new shortcut, and add **Get Contents of URL**.
2. Set the URL to `https://your-host/api/ingest`.
3. Set Method to **POST** and Request Body to **Form**.
4. Add a field `kind` with the value `url`.
5. Add a field `url` and set its value to **Shortcut Input**.
6. If access control is on, add a header `x-textcast-token` holding your token.
7. In the shortcut details, turn on **Show in Share Sheet** and accept URLs.

The same steps are on the Add page, with your host filled in. Anything shared
this way lands in the library parsed and unbuilt, like anything else.

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

## Summaries

Optional, off unless you ask for it. Each section gets a two or three sentence
summary written **as a block** at the top of that section, so it is read aloud
before the section itself, highlighted like any other paragraph, searchable,
and hideable from the player.

The endpoint is OpenAI-compatible, which every provider now speaks. Point it
wherever you like on the **Summaries** page — the model, the endpoint, the key
and the prompt are all editable there, and the key is stored on your machine:

Pick one from the dropdown and it fills in the address and suggests models:
Gemini, OpenAI, Anthropic, OpenRouter, Groq, DeepSeek, Mistral, xAI, Together,
Cerebras, or an Ollama or LM Studio on your own machine. Anything else that
speaks the protocol works too — type its address in.

A router such as litellm was measured and not taken: 183 MB across 114
packages against 21 MB, and the only thing it reaches that this does not is a
provider with no OpenAI endpoint at all (Bedrock, Vertex, SageMaker).

`TEXTCAST_SUMMARY_MODEL`, `TEXTCAST_SUMMARY_BASE_URL` and
`TEXTCAST_SUMMARY_API_KEY` (or `GEMINI_API_KEY`) supply the defaults; what you
save on the page wins over them, so the settings always take effect.

Summarising queues a rebuild, because a new block moves every paragraph after
it. That is also why it is its own job: the model runs once, the audio follows.

## The voice

Kokoro-82M reads everything. Measured here on 4 ARM cores with no GPU, against
a real 659-character paragraph:

| Engine | RTF | Speed | Rate | Install |
| --- | --- | --- | --- | --- |
| **Kokoro-82M** (`af_heart`) | 0.65 | 1.5× real time | 24 kHz | 113 packages, 4.9 GB |

A faster ONNX engine shipped alongside it for a while and was dropped. It was
about twice the speed, but its delivery was thinner: volume drifted and long
paragraphs glitched. Carrying two engines cost a registry, a build option, a
second set of weights and a second licence, and the second one was never the
one worth listening to.

`textcast voices` lists the 20 American voices the default pipeline loads;
8 British ones ship too. Set the default with `TEXTCAST_TTS_VOICE`. Voice,
quote voice, reading pace and footnote handling are then set **per article** —
when you add it, or on its page afterwards. Nothing is inherited from a folder
or a feed.

**Quote voice.** A block quote needs a mark or it runs into the sentence before
it. Left blank, the reader says "Start quote" and "End quote" aloud — the
*spoken cues*. Give quotes a second voice and the change of voice does that job
instead, with no extra words.

**Reading pace** is baked into the audio, so changing it rebuilds. The player's
speed control is a separate thing and changes nothing on disk.

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
tailnet in front, or nothing at all on a private LAN. `TEXTCAST_HOST` and
`TEXTCAST_PORT` control where it listens.

Access control is off by default, which suits a private network. For anything
internet-facing set `TEXTCAST_REQUIRE_AUTH=1` and `TEXTCAST_AUTH_TOKEN`. A
browser is then sent to `/login`, which takes the token and keeps it in a
cookie; scripts send it as an `x-textcast-token` header instead.

## Data model

The **block** is the unit — a paragraph, quote, list item, footnote or
generated summary is one row with a stable id. Reading, listening, highlighting, seeking and
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
├── summarize.py    section summaries from any OpenAI-compatible endpoint
├── ingest/         adapters + the shared DOM walker
│   ├── dom.py      selectolax helpers
│   ├── extract.py  content-density fallback
│   └── documents.py text, Markdown, PDF and DOCX readers
├── tts/            engine registry and kokoro.py
├── audio.py        synthesis, Opus encoding, WebVTT
├── db.py           SQLite, search, tags, jobs, positions
├── migrate.py      schema migrations, run on every start
├── jobs.py         the build worker
├── service.py      ingestion shared by the CLI and the web app
├── mail.py         IMAP newsletter fetch
└── web/            FastAPI, templates, one stylesheet, the player
```

## Licence

MIT. Kokoro's weights are Apache-2.0.
Bundles [media-chrome](https://github.com/muxinc/media-chrome) (MIT).
