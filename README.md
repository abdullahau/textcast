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
2. **Parse it properly.** Per-publication adapters — Bloomberg, the FT, the
   Economist, Substack — with a content-density fallback for everything else.
   Footnotes are inlined into the sentence that cites them.
3. **Teach it how to say things.** Money, percentages and dates are rewritten
   for the ear. Acronyms are respelled — `GAAP` as `gap` — and where no
   spelling reaches a word, IPA phonemes are handed to the model directly.
   Every rule is editable in the app, testable against your own voice, and
   exportable.
4. **Speak it.** Optionally summarise each section first, then Kokoro reads it
   a block at a time, encoded to Opus.
5. **Keep the pictures.** Every chart, table and photograph an article cites is
   fetched once and stored beside the audio, so an article kept offline is
   still the article. A picture is a block like any other, in its place in the
   read-along, and the player can stop at one so you can look at it.
6. **Read along.** The player highlights the current paragraph and follows it
   down the page. The text stays selectable, so you can still copy from it.
7. **Take it with you.** Install to the home screen, keep articles offline,
   control it from the lock screen.

## Install it on a fresh machine

Docker and the compose plugin are the only things you need first. Five steps,
start to finish:

```bash
# 1. Docker, if the machine has none (Debian or Ubuntu)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker

# 2. The code
git clone https://github.com/abdullahau/textcast.git && cd textcast

# 3. The settings. Set TEXTCAST_UID and TEXTCAST_GID or SQLite will refuse
#    to write: the image's user is 10001 and yours is not.
cp .env.example .env
printf 'TEXTCAST_UID=%s\nTEXTCAST_GID=%s\n' "$(id -u)" "$(id -g)" >> .env

# 4. Build and start. The first build takes about seven minutes and 3.3 GB:
#    it bakes the Kokoro weights in, so a fresh container needs no network.
docker compose -f docker-compose.yml up -d --build

# 5. Check it
curl -s localhost:8000/health   # {"ok":true,...}
docker compose logs -f worker
```

Open <http://127.0.0.1:8000> and add something. Nothing else is installed on
the host — espeak-ng, ffmpeg and the model are all in the image.

**Anything reachable from the internet needs three more lines in `.env`**, set
before the first start, because they seed the account and are then never read
again:

```ini
TEXTCAST_REQUIRE_AUTH=1
TEXTCAST_USERNAME=you
TEXTCAST_AUTH_TOKEN=a-long-password-you-will-change-in-the-app
TEXTCAST_PUBLIC_URL=https://textcast.example.com   # what the bookmarklet posts to
```

Put a reverse proxy or a tailnet in front for TLS; textcast takes no view on
which. To update: `git pull && docker compose -f docker-compose.yml up -d
--build`. `./data` is a bind mount, so it survives, and it is the whole backup.

## How it runs

Two containers off one image: `app` serves the pages, `worker` does the
synthesis (CPU-limited, so it cannot starve the web process). Both share
`./data` as a **bind mount**, so the database, the audio, the saved originals
and the render cache stay on the host where your backups already point.

Both engines' weights are baked into the image, which is what the first build
spends its 3.3 GB and seven minutes on. A fresh container then needs no
network at all — and no espeak-ng or ffmpeg on the host either.

There is no command line. Adding an article, choosing a voice, summarising and
building are all done in the app.

### Working on it

`docker-compose.override.yml` is picked up automatically and mounts `./src`
over the image's copy, so an edit on the host *is* the code the container
runs — uvicorn reloads the web process, watchfiles restarts the worker, and
templates and stylesheets are read per request. Nothing needs a rebuild except
a dependency change.

On a server, leave the override out:

```bash
docker compose -f docker-compose.yml up -d
```

With an NVIDIA GPU, add the GPU overlay. It builds the same image from the
CUDA wheels and passes the device to the worker:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The device cannot be a setting in `.env`: torch and onnxruntime each ship a
different distribution per device, so it is chosen when the image is built.
Both engines then ask the machine themselves, so the GPU image still runs
where there is no device.

One trap: the service worker caches `/static/` hard, so a CSS or JS edit can
be invisible in the browser while live on disk. Hard-reload to see it.

### Without Docker

```bash
uv sync --extra cpu --extra kokoro --extra kokoro-onnx \
        --extra web --extra documents --extra summaries
uv run uvicorn textcast.web.app:app --port 8000   # the app, worker included
uv run python -m textcast                         # or the worker on its own
```

`--extra cpu` is not optional. It is what picks the CPU wheels of torch and
onnxruntime; without it torch comes from PyPI, which means the CUDA build and
about 4 GB of NVIDIA runtime. Swap it for `--extra cuda` on a machine with an
NVIDIA GPU. The two conflict, so exactly one.

Kokoro needs **espeak-ng** on the system (`brew install espeak-ng`, or
`apt install espeak-ng espeak-ng-data`); textcast finds it in the usual places.

## Sharing to it from a phone

Android reads the web app manifest, so installing textcast to the home screen
puts it in the share sheet. iOS has no Web Share Target, so it needs a Shortcut.
Build it once:

1. Open Shortcuts, make a new shortcut, and add **Get Contents of URL**.
2. Set the URL to `https://your-host/api/ingest`.
3. Set Method to **POST** and Request Body to **Form**.
4. Add a field `kind` with the value `url`.
5. Add a field `url` and set its value to **Shortcut Input**.
6. If access control is on, add a header `x-textcast-token` holding the
   **ingest key** from the Settings page — not your password.
7. In the shortcut details, turn on **Show in Share Sheet** and accept URLs.

The same steps are on the Add page, with your host filled in. Anything shared
this way lands in the library parsed and unbuilt, like anything else.

## Tags

Tags are the only way things are grouped. There is no separate notion of a
newsletter or a folder: a detected newsletter simply becomes a tag, alongside
any you make yourself. Add them while adding an article, or on the article page,
and filter the library by one from the dropdown.

## Newsletters by mailbox

Point it at a dedicated address or a filtered folder and every unread issue
becomes an article, tagged with its publication. IMAP only: the app connects
outwards, so there is no mail server to run and no spam surface. It checks the
`List-Id` headers first, so personal mail stays unread.

Set the mailbox in `.env` and give it a poll interval; the worker does the
rest, on its own schedule, in the same container that builds the audio.

```ini
TEXTCAST_IMAP_HOST=imap.fastmail.com
TEXTCAST_IMAP_USER=you@example.com
TEXTCAST_IMAP_PASSWORD=an-app-password
TEXTCAST_MAIL_POLL_MINUTES=30
```

## Teaching it how to say things

This is the part that makes a local model listenable, and it is all editable
in the app, on the **Voice** page.

Anything with a *shape* is rewritten on the way to the engine and never on
screen, so the page keeps the author's punctuation:

| Written | Spoken |
| --- | --- |
| `$72mm`, `£5bn`, `€300k` | 72 million dollars, 5 billion pounds, 300 thousand euros |
| `150bps`, `12x`, `2.5%` | 150 basis points, 12 times, 2.5 percent |
| `Q3`, `FY2024`, `2019-21` | quarter 3, fiscal year 2024, 2019 to 2021 |

Anything that is a *lookup* is a rule you can edit, add to or turn off. Three
kinds, in the order you should reach for them:

- **Respell it.** `GAAP` → `gap`, `EBITDA` → `ee bitda`. Ordinary letters, no
  phonetic alphabet, and anyone can see what it does. This is the right answer
  almost every time.
- **Join it up.** `start-up` → `startup`. Kokoro breaks on the hyphen —
  measured at 182 ms of silence mid-phrase against 113 ms joined.
- **Give it phonemes.** When no spelling reaches it, the replacement can be
  **IPA**: `LIBOR` → `lˈIbɔɹ`, handed to the model verbatim. Of the hundred-odd
  rules that ship, exactly one needs this — which is the point. Reach for it
  last, and only when you can hear that respelling has failed.

Type a sentence into **Test the voice** and you get the text the engine will
actually be given, the phonemes it will pronounce, which rules fired, and the
audio itself, in whichever voice and pace you pick. Hearing it is the only way
to judge a respelling.

Change a rule and the page names the built articles that use the word and
rebuilds them on one click. The whole rule set exports to a JSON file and
imports back, so it survives a rebuild and moves between machines.

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

API keys are typed on the Summaries page, one per endpoint, and kept in the
database on this machine. There is no key variable: one standing behind every
provider meant the page could not say whose key was in use.
`TEXTCAST_SUMMARY_MODEL` and `TEXTCAST_SUMMARY_BASE_URL` supply starting
values, and what you save on the page wins over them.

A provider running on this machine — Ollama, LM Studio — needs no key at all.

Summarising queues **nothing**. A summary is a block, and inserting one moves
every id after it, so the audio has to be made after the text is final — and
when that happens is yours to say. That is why it is its own job kind: it
writes the blocks and stops.

## The voice

Kokoro-82M reads everything, through either of two runtimes. They are the
*same weights* and were judged indistinguishable by ear, so the choice is made
on everything else. Measured here on 4 ARM cores with no GPU, over a whole
build pool:

| Engine | RTF | Resident | Install |
| --- | --- | --- | --- |
| **kokoro-onnx** (default) | 0.456 | 530 MB | ~40 MB of wheels |
| kokoro (torch) | 0.557 | 1,512 MB | torch, ~1.4 GB |

ONNX is the default: cheaper by every measure that is not the sound. Both are
built into the image, and the engine is a per-article choice on the article
page, so switching back and forth costs nothing after the first build of each.
`TEXTCAST_TTS_ENGINE` moves the default.

A third engine, Supertonic, was measured and refused: about twice the speed,
but volume drifted and long paragraphs glitched. Speed does not earn an engine
its place.

20 American voices ship, and 8 British. Set the default on the **Voice** page
or with `TEXTCAST_TTS_VOICE`. Voice, quote voice, reading pace and footnote
handling are then set **per article** — on its page, after you have seen what
the parser made of it. Nothing is inherited from a folder or a feed.

**Quote voice.** A block quote needs a mark or it runs into the sentence before
it. Left blank, the reader says "Start quote" and "End quote" aloud — the
*spoken cues*. Give quotes a second voice and the change of voice does that job
instead, with no extra words.

**Reading pace** is baked into the audio, so changing it rebuilds. The player's
speed control is a separate thing and changes nothing on disk.

## The player

The two hard parts are not hand-written:

- **The timings come from the build, not from a guess.** One list produces the
  block rows, the WebVTT track and the JSON the page carries, so they cannot
  disagree. The highlight reads the audio clock every frame and finds the
  block with one bisect — 2 ms and the same in every browser. The WebVTT track
  stays as the backstop for a background tab, where frames stop and audio does
  not.
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

## Reaching it

textcast takes no view on how you get to it. Put a reverse proxy, a VPN or a
tailnet in front, or nothing at all on a private LAN. `TEXTCAST_HOST` and
`TEXTCAST_PORT` control where it listens.

Access control is off by default, which suits a private network. For anything
internet-facing set `TEXTCAST_REQUIRE_AUTH=1`, plus `TEXTCAST_USERNAME` and
`TEXTCAST_AUTH_TOKEN`. Those two seed the account row **once**, on an empty
database, and are never read again: after that the username, the password and
the profile picture live in the app, on the Settings page. Editing `.env` later
does nothing, which is worth knowing before you try it.

A browser is sent to `/login` and carries a **session** in a cookie — never the
password, so changing the password signs every other browser out. The
bookmarklet and the iPhone Shortcut carry a second secret, the **ingest key**,
which opens `POST /api/ingest` and nothing else: one lifted out of a bookmarks
bar can add an article and cannot delete one. Rotate it on the Settings page.

## Data model

The **block** is the unit — a paragraph, quote, list item, footnote, picture,
table or generated summary is one row with a stable id. Reading, listening,
highlighting, seeking and search all read the same table, so they cannot drift
apart. SQLite in WAL mode, with an FTS5 index over block text.

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
uv run pytest
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
├── pronounce.py    the editable word rules: respellings and IPA
├── pictures.py     fetches every picture an article cites, and sweeps them
├── accounts.py     the one account, the session and the ingest key
├── tts/            engine registry and the two Kokoro runtimes
├── audio.py        synthesis, Opus encoding, WebVTT
├── cache.py        what the block cache holds and may forget
├── db.py           SQLite, search, tags, jobs, positions
├── migrate.py      runs on every start; seeding and column adds
├── jobs.py         the build worker, and `python -m textcast`
├── prefs.py        default engine, voice, quote voice and reading pace
├── service.py      ingestion, deletion, re-parse, summary queueing
├── mail.py         IMAP newsletter fetch
└── web/            FastAPI, templates, one stylesheet, the player
```

## Licence

MIT. Kokoro's weights are Apache-2.0.
Bundles [media-chrome](https://github.com/muxinc/media-chrome) (MIT).
