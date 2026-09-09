"""The read-along player, driven in a real browser.

Sync correctness cannot be asserted from Python: it depends on the browser's
WebVTT "time marches on" algorithm firing `cuechange` against a real decoded
audio file. So this starts the app, builds an article with a stub engine, and
drives Chromium.

Skipped unless playwright and its Chromium build are present:

    uv pip install playwright && playwright install chromium
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import pytest
from conftest import free_port

from textcast import db
from textcast.audio import render_article
from textcast.document import Article, Block, BlockKind, Section
from textcast.tts.base import Clip, Voice

pytest.importorskip("playwright", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class ToneEngine:
    """Audible, decodable audio without loading a real TTS model."""

    name = "tone"
    sample_rate = 24000

    def voices(self):
        return [Voice(id="t1", name="Tone")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        seconds = max(1.0, len(text) / 15.0)
        n = int(seconds * self.sample_rate)
        t = np.linspace(0, seconds, n, endpoint=False, dtype=np.float32)
        return Clip(samples=(0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32),
                    sample_rate=self.sample_rate)


def sample_article() -> Article:
    def para(n):
        return Block(kind=BlockKind.PARA, text=f"Paragraph number {n}. " + "Filler words here. " * n)

    return Article(
        title="A Drug-Trial Stock Sale",
        subtitle="INmune, Linqto and the AI pay wars.",
        source="Bloomberg",
        series="Money Stuff",
        sections=[
            Section(title="INMB", blocks=[para(1), para(3), para(5),
                                          Block(kind=BlockKind.FOOTNOTE, text="Footnote 1. A note.")]),
            # The table goes last, in the second section, so every block id
            # the other tests name stays where it was.
            Section(title="Linqto", blocks=[para(2), para(4), Block(
                kind=BlockKind.TABLE,
                text="Table: Two by two",
                media={"rows": [["A", "B"], ["1", "2"]], "header": True},
            )]),
        ],
    ).renumber()


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A running app with one fully built article."""
    data = tmp_path_factory.mktemp("data")
    import os

    os.environ["TEXTCAST_DATA_DIR"] = str(data)
    os.environ["TEXTCAST_WORKERS"] = "0"

    from textcast.settings import get_settings

    settings = get_settings(refresh=True)
    settings.ensure_dirs()
    db.close()
    conn = db.init(settings.db_path)

    article = sample_article()
    article_id = db.save_article(article, conn)
    row = db.get_article(article_id, conn)

    manifest = render_article(
        article, ToneEngine(), settings.media_dir / row["slug"], voice="t1", gap_ms=200
    )
    db.save_manifest(article_id, manifest, audio_bytes=1, conn=conn)
    db.close()

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "textcast.web.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import urllib.request

            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("the app did not start")

    yield base, row["slug"], manifest
    proc.terminate()
    proc.wait(timeout=10)


class EvenlySpacedAligner:
    """Stands in for the real ONNX aligner: one word per target word, spaced
    evenly across the clip. ToneEngine's audio is a sine wave, not speech,
    so there is nothing for a real aligner to find here -- what this fixture
    tests is the browser's word-level highlight loop, not alignment
    quality, which test_aligner.py and test_sourcemap.py already cover
    against real audio and the real corpus."""

    def align(self, waveform, sample_rate, text):
        from textcast.tts.aligner import AlignedWord, to_target_text

        target_words = [w for w in to_target_text(text).split("|") if w]
        if not target_words:
            return []
        dur_ms = round(len(waveform) / sample_rate * 1000)
        step = dur_ms / len(target_words)
        return [
            AlignedWord(text=w, start_ms=round(i * step), end_ms=round((i + 1) * step))
            for i, w in enumerate(target_words)
        ]


@pytest.fixture(scope="module")
def live_words(tmp_path_factory):
    """A running app with one article built *and* word-aligned.

    Its own database, its own port: the module-scoped `live` fixture is
    shared by tests that must not see word_highlight data appear underneath
    them mid-module.
    """
    from textcast.audio import align_article

    data = tmp_path_factory.mktemp("data_words")
    import os

    os.environ["TEXTCAST_DATA_DIR"] = str(data)
    os.environ["TEXTCAST_WORKERS"] = "0"

    from textcast.settings import get_settings

    settings = get_settings(refresh=True)
    settings.ensure_dirs()
    db.close()
    conn = db.init(settings.db_path)

    article = sample_article()
    article_id = db.save_article(article, conn)
    row = db.get_article(article_id, conn)

    manifest = render_article(
        article, ToneEngine(), settings.media_dir / row["slug"], voice="t1", gap_ms=200,
        cache_dir=settings.cache_dir,
    )
    db.save_manifest(article_id, manifest, audio_bytes=1, conn=conn)
    align_article(
        article, manifest, EvenlySpacedAligner(), voice="t1", cache_dir=settings.cache_dir,
    )
    db.save_word_timings(article_id, manifest, conn)
    db.close()

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "textcast.web.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import urllib.request

            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("the app did not start")

    yield base, row["slug"], manifest
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    """One browser for the module.

    Sync Playwright allows a single running instance per thread, so every test
    that needs a page takes a context from this one rather than starting its
    own.
    """
    try:
        pw = sync_playwright().start()
        launched = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    yield launched
    launched.close()
    pw.stop()


@pytest.fixture(scope="module")
def page(live, browser):
    base, slug, _ = live
    p = browser.new_page()
    p.errors = []
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    p.goto(f"{base}/a/{slug}", wait_until="networkidle")
    p.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )
    yield p
    p.close()


@pytest.fixture
def quiet(page):
    """Silence the module-scoped reader for a test about the saved position.

    It is a second reader of the same article and it saves on a timer, so its
    `finished: false` landed between a test writing the row and the page under
    test loading it — after which that page carried `finished: false` for the
    rest of its life and no amount of waiting recovered it. Pausing stops the
    timer; the one save the pause itself fires is waited for here rather than
    in the middle of the test.
    """
    page.evaluate("document.getElementById('audio').pause()")
    time.sleep(0.6)
    return page


@pytest.fixture
def still_page(live, browser):
    """A reader of its own, for tests that assert nothing moves.

    The module-scoped page is a live player: it can still be playing from an
    earlier test, and it rolls on to the next section by itself when a section
    ends. A test about staying put cannot share that.
    """
    base, slug, _ = live
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a && a.readyState >= 1 && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )
    yield page
    context.close()


@pytest.fixture
def still_page_words(live_words, browser):
    """The word-highlighted article's own still page — see `still_page`."""
    base, slug, _ = live_words
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a && a.readyState >= 1 && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )
    yield page
    context.close()


def seek_to(page, seconds: float) -> None:
    """Put the clock somewhere and wait for it to have gone there.

    `loadSection` positions the audio from `loadedmetadata`, which fires
    after the track's cues have loaded -- so a test that waits only for the
    cues can set `currentTime` and have the player set it straight back to
    the section start. It is only ever visible on a loaded box, where the
    metadata takes longer to arrive than the track does: measured at two
    failures in six runs with six spinning processes beside them, and none
    without.
    """
    page.evaluate("(want) => { document.getElementById('audio').currentTime = want; }", seconds)
    page.wait_for_function(
        "(want) => Math.abs(document.getElementById('audio').currentTime - want) < 0.05",
        arg=seconds,
        timeout=10000,
    )


def test_word_level_highlight_sits_on_top_of_the_block_highlight(still_page_words, live_words):
    """Word-level lights one <span data-w="N"> inside the already-highlighted
    block, on top of it -- never in place of it, and never before the block
    itself is the active one."""
    _base, _slug, manifest = live_words
    target = manifest.sections[0].blocks[2]
    word = target.words[1]
    at = (word.start_ms + word.dur_ms / 2) / 1000

    seek_to(still_page_words, at)
    still_page_words.wait_for_function(
        "() => { const el = document.querySelector('.w.on');"
        " return el && el.dataset.w === '1'; }",
        timeout=10000,
    )

    assert still_page_words.evaluate("(document.querySelector('#doc .b.on') || {}).id") == target.id
    on_words = still_page_words.evaluate(
        "Array.from(document.querySelectorAll('.w.on')).map(el => el.dataset.w)"
    )
    assert on_words == ["1"], f"expected only word 1 lit, got {on_words}"
    # It is inside the highlighted block, not merely on the page somewhere.
    assert still_page_words.evaluate(
        f"!!document.querySelector('#{target.id} .w.on')"
    )


def test_word_level_highlight_moves_forward_with_the_clock(still_page_words, live_words):
    _base, _slug, manifest = live_words
    target = manifest.sections[0].blocks[2]
    assert len(target.words) >= 3, "fixture block needs at least three words for this test"
    third = target.words[2]
    at = (third.start_ms + third.dur_ms / 2) / 1000

    seek_to(still_page_words, at)
    still_page_words.wait_for_function(
        "() => { const el = document.querySelector('.w.on');"
        " return el && el.dataset.w === '2'; }",
        timeout=10000,
    )


def test_word_level_highlight_clears_when_the_block_changes(still_page_words, live_words):
    """Leaving a block for one with no word timings (or a different one)
    must not leave a stale word lit behind."""
    _base, _slug, manifest = live_words
    first_block = manifest.sections[0].blocks[2]
    next_block = manifest.sections[0].blocks[3]  # the footnote, right after it
    word = first_block.words[0]

    seek_to(still_page_words, (word.start_ms + word.dur_ms / 2) / 1000)
    still_page_words.wait_for_function(
        "() => document.querySelector('.w.on') !== null", timeout=10000
    )

    seek_to(still_page_words, next_block.start_ms / 1000 + 0.05)
    still_page_words.wait_for_function(
        f"() => (document.querySelector('#doc .b.on') || {{}}).id === '{next_block.id}'",
        timeout=10000,
    )
    stale = still_page_words.evaluate(
        f"!!document.querySelector('#{first_block.id} .w.on')"
    )
    assert not stale, "a word from the block just left is still lit"


def active_id(page):
    return page.evaluate("(document.querySelector('#doc .b.on') || {}).id || null")


def first_audio_url(page, slug):
    """The address the player really uses, `?b=<built_at>` and all.

    The cache is keyed on the whole URL, so a test that asks for the bare path
    is asking for something that was never stored.
    """
    name = page.evaluate(
        "() => JSON.parse(document.getElementById('payload').textContent).sections[0].file"
    )
    return f"/media/{slug}/{name}"


def test_media_chrome_upgrades(page):
    assert page.evaluate("!!customElements.get('media-play-button')")
    assert page.locator("#player").is_visible()


def test_one_vtt_cue_per_block(page, live):
    _base, _slug, manifest = live
    cues = page.evaluate("document.getElementById('audio').textTracks[0].cues.length")
    assert cues == len(manifest.sections[0].blocks)
    assert page.evaluate("document.getElementById('audio').textTracks[0].cues[0].id") == "b0-0"


def test_highlight_follows_the_audio(page, live):
    """The browser activates the cue; we only react to it."""
    _base, _slug, manifest = live
    third = manifest.sections[0].blocks[2]
    at = (third.start_ms + third.dur_ms / 2) / 1000

    seek_to(page, at)
    # Wait for the expected id, not merely for "something is highlighted":
    # the first block is already highlighted at load, so a loose check races.
    page.wait_for_function(
        f"() => {{ const el = document.querySelector('#doc .b.on');"
        f" return el && el.id === '{third.id}'; }}",
        timeout=10000,
    )
    assert active_id(page) == third.id


def test_highlight_moves_during_playback(page, live):
    _base, _slug, manifest = live
    first, second = manifest.sections[0].blocks[0], manifest.sections[0].blocks[1]

    seek_to(page, first.start_ms / 1000)
    page.wait_for_function(
        f"() => {{ const el = document.querySelector('#doc .b.on');"
        f" return el && el.id === '{first.id}'; }}",
        timeout=10000,
    )
    page.evaluate("document.getElementById('audio').play()")
    page.wait_for_function(
        f"() => {{ const el = document.querySelector('#doc .b.on');"
        f" return el && el.id === '{second.id}'; }}",
        timeout=20000,
    )
    page.evaluate("document.getElementById('audio').pause()")


def test_the_gutter_handle_seeks_to_its_paragraph(page, live):
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[2]
    want = target.start_ms / 1000

    page.evaluate("document.getElementById('audio').pause()")
    page.locator(f'[data-seek="{target.id}"]').click(force=True)
    page.wait_for_function(
        f"() => Math.abs(document.getElementById('audio').currentTime - {want}) < 1.0",
        timeout=10000,
    )
    assert abs(page.evaluate("document.getElementById('audio').currentTime") - want) < 1.0


def test_selecting_text_does_not_seek(still_page, live):
    """Selecting a paragraph used to start playback, which made copying impossible."""
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[1]

    still_page.evaluate("document.getElementById('audio').pause()")
    still_page.evaluate("document.getElementById('audio').currentTime = 0")
    before = still_page.evaluate("document.getElementById('audio').currentTime")

    selected = still_page.evaluate(
        """(id) => {
            const el = document.getElementById(id);
            const range = document.createRange();
            range.selectNodeContents(el);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            el.dispatchEvent(new MouseEvent("click", {bubbles: true}));
            return sel.toString().length;
        }""",
        target.id,
    )

    assert selected > 10, "the paragraph text is selectable"
    assert still_page.evaluate("document.getElementById('audio').currentTime") == before
    assert still_page.evaluate("document.getElementById('audio').paused")


def test_blocks_are_never_buttons(page):
    """A <button> wrapper is what broke selection in the first place.

    Prose is a <p> and a visual is a <figure>, which cannot swallow a click on
    the text because there is no text in it to select. What must never come
    back is the block that is itself a control.
    """
    tags = page.evaluate(
        "Array.from(document.querySelectorAll('#doc .b')).map(e => e.tagName)"
    )
    assert set(tags) <= {"P", "FIGURE"}
    assert "P" in tags


def test_moving_to_the_next_section_loads_its_own_track(page):
    # The next button carries playback across only if it was already playing,
    # and the highlight below needs cuechange, which needs the clock running.
    # The page fixture is shared, so say so rather than inherit it by luck.
    page.evaluate("document.getElementById('audio').play()")
    page.wait_for_function("() => !document.getElementById('audio').paused", timeout=8000)

    page.evaluate("document.getElementById('next').click()")
    # Loading a section fetches and decodes a second audio file and its track,
    # so this is the slowest step in the suite on a loaded machine.
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a.textTracks.length && a.textTracks[a.textTracks.length - 1].cues"
        " && a.textTracks[a.textTracks.length - 1].cues.length"
        " && a.textTracks[a.textTracks.length - 1].cues[0].id.startsWith('b1-'); }",
        timeout=60000,
    )
    page.wait_for_function(
        "() => { const el = document.querySelector('#doc .b.on');"
        " return el && el.id.startsWith('b1-'); }",
        timeout=60000,
    )
    assert active_id(page).startswith("b1-")


def test_media_session_is_populated(page):
    title = page.evaluate(
        "navigator.mediaSession && navigator.mediaSession.metadata"
        " ? navigator.mediaSession.metadata.title : null"
    )
    assert title == "A Drug-Trial Stock Sale"


def test_chapters_and_toggles(page, live):
    _base, _slug, manifest = live
    assert page.locator("#chapters .chapter").count() == len(manifest.sections)

    page.evaluate("document.getElementById('opt-footnotes').click()")
    assert page.evaluate("document.body.classList.contains('hide-footnotes')")
    assert not page.locator("#doc .b.footnote").first.is_visible()

    page.evaluate("document.getElementById('opt-footnotes').click()")
    assert page.locator("#doc .b.footnote").first.is_visible()


def test_position_is_saved_to_the_server(page):
    """Leaving the page writes the position, so another device resumes there."""
    page.evaluate("document.getElementById('audio').pause()")
    page.evaluate("document.getElementById('audio').currentTime = 3")

    # sendBeacon fires on pagehide; visibilitychange is the same path.
    page.evaluate(
        "Object.defineProperty(document, 'hidden', {value: true, configurable: true});"
        "document.dispatchEvent(new Event('visibilitychange'))"
    )

    # Which section the player actually has loaded, from the highlighted block.
    expected = int(page.evaluate(
        "(document.querySelector('#doc .b.on') || {dataset:{s:'0'}}).dataset.s"
    ))

    conn = db.init()
    for _ in range(60):
        saved = db.get_position(1, conn)
        # An earlier test may have left a row, so wait for one that agrees
        # with where the player is now rather than for any row at all.
        if saved and saved["ms"] > 0 and saved["section_idx"] == expected:
            break
        time.sleep(0.25)
    else:
        raise AssertionError(
            f"no position written for section {expected}; last saw "
            f"{dict(saved) if saved else None}"
        )

    assert saved["ms"] > 0


def test_no_javascript_errors(page):
    assert [e for e in page.errors if "favicon" not in e.lower()] == []


def test_threading_is_not_required():
    """Guard against the worker being started by the test app."""
    assert threading.active_count() >= 1


# --------------------------------------------------------------------------
# offline
# --------------------------------------------------------------------------


def test_keeping_an_article_offline_survives_losing_the_network(live, browser):
    """The whole point of the service worker, and previously untested.

    A context of its own, because this registers a worker, fills a cache and
    then pulls the network out from under the page.
    """
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        # The worker claims its clients on activation, but not instantly.
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )

        page.click("#menu")
        page.check("#opt-offline")

        audio_url = first_audio_url(page, slug)
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url,
            timeout=20000,
        )

        # The page itself is cached in the same write as the audio, but ask for
        # it by name: it is what the reload below has to find.
        page.wait_for_function(
            "async (url) => !!(await caches.match(url))",
            arg=f"/a/{slug}",
            timeout=20000,
        )

        context.set_offline(True)
        page.reload(wait_until="domcontentloaded")

        assert page.locator("#doc .b").count() > 0, "the reader did not come back offline"
        cached = page.evaluate(
            "async (url) => (await fetch(url)).ok",
            audio_url,
        )
        assert cached, "the audio was not served from the cache"
    finally:
        context.set_offline(False)
        context.close()


def wait_until_uncached(page, url, timeout=10.0):
    """Poll from Python, not with `wait_for_function`.

    `wait_for_function` does not await a promise-returning predicate here —
    proven by handing it one that always resolves `false` after a delay and
    watching it return immediately anyway — so a predicate built on
    `caches.match` cannot be trusted to actually wait for anything. `evaluate`
    does await correctly; this just calls it in a loop.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not page.evaluate("async (u) => !!(await caches.match(u))", url):
            return
        time.sleep(0.2)
    raise AssertionError(f"{url} was never removed from the cache")


def test_reconciling_drops_an_articles_page_along_with_its_media(live, browser):
    """An article unticked in another tab, or removed from the library, is
    only caught by the next page load's `reconcile` message. It used to drop
    the marker and the media but leave the page cached for ever, with
    nothing left pointing at it."""
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )

        page.click("#menu")
        page.check("#opt-offline")

        audio_url = first_audio_url(page, slug)
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url,
            timeout=20000,
        )
        page.wait_for_function(
            "async (url) => !!(await caches.match(url))",
            arg=f"/a/{slug}",
            timeout=20000,
        )

        # Removed directly, not through the checkbox -- unticking it would
        # already correctly clean up via `drop-article`. This is what a
        # second tab doing the same, or the article being deleted from the
        # library, looks like from here: the marker and the files stay put
        # until the next page load reconciles them.
        page.evaluate("(slug) => localStorage.removeItem('tc:offline:' + slug)", slug)
        page.reload(wait_until="domcontentloaded")

        wait_until_uncached(page, f"/a/{slug}")
        wait_until_uncached(page, audio_url)
    finally:
        context.set_offline(False)
        context.close()


def test_cached_audio_still_answers_a_byte_range(live, browser):
    """An <audio> element asks for ranges, and the Cache API ignores them.

    The worker answered a request for 100 KB with the whole 2.4 MB file and
    status 200, so the element read the first byte it got as the byte it had
    asked for. On a phone that put every seek a few blocks late.
    """
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )
        page.click("#menu")
        page.check("#opt-offline")

        audio_url = first_audio_url(page, slug)
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url,
            timeout=20000,
        )

        got = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {headers: {Range: "bytes=100-199"}});
                return {status: r.status,
                        range: r.headers.get("content-range"),
                        bytes: (await r.arrayBuffer()).byteLength};
            }""",
            audio_url,
        )

        assert got["status"] == 206, f"the worker answered {got['status']}, not a partial"
        assert got["bytes"] == 100, f"asked for 100 bytes, got {got['bytes']}"
        assert got["range"].startswith("bytes 100-199/")
    finally:
        context.close()


def test_a_suffix_range_and_a_malformed_one_are_both_answered(live, browser):
    """The two odd shapes a Range header comes in.

    "bytes=-500" is the *last* 500 bytes, not the first. And a header the
    worker cannot parse falls back to handing over the whole file — which it
    had already read to measure it, so what came back was a Response nothing
    could read any more. The range is parsed before the body is touched now.
    """
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )
        page.click("#menu")
        page.check("#opt-offline")

        audio_url = first_audio_url(page, slug)
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url,
            timeout=20000,
        )

        got = page.evaluate(
            """async (url) => {
                const ask = async (value) => {
                    const r = await fetch(url, {headers: {Range: value}});
                    return {status: r.status, bytes: (await r.arrayBuffer()).byteLength};
                };
                return {suffix: await ask("bytes=-500"), junk: await ask("bytes=oops")};
            }""",
            audio_url,
        )

        assert got["suffix"]["status"] == 206
        assert got["suffix"]["bytes"] == 500, "the last 500 bytes, not the first"
        assert got["junk"]["bytes"] > 0, "a range it cannot parse still returns a readable body"
    finally:
        context.close()


def test_clicking_a_block_starts_at_that_block_and_nowhere_else(page, live):
    """Seeking while playing used to let the buffered tail of the old position out."""
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[3]

    page.evaluate("document.getElementById('audio').currentTime = 0")
    page.click(f"#{target.id} [data-seek]")
    page.wait_for_function(
        f"() => {{ const a = document.getElementById('audio');"
        f" return !a.seeking && Math.abs(a.currentTime * 1000 - {target.start_ms}) < 250; }}",
        timeout=8000,
    )

    at = page.evaluate("document.getElementById('audio').currentTime") * 1000
    assert abs(at - target.start_ms) < 250, f"landed at {at:.0f}ms, wanted {target.start_ms}ms"
    page.evaluate("document.getElementById('audio').pause()")


def test_a_stray_seek_does_not_start_playback(still_page, live):
    """`seeked` is asynchronous; an armed resume must not fire on someone else's seek."""
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[2]

    still_page.evaluate("document.getElementById('audio').pause()")
    seek_to(still_page, target.start_ms / 1000)
    still_page.evaluate("document.getElementById('audio').currentTime = 0")
    still_page.wait_for_timeout(300)

    assert still_page.evaluate("document.getElementById('audio').paused")


def test_seeking_to_a_block_highlights_that_block_not_the_one_before(still_page, live):
    """At a boundary the browser calls both cues active, ordered by start time.

    Taking activeCues[0] took the cue that was *ending*, so every click on a
    block's handle left the highlight one block behind while the right audio
    played.
    """
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks

    for target in blocks[1:5]:
        seek_to(still_page, target.start_ms / 1000)
        still_page.wait_for_timeout(250)
        got = still_page.evaluate("(document.querySelector('#doc .b.on') || {}).id || null")
        assert got == target.id, f"seeking to {target.id} highlighted {got}"


def test_the_first_block_is_highlighted_before_any_cue_boundary(live, browser):
    """cuechange only fires when the set changes, so a track that loads with a
    cue already active used to leave the first block unmarked until the next
    boundary — very visible when it is a long summary."""
    base, slug, manifest = live
    # An earlier test leaves a saved position, and resuming into the middle is
    # the right behaviour. This one is about opening at the top.
    conn = db.init()
    conn.execute("DELETE FROM position")

    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => { const a = document.getElementById('audio');"
            " return a && a.textTracks.length && a.textTracks[0].cues"
            " && a.textTracks[0].cues.length > 0; }",
            timeout=20000,
        )
        page.wait_for_function(
            "() => !!document.querySelector('#doc .b.on')", timeout=8000
        )
        assert page.evaluate("document.querySelector('#doc .b.on').id") == manifest.sections[0].blocks[0].id
    finally:
        context.close()


def test_the_sheet_can_be_closed_without_a_keyboard(still_page):
    """Escape is not a key a phone has. Without a close button or an outside
    tap, the sheet could only be left by reloading the page."""
    sheet = still_page.locator("#sheet")

    still_page.click("#menu")
    assert sheet.is_visible()
    still_page.click("#sheet-close")
    assert sheet.is_hidden(), "the close button"

    still_page.click("#menu")
    assert sheet.is_visible()
    still_page.click("#doc", position={"x": 5, "y": 5})
    assert sheet.is_hidden(), "a tap outside it"


def test_the_highlight_follows_the_clock_not_the_cue_events(still_page, live):
    """`cuechange` fires when the browser gets round to it, and a seek made by
    the transport — media-chrome owns the skip buttons and the scrub bar —
    changes no cue set at all. Reading the clock covers both."""
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks

    def expected(ms):
        found = None
        for timing in blocks:
            if timing.start_ms <= ms:
                found = timing.id
        return found

    audio = "document.getElementById('audio')"
    for js, label in (
        (f"{audio}.currentTime = {blocks[3].start_ms / 1000 + 0.4}", "a seek into a block"),
        (f"{audio}.currentTime += 1", "a nudge forward, as the skip button does"),
        (f"{audio}.currentTime = {blocks[1].start_ms / 1000 + 0.2}", "a scrub backwards"),
    ):
        still_page.evaluate(js)
        still_page.wait_for_timeout(200)
        at = still_page.evaluate("document.getElementById('audio').currentTime") * 1000
        got = still_page.evaluate("(document.querySelector('#doc .b.on') || {}).id || null")
        assert got == expected(at), f"{label}: at {at:.0f}ms wanted {expected(at)}, got {got}"


def test_the_highlight_needs_no_cues_at_all(still_page, live):
    """The timing map is in the page; the track is a convenience on top."""
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[2]

    still_page.evaluate("document.getElementById('audio').textTracks[0].mode = 'disabled'")
    seek_to(still_page, target.start_ms / 1000 + 0.3)
    still_page.wait_for_timeout(250)

    assert still_page.evaluate("(document.querySelector('#doc .b.on') || {}).id") == target.id
    still_page.evaluate("document.getElementById('audio').textTracks[0].mode = 'hidden'")


def test_playback_starts_where_it_was_asked_even_deep_into_the_file(still_page, live):
    """`seeked` means the playhead moved, not that anything is decoded there.
    Starting anyway runs the clock while no sound comes out — the first word
    or two of the block, gone, most visibly on a slow connection."""
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[-1]

    still_page.evaluate("""() => {
      const a = document.getElementById('audio');
      a.pause(); a.currentTime = 0;
      window.FIRST_SOUND = null;
      a.addEventListener('playing', () => {
        if (window.FIRST_SOUND === null) window.FIRST_SOUND = a.currentTime * 1000;
      });
    }""")
    still_page.click(f"#{target.id} [data-seek]")
    still_page.wait_for_function("() => window.FIRST_SOUND !== null", timeout=15000)

    began = still_page.evaluate("window.FIRST_SOUND")
    assert abs(began - target.start_ms) < 250, (
        f"sound began at {began:.0f}ms, {began - target.start_ms:+.0f}ms from the block"
    )
    assert still_page.evaluate("document.getElementById('audio').readyState") >= 3
    still_page.evaluate("document.getElementById('audio').pause()")


def test_stopping_returns_to_the_start_and_forgets_the_position(still_page, quiet):
    """The stop button in the sheet. Playback stops, the playhead goes back to
    the top of the article, and the saved position is deleted, so the article
    leaves "Continue listening"."""
    conn = db.init()
    db.save_position(1, section_idx=1, ms=9000, conn=conn)
    assert db.get_position(1, conn) is not None

    still_page.evaluate("document.getElementById('audio').play()")
    still_page.wait_for_function("() => !document.getElementById('audio').paused", timeout=10000)

    still_page.click("#menu")
    still_page.click("#stop")

    assert still_page.locator("#sheet").is_hidden(), "the sheet closes behind it"
    still_page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a.paused && a.currentTime < 1"
        " && a.currentSrc.includes('section-000.opus'); }",
        timeout=15000,
    )

    for _ in range(60):
        if db.get_position(1, conn) is None:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the position was not cleared")


def test_stopping_is_not_undone_by_the_save_that_follows_it(still_page, quiet):
    """Pausing writes the position, and so does hiding the page. Both had to
    learn to stay quiet, or the row came straight back and the article never
    left "Continue listening"."""
    conn = db.init()
    db.save_position(1, section_idx=1, ms=9000, conn=conn)

    still_page.click("#menu")
    still_page.click("#stop")
    for _ in range(60):
        if db.get_position(1, conn) is None:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the position was not cleared")

    # The same path the page uses when it is hidden or unloaded.
    still_page.evaluate(
        "Object.defineProperty(document, 'hidden', {value: true, configurable: true});"
        "document.dispatchEvent(new Event('visibilitychange'))"
    )
    time.sleep(1.5)

    assert db.get_position(1, conn) is None, "a save wrote the position back"


def test_opening_a_finished_article_does_not_un_finish_it(still_page, quiet):
    """Every save used to send finished = false, so opening a completed
    article and leaving without playing threw the completed badge away."""
    conn = db.init()
    db.save_position(1, section_idx=0, ms=4000, finished=True, conn=conn)
    still_page.reload(wait_until="domcontentloaded")

    still_page.evaluate(
        "Object.defineProperty(document, 'hidden', {value: true, configurable: true});"
        "document.dispatchEvent(new Event('visibilitychange'))"
    )

    # Polled, not slept. The module-scoped page is a second reader of the same
    # article and saves on its own timer, so a fixed wait sometimes read its
    # row rather than this one's.
    for _ in range(60):
        if db.get_position(1, conn)["finished"] == 1:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the save cleared the completed flag")


def _play_into_the_table(page, base, slug, manifest):
    """Start the second section playing, just short of the table at its end.

    Seeking *to* the table would not do: the player deliberately does not stop
    on a block you asked it to jump to. The stop is for reading past one.
    """
    page.goto(f"{base}/a/{slug}")
    page.wait_for_selector("#doc .b")
    table = manifest.sections[1].blocks[-1]
    before = manifest.sections[1].blocks[-2]

    page.evaluate(f'document.querySelector(\'[data-seek="{before.id}"]\').click()')
    page.wait_for_function("() => !document.getElementById('audio').paused", timeout=8000)
    page.evaluate(
        "ms => { document.getElementById('audio').currentTime = ms / 1000 - 0.3; }",
        table.start_ms,
    )
    return table


def test_the_player_stops_at_a_table_so_it_can_be_looked_at(live, browser):
    """The whole reason the parsers keep a chart: you can pause and look at it.

    A page of its own, because it pauses the audio and a test that inherited
    that state has been broken by it before.
    """
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}")
    page.wait_for_selector("#doc .b")
    # The toggle lives in the sheet, which is hidden until it is opened. The
    # setting is what is being tested, not the sheet's own machinery.
    page.evaluate(
        "() => { const box = document.getElementById('opt-pause-visual');"
        " box.checked = true; box.dispatchEvent(new Event('change')); }"
    )

    table = _play_into_the_table(page, base, slug, manifest)
    assert page.query_selector(f'#{table.id}[data-visual="1"]'), "the table is not marked"

    page.wait_for_function("() => document.getElementById('audio').paused", timeout=10000)
    assert page.evaluate("document.querySelector('#doc .b.on').id") == table.id

    # Pressing play again carries on rather than stopping on the same block
    # for ever.
    page.evaluate("document.getElementById('audio').play()")
    page.wait_for_timeout(700)
    assert page.evaluate("!document.getElementById('audio').paused")
    context.close()


def test_the_player_reads_through_a_table_unless_it_is_asked_not_to(live, browser):
    """Off by default: nothing stops for a reader who did not ask it to."""
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}")
    page.wait_for_selector("#doc .b")

    assert page.evaluate("document.getElementById('opt-pause-visual').checked") is False

    _play_into_the_table(page, base, slug, manifest)
    page.wait_for_timeout(1500)

    assert page.evaluate("!document.getElementById('audio').paused"), "it stopped anyway"
    context.close()


def test_the_pause_at_a_chart_setting_survives_a_reload(live, browser):
    """It was stored as the string "true" and read back against "1".

    `store(key, fallback, value)` takes the value third; this call passed the
    "1"/"0" as the fallback and `true` as the value. So the box was ticked,
    the setting was written, and the next page load compared "true" with "1"
    and drew it empty. The two other stores in the file had it right, which
    is why nothing else lost its setting.
    """
    base, slug, _manifest = live
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}")
    page.wait_for_selector("#doc .b")

    page.evaluate(
        "() => { const box = document.getElementById('opt-pause-visual');"
        " box.checked = true; box.dispatchEvent(new Event('change')); }"
    )
    assert page.evaluate("localStorage.getItem('tc:pause-visual')") == "1"

    page.reload()
    page.wait_for_selector("#doc .b")
    assert page.evaluate("document.getElementById('opt-pause-visual').checked") is True, (
        "the setting was saved and did not come back"
    )
    context.close()



def test_space_plays_and_pauses_wherever_the_focus_is(still_page):
    """media-chrome binds keys, but only while something inside the
    media-controller has focus. So Space did one of two wrong things: pressed
    the block play button that was last clicked, or scrolled the page."""
    audio = "document.getElementById('audio')"
    still_page.evaluate(f"{audio}.pause()")

    still_page.evaluate("document.body.focus()")
    still_page.keyboard.press("Space")
    still_page.wait_for_function(f"() => !{audio}.paused", timeout=5000)

    still_page.keyboard.press("Space")
    still_page.wait_for_function(f"() => {audio}.paused", timeout=5000)


def test_space_on_a_block_handle_pauses_instead_of_seeking_again(still_page, live):
    """The complaint, exactly: click a block's play button, then press Space,
    and it fired that button again rather than pausing."""
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[2]
    audio = "document.getElementById('audio')"

    still_page.click(f"#{target.id} [data-seek]")
    still_page.wait_for_function(f"() => !{audio}.paused", timeout=8000)
    # The handle gives focus up on the way, so nothing can re-fire it.
    assert still_page.evaluate("document.activeElement.matches('[data-seek]')") is False

    still_page.keyboard.press("Space")
    still_page.wait_for_function(f"() => {audio}.paused", timeout=5000)
    assert still_page.evaluate("(document.querySelector('#doc .b.on') || {}).id") == target.id


def test_arrow_keys_skip_five_seconds_wherever_the_focus_is(still_page):
    """Same shortcut as Space, for rewind and skip: five seconds, matching
    the lock screen's step, not media-chrome's own ten-second default."""
    audio = "document.getElementById('audio')"
    still_page.evaluate(f"{audio}.pause()")
    still_page.evaluate(f"{audio}.currentTime = 3")

    still_page.evaluate("document.body.focus()")
    still_page.keyboard.press("ArrowRight")
    still_page.wait_for_function(f"() => {audio}.currentTime > 7.5 && {audio}.currentTime < 8.5")

    still_page.keyboard.press("ArrowLeft")
    still_page.wait_for_function(f"() => {audio}.currentTime > 2.5 && {audio}.currentTime < 3.5")


def test_arrow_key_on_a_transport_button_does_not_double_skip(still_page):
    """media-chrome binds these same two keys on the controller, at its own
    ten-second step. Focus a button inside it — the play button gives focus
    up on click, like the block handles do, so it is given directly — and
    check the skip is still five seconds, not fifteen from both firing."""
    audio = "document.getElementById('audio')"
    still_page.evaluate(f"{audio}.pause()")
    still_page.evaluate(f"{audio}.currentTime = 3")

    still_page.evaluate("document.querySelector('media-play-button').focus()")
    still_page.keyboard.press("ArrowRight")
    still_page.wait_for_function(f"() => {audio}.currentTime > 7.5 && {audio}.currentTime < 8.5")


def test_space_still_types_and_still_ticks_a_box(still_page):
    """Stealing the key everywhere would break the search field and every
    checkbox in the sheet, which is what Space is for in both."""
    audio = "document.getElementById('audio')"
    still_page.evaluate(f"{audio}.pause()")

    still_page.click(".find input[name=q]")
    still_page.keyboard.type("a b")
    assert still_page.input_value(".find input[name=q]") == "a b"
    assert still_page.evaluate(f"{audio}.paused"), "typing a space started the audio"

    still_page.click("#menu")
    still_page.focus("#opt-footnotes")
    before = still_page.evaluate("document.getElementById('opt-footnotes').checked")
    still_page.keyboard.press("Space")
    assert still_page.evaluate("document.getElementById('opt-footnotes').checked") is not before
    assert still_page.evaluate(f"{audio}.paused"), "ticking a box started the audio"
    still_page.click("#sheet-close")


def test_locking_the_bar_stops_a_stray_tap_seeking(still_page):
    """Listening with the phone in a hand doing something else lands taps
    wherever a thumb falls, and the scrub bar is the width of the screen."""
    still_page.click("#lock")
    assert still_page.evaluate("document.getElementById('player').classList.contains('locked')")

    for selector in ("media-time-range", "#menu", "media-play-button"):
        assert still_page.evaluate(
            f"getComputedStyle(document.querySelector('{selector}')).pointerEvents"
        ) == "none", selector
    # The handles beside each block are the other thing a thumb finds.
    assert still_page.evaluate(
        "getComputedStyle(document.querySelector('#doc .b [data-seek]')).pointerEvents"
    ) == "none"

    # A tap does not undo it; that is the whole point.
    still_page.click("#lock")
    assert still_page.evaluate("document.getElementById('player').classList.contains('locked')")

    still_page.evaluate("document.getElementById('lock').dispatchEvent(new Event('pointerdown'))")
    still_page.wait_for_timeout(750)
    still_page.evaluate("document.getElementById('lock').dispatchEvent(new Event('pointerup'))")
    assert not still_page.evaluate(
        "document.getElementById('player').classList.contains('locked')"
    ), "a hold did not unlock it"


def to_first_section(page):
    """Put the page on section 0, paused.

    Every test above this one leaves a saved position on the server, and the
    reader resumes it — so a `currentTime` taken from `sections[0]` lands in
    whichever section happens to be loaded.
    """
    audio = "document.getElementById('audio')"
    page.evaluate(f"{audio}.pause()")
    page.click("#menu")
    page.locator("#chapters .chapter").first.click()
    page.wait_for_timeout(400)
    page.evaluate(f"{audio}.pause()")
    page.wait_for_timeout(200)
    page.evaluate(f"{audio}.pause()")


def test_the_highlight_timing_offset_holds_the_highlight_back(still_page, live):
    """`audio.currentTime` is where the decoder is, not where the speaker is.
    Over Bluetooth the difference is a fifth of a second or more, and no page
    can measure it — so the reader is given the number to turn."""
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks
    audio = "document.getElementById('audio')"

    to_first_section(still_page)
    # Just inside the third block, by less than the offset about to be set.
    still_page.evaluate(f"{audio}.currentTime = {blocks[2].start_ms / 1000 + 0.3}")
    still_page.wait_for_timeout(250)
    assert active_id(still_page) == blocks[2].id

    still_page.evaluate(
        "() => { const s = document.getElementById('opt-sync');"
        " s.value = '1000'; s.dispatchEvent(new Event('input')); }"
    )
    still_page.wait_for_timeout(250)
    assert active_id(still_page) == blocks[1].id, "the highlight did not move back"
    assert still_page.evaluate("localStorage.getItem('tc:sync-offset')") == "1000"

    still_page.evaluate(
        "() => { const s = document.getElementById('opt-sync');"
        " s.value = '0'; s.dispatchEvent(new Event('input')); }"
    )
    still_page.wait_for_timeout(250)
    assert active_id(still_page) == blocks[2].id


def test_following_leaves_the_page_alone_while_the_block_is_readable(still_page, live):
    """It used to call `scrollIntoView` on every block whatever the page was
    doing, so a thumb-scroll to look ahead was undone by the next paragraph,
    and the block was centred in a viewport whose top and bottom are covered
    by two fixed bars."""
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks
    audio = "document.getElementById('audio')"

    to_first_section(still_page)
    # Put the block comfortably on screen by hand, then hand it to the player.
    still_page.evaluate(f"document.getElementById('{blocks[1].id}')"
                        ".scrollIntoView({block: 'center'})")
    still_page.wait_for_timeout(300)

    where = still_page.evaluate("scrollY")
    still_page.evaluate(f"{audio}.currentTime = {blocks[1].start_ms / 1000 + 0.2}")
    still_page.wait_for_timeout(700)
    assert active_id(still_page) == blocks[1].id
    assert still_page.evaluate("scrollY") == where, "it scrolled to a block already on screen"


def test_a_block_below_the_player_is_scrolled_clear_of_it(still_page, live):
    """The bottom of the window is not the bottom of what you can read: the
    player sits over it. `block: "center"` knew nothing about that."""
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks
    audio = "document.getElementById('audio')"

    to_first_section(still_page)
    still_page.evaluate("scrollTo(0, 0)")
    still_page.wait_for_timeout(900)
    still_page.evaluate(f"{audio}.currentTime = {blocks[-1].start_ms / 1000 + 0.2}")
    still_page.wait_for_timeout(900)

    clear = still_page.evaluate(
        "() => { const el = document.querySelector('#doc .b.on');"
        " const bar = document.getElementById('player').getBoundingClientRect();"
        " const head = document.querySelector('header.bar').getBoundingClientRect();"
        " const box = el.getBoundingClientRect();"
        " return box.top >= head.bottom && box.top < bar.top; }"
    )
    assert clear, "the read block was under a bar"


def test_an_article_nobody_asked_to_keep_is_not_kept(live, browser):
    """`mediaResponse` used to store every 200 it saw.

    So the cache grew without limit and "Keep offline" meant nothing — the
    audio was there either way. Worse, the copy outlived the article:
    /media/<slug>/section-000.opus is rewritten by every build and the URL does
    not change, so a rebuilt article played its *old* audio against its *new*
    timing map, on whichever device happened to have a service worker.
    """
    base, slug, manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )

        audio_url = first_audio_url(page, slug)
        # Fetch it the way the audio element would, without ticking the box.
        assert page.evaluate("async (url) => (await fetch(url)).ok", audio_url)
        page.wait_for_timeout(600)

        assert not page.evaluate(
            "async (url) => !!(await caches.match(new Request(url)))", audio_url
        ), "an article nobody marked was stored anyway"

        # And ticking the box still keeps it, which is the other half.
        page.click("#menu")
        page.check("#opt-offline")
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url,
            timeout=20000,
        )
    finally:
        context.close()


def test_unticking_removes_every_file_it_stored(live, browser):
    """"Untick it and the space comes back" has to be true of all of it: the
    page, the audio, the timing map and the marker."""
    base, slug, _manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )
        page.click("#menu")
        page.check("#opt-offline")
        audio_url = first_audio_url(page, slug)
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url, timeout=20000,
        )

        page.uncheck("#opt-offline")
        page.wait_for_function(
            """async (slug) => {
                const cache = await caches.open("textcast-offline");
                const keys = await cache.keys();
                return keys.every(r => !r.url.includes("/media/" + slug + "/")
                                       && !r.url.includes("__offline__"));
            }""",
            arg=slug, timeout=20000,
        )
        assert page.evaluate("localStorage.getItem('tc:offline:' + arguments[0])"
                             .replace("arguments[0]", f"'{slug}'")) == "0"
    finally:
        context.close()


def test_a_page_load_collects_what_nothing_points_at_any_more(live, browser):
    """The boxes are the only record of what was asked for, so the worker is
    told them on every page. Without it the cache only ever grew: an article
    deleted from the library, or unticked in another tab, had no way to be
    heard about."""
    base, slug, _manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="networkidle")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=20000,
        )
        page.click("#menu")
        page.check("#opt-offline")
        audio_url = first_audio_url(page, slug)
        page.wait_for_function(
            "async (url) => !!(await caches.match(new Request(url)))",
            arg=audio_url, timeout=20000,
        )

        # The box goes without the worker being told — which is exactly the
        # shape of "deleted in another tab".
        page.evaluate("(slug) => localStorage.removeItem('tc:offline:' + slug)", slug)
        page.reload(wait_until="networkidle")

        page.wait_for_function(
            "async (url) => !(await caches.match(new Request(url)))",
            arg=audio_url, timeout=20000,
        )
    finally:
        context.close()


def test_the_play_button_is_round_and_not_an_oval(still_page):
    """media-chrome sizes a button from the glyph and the padding, so the
    height came from --media-control-height and the width from whatever the
    play mark happened to be."""
    box = still_page.evaluate(
        "() => { const r = document.querySelector('media-play-button')"
        ".getBoundingClientRect(); return {w: r.width, h: r.height}; }"
    )
    assert abs(box["w"] - box["h"]) < 1.5, f"{box['w']}x{box['h']} is not a circle"


def test_the_hold_to_unlock_says_how_long_to_hold(still_page):
    """Holding for an unmarked length of time is a guess, and a guess that has
    to be repeated is worse than a second tap."""
    still_page.click("#lock")
    hint = still_page.locator("#lock-hint")
    assert hint.is_hidden(), "nothing to say until something is held"

    still_page.evaluate(
        "() => document.getElementById('lock')"
        ".dispatchEvent(new PointerEvent('pointerdown', {pointerId: 1}))"
    )
    assert hint.is_visible(), "the hold showed no sign of being a hold"
    assert "hold" in still_page.locator("#lock-hint-text").inner_text().lower()
    assert still_page.evaluate(
        "() => document.getElementById('lock-fill').classList.contains('filling')"
    ), "the bar does not fill, so the hold still has no length"

    still_page.wait_for_timeout(800)
    assert not still_page.evaluate(
        "document.getElementById('player').classList.contains('locked')"
    )


def test_letting_go_early_says_so_rather_than_doing_nothing(still_page):
    still_page.click("#lock")
    still_page.evaluate(
        "() => document.getElementById('lock')"
        ".dispatchEvent(new PointerEvent('pointerdown', {pointerId: 2}))"
    )
    still_page.wait_for_timeout(120)
    still_page.evaluate(
        "() => document.getElementById('lock')"
        ".dispatchEvent(new PointerEvent('pointerup', {pointerId: 2}))"
    )

    assert still_page.evaluate(
        "document.getElementById('player').classList.contains('locked')"
    ), "a short hold unlocked it"
    assert "longer" in still_page.locator("#lock-hint-text").inner_text()


def test_a_tap_on_a_dead_control_says_why_nothing_happened(still_page):
    """The controls are `pointer-events: none`, so the tap falls through to the
    bar. Saying why is the difference between a lock and a broken player."""
    still_page.click("#lock")
    still_page.evaluate(
        "() => document.getElementById('player')"
        ".dispatchEvent(new PointerEvent('pointerdown', {pointerId: 3, bubbles: true}))"
    )

    assert still_page.locator("#lock-hint").is_visible()
    assert "Locked" in still_page.locator("#lock-hint-text").inner_text()


def test_the_output_delay_is_measured_and_not_asked_for(still_page):
    """The slider was the whole answer once, and a control for something the
    browser already knows is a control that should not exist.

    `AudioContext.outputLatency` is the output *device's* latency, so a
    context with nothing connected to it reports the number while the audio
    element keeps its own path to the speaker. It reads 0 until the device's
    stream is open, which is why it is asked after play and not at load.
    """
    said = still_page.locator("#sync-detected")
    assert "press play" in said.inner_text(), "it claimed to have measured before playing"

    still_page.evaluate("document.getElementById('audio').play()")
    still_page.wait_for_function(
        "() => /reports \\d+ ms|will not say/.test("
        "document.getElementById('sync-detected').textContent)",
        timeout=20000,
    )
    text = said.inner_text()
    assert "press play" not in text

    # Whatever it found is applied, and the slider is only the leftover.
    assert still_page.evaluate("document.getElementById('opt-sync').value") == "0"
    still_page.evaluate("document.getElementById('audio').pause()")


def test_the_measured_delay_actually_moves_the_highlight(still_page, live):
    """Detected and trimmed are added together, so a device that reports 200 ms
    holds the highlight back by 200 ms with the slider still at zero."""
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks
    audio = "document.getElementById('audio')"

    to_first_section(still_page)
    still_page.evaluate(f"{audio}.currentTime = {blocks[2].start_ms / 1000 + 0.3}")
    still_page.wait_for_timeout(250)
    assert active_id(still_page) == blocks[2].id

    # The trim is the same arithmetic the detected number goes through.
    still_page.evaluate(
        "() => { const s = document.getElementById('opt-sync');"
        " s.value = '1000'; s.dispatchEvent(new Event('input')); }"
    )
    still_page.wait_for_timeout(250)
    assert active_id(still_page) == blocks[1].id
    still_page.evaluate(
        "() => { const s = document.getElementById('opt-sync');"
        " s.value = '0'; s.dispatchEvent(new Event('input')); }"
    )


def test_a_touch_screen_never_gets_a_hover_style(live, browser):
    """iOS paints the hover state on the first tap and activates on the second,
    so a hover rule is why the padlock took two taps on a phone and one on a
    desktop — and why "scroll with the audio" looked armed after any tap,
    since its hover background is the same one its armed state uses.

    Asserted over the whole stylesheet rather than over the two controls that
    were reported: any hover rule reintroduced without the guard brings both
    faults straight back.
    """
    base, slug, _manifest = live
    context = browser.new_context(
        viewport={"width": 400, "height": 780}, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}")
        page.wait_for_selector("#doc .b")

        loose = page.evaluate(
            """() => {
                const found = [];
                const walk = (rules, guarded) => {
                    for (const rule of rules) {
                        if (rule.type === CSSRule.MEDIA_RULE) {
                            walk(rule.cssRules,
                                 guarded || rule.conditionText.includes("hover: hover"));
                        } else if (rule.type === CSSRule.STYLE_RULE && !guarded
                                   && rule.selectorText.includes(":hover")) {
                            found.push(rule.selectorText);
                        }
                    }
                };
                for (const sheet of document.styleSheets) {
                    try { walk(sheet.cssRules, false); } catch (e) { /* other origin */ }
                }
                return found;
            }"""
        )
        assert loose == [], f"hover rules a phone will apply on tap: {loose}"
    finally:
        context.close()


def test_scroll_with_the_audio_reads_the_same_armed_on_a_phone(live, browser):
    """Armed is the accent colour and a filled background; disarmed is
    nothing. On a phone the sticky hover painted that same background on a
    disarmed button, so the only difference left was the arrow's brightness.
    """
    base, slug, _manifest = live
    context = browser.new_context(
        viewport={"width": 400, "height": 780}, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}")
        page.wait_for_selector("#player:not([hidden])")

        def look():
            # After the transition, not during it. `button` moves
            # background-color over 120 ms, so an immediate read catches the
            # colour half way out and reports an alpha nobody ever authored.
            page.wait_for_timeout(300)
            return page.evaluate(
                "() => { const s = getComputedStyle(document.getElementById('follow'));"
                " return {bg: s.backgroundColor, fg: s.color}; }"
            )

        # It starts armed, and the class of the reader is that it stays put.
        assert page.get_attribute("#follow", "aria-pressed") == "true"
        armed = look()

        page.tap("#follow")
        assert page.get_attribute("#follow", "aria-pressed") == "false"
        disarmed = look()

        assert armed != disarmed, "the two states look the same"
        assert armed["bg"] != disarmed["bg"], "only the arrow's brightness changed"
        # Disarmed is no effect at all, not a lighter effect.
        assert disarmed["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), disarmed["bg"]

        page.tap("#follow")
        assert page.get_attribute("#follow", "aria-pressed") == "true"
        assert look() == armed, "a tap left something behind"
    finally:
        context.close()


def test_following_a_tall_block_tracks_the_word_not_the_block(live_words, browser):
    """A paragraph taller than the band can never be "inside" it, so the
    block-level rule pinned its first line to the top of the band and then
    had nothing left to do. The read-along walked off the bottom of the
    screen for the rest of the block, which on a phone is most of them, and a
    quote carrying several paragraphs is one block too."""
    base, slug, manifest = live_words
    target = max(manifest.sections[0].blocks, key=lambda b: len(b.words))
    assert len(target.words) >= 6, "fixture block needs enough words to run off a screen"

    context = browser.new_context(viewport={"width": 420, "height": 640})
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => { const a = document.getElementById('audio');"
            " return a && a.textTracks.length && a.textTracks[0].cues"
            " && a.textTracks[0].cues.length > 0; }",
            timeout=20000,
        )
        # Make this one block several screens tall, which is what a long
        # paragraph already is at a phone's width.
        page.evaluate(
            f"() => {{ const el = document.getElementById('{target.id}');"
            " el.style.fontSize = '40px'; el.style.lineHeight = '5'; }"
        )

        last = target.words[-1]
        page.evaluate(
            "document.getElementById('audio').currentTime = "
            f"{(last.start_ms + last.dur_ms / 2) / 1000}"
        )
        page.wait_for_function(
            f"() => {{ const el = document.querySelector('#{target.id} .w.on');"
            f" return el && el.dataset.w === '{len(target.words) - 1}'; }}",
            timeout=10000,
        )
        # It has to settle: the scroll that brings the word back is smooth.
        page.wait_for_timeout(1200)

        visible = page.evaluate(
            f"""() => {{
                const el = document.querySelector('#{target.id} .w.on');
                const header = document.querySelector('header');
                const player = document.getElementById('player');
                const box = el.getBoundingClientRect();
                const top = header ? header.getBoundingClientRect().bottom : 0;
                const bottom = (window.visualViewport || window).height
                    - (player && !player.hidden ? player.getBoundingClientRect().height : 0);
                return {{ ok: box.top >= top && box.bottom <= bottom,
                          wordTop: box.top, bandTop: top, bandBottom: bottom }};
            }}"""
        )
        assert visible["ok"], f"the lit word is outside the readable band: {visible}"
    finally:
        context.close()


def test_a_redirected_page_is_told_apart_from_the_one_that_was_asked_for(live, browser):
    """`fetch` follows redirects and hands back the destination, so an
    expired session turns a GET of /a/<slug> into a 200 for the sign-in page
    -- which the worker stored under the article's own address, and the next
    flight opened a kept article and found a form.

    `response.redirected` is what tells the two apart, and this is the half
    of the fix that can be pinned down without racing the cache: a page that
    was served straight must not report it, or the guard would stop
    refreshing every kept article instead of only the wrong ones.
    """
    base, slug, _manifest = live
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")

        straight = page.evaluate(
            "async (url) => (await fetch(url)).redirected", f"/a/{slug}"
        )
        assert straight is False, "an ordinary article page reports itself redirected"

        # Intercepted below the service worker, so this is a real redirect
        # chain in the browser and not a stub of one.
        context.route(f"**/a/{slug}", lambda route: route.fulfill(
            status=303, headers={"location": "/login"}
        ))
        bounced = page.evaluate(
            "async (url) => (await fetch(url)).redirected", f"/a/{slug}"
        )
        assert bounced is True, "a bounced page is indistinguishable from the real one"
    finally:
        context.close()


# --------------------------------------------------------------------------
# hint bubbles
# --------------------------------------------------------------------------


@pytest.fixture
def hints_page(live, browser):
    """`/pronunciations` on a phone-sized viewport. Seven hints, no player."""
    base, _slug, _manifest = live
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = context.new_page()
    page.goto(f"{base}/pronunciations", wait_until="domcontentloaded")
    page.wait_for_selector(".tip > .q")
    yield page
    context.close()


def tap_without_focus(page, index: int = 0) -> None:
    """Click a hint the way an iPhone does: an event, and no focus.

    Safari does not focus a <button> when you tap it, and every browser on
    iOS is Safari underneath -- so on a phone `:focus-within`, which used to
    be the whole of the tap story, never fired once and no hint opened at
    all. `el.click()` moves focus in no engine, which makes this the same
    condition on a desktop Chromium that can be run here.
    """
    page.evaluate(
        "(i) => document.querySelectorAll('.tip > .q')[i].click()", index
    )
    page.wait_for_timeout(120)


def shown(page, index: int = 0) -> bool:
    return page.evaluate(
        "(i) => getComputedStyle(document.querySelectorAll('.tip-body')[i]).display",
        index,
    ) == "block"


def test_a_hint_opens_on_a_tap_that_does_not_focus_its_button(hints_page):
    tap_without_focus(hints_page)
    assert hints_page.evaluate("() => document.activeElement.tagName") == "BODY", (
        "the test did not reproduce the iPhone: the button took focus"
    )
    assert shown(hints_page), "the hint did not open"
    assert hints_page.evaluate(
        "() => document.querySelectorAll('.tip > .q')[0].getAttribute('aria-expanded')"
    ) == "true"


def test_a_hint_closes_again_the_three_ways_it_can(hints_page):
    """One test for the dismiss paths, because they are one mechanism: the
    handler tracks a single open tip and clears it. A tap inside the bubble
    is reading it -- they carry bold runs and the odd link -- not dismissing
    it."""
    tap_without_focus(hints_page)
    tap_without_focus(hints_page)
    assert not shown(hints_page), "a second tap on the same button left it open"

    tap_without_focus(hints_page, 0)
    tap_without_focus(hints_page, 1)
    assert not shown(hints_page, 0), "two hints were open at once"

    hints_page.evaluate("() => document.querySelectorAll('.tip-body')[1].click()")
    hints_page.wait_for_timeout(120)
    assert shown(hints_page, 1), "reading the bubble dismissed it"

    hints_page.evaluate("() => document.querySelector('h1').click()")
    hints_page.wait_for_timeout(120)
    assert not shown(hints_page, 1), "a tap outside left it open"

    tap_without_focus(hints_page)
    hints_page.keyboard.press("Escape")
    hints_page.wait_for_timeout(120)
    assert not shown(hints_page), "Escape left it open"


def test_a_hint_on_a_phone_opens_in_front_of_the_player(live, browser):
    """Under 44rem the bubble is fixed to the bottom of the viewport, which is
    where the player already sits. Both carried z-index 30, and the player is
    last in reader.html -- so the bubble opened behind the transport and
    showed about six pixels of itself."""
    base, slug, _manifest = live
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = context.new_page()
    try:
        page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
        page.wait_for_selector(".tip > .q")
        page.wait_for_function("() => { const p = document.getElementById('player');"
                               " return p && !p.hidden; }", timeout=20000)
        tap_without_focus(page)

        seen = page.evaluate("""() => {
          const body = document.querySelector('.tip.open > .tip-body');
          const r = body.getBoundingClientRect();
          const top = document.elementFromPoint(
            r.left + r.width / 2, r.top + Math.min(20, r.height / 2));
          return { open: getComputedStyle(body).display === 'block',
                   overPlayer: !!(top && top.closest('.player')) };
        }""")
        assert seen["open"], "the hint did not open on the reader page"
        assert not seen["overPlayer"], "the player is drawn on top of the hint"
    finally:
        context.close()


def _settled(page) -> float:
    """The clock, once the element has stopped seeking."""
    page.wait_for_function("() => !document.getElementById('audio').seeking", timeout=10000)
    return page.evaluate("document.getElementById('audio').currentTime")


def test_a_burst_of_back_skips_moves_one_step_for_every_press(still_page, live):
    """Eight presses in random bursts, with listening in between.

    media-chrome's skip buttons work their target out from the
    `mediacurrenttime` *attribute*, and the controller refreshes that on
    `timeupdate` and `loadedmetadata` only. Every press inside one of those
    windows read the same stale number and asked for the same destination, so
    a burst moved one step however many times it was pressed, and the
    read-along followed the audio somewhere nobody had asked for.

    A burst is fired back to back rather than at some measured spacing,
    because the window is what matters and its width is not ours: headless
    Chromium refreshes far faster than a phone, so pressing on a timer here
    would test the harness's `timeupdate` rate rather than the bug. Back to
    back is one window on any browser, and is what a fast double tap is.

    Bursts are two or three, never one -- a single press is right even with
    the stale read, so it discriminates nothing. The step is shrunk to a
    second so eight presses fit the fixture's audio; the step is not under
    test. The rhythm is random so no one rhythm is special-cased, and seeded
    so a failure runs again.
    """
    _base, _slug, manifest = live
    blocks = manifest.sections[0].blocks
    rng = random.Random(20260908)
    step, presses = 1.0, 8

    still_page.evaluate(
        "(s) => document.querySelector('media-seek-backward-button')"
        ".setAttribute('seekoffset', String(s))",
        step,
    )
    duration = still_page.evaluate("document.getElementById('audio').duration")
    assert duration > presses * step + 3, "the fixture's audio is too short for this test"

    seek_to(still_page, duration * 0.6)
    still_page.evaluate("document.getElementById('audio').play()")
    still_page.wait_for_timeout(500)

    left = presses
    while left:
        burst = min(left, rng.randint(2, 3))
        left -= burst

        before = _settled(still_page)
        still_page.evaluate(
            "(n) => { const b = document.querySelector('media-seek-backward-button');"
            " for (let i = 0; i < n; i++) b.click(); }",
            burst,
        )
        after = _settled(still_page)

        moved = before - after
        assert moved == pytest.approx(burst * step, abs=0.6), (
            f"{burst} presses moved {moved:.2f}s, not {burst * step:.2f}s"
        )

        # Listen for a moment before the next burst, the way a person would.
        still_page.wait_for_timeout(rng.randint(400, 900))
        assert not still_page.evaluate("document.getElementById('audio').paused"), (
            "a burst of skips left the audio paused"
        )

        # Read both in one go: apart, the clock moves between them.
        state = still_page.evaluate(
            "() => ({ at: document.getElementById('audio').currentTime * 1000,"
            " lit: (document.querySelector('#doc .b.on') || {}).id || null })"
        )
        assert state["lit"] and state["lit"].startswith("b0-"), (
            f"the skips left section 0 for {state['lit']}"
        )
        lit = next(b for b in blocks if b.id == state["lit"])
        assert lit.start_ms - 400 <= state["at"] <= lit.start_ms + lit.dur_ms + 400, (
            f"{state['lit']} is lit at {state['at']:.0f} ms, but it runs "
            f"{lit.start_ms}-{lit.start_ms + lit.dur_ms} ms"
        )


@pytest.fixture
def trimmed_page(live_words, browser):
    """A word-aligned reader carrying a known manual sync trim.

    The trim stands in for the output latency a headless browser has none of:
    `AudioContext.outputLatency` reads 0 here, so `detectedMs` stays 0 and the
    hold-back would otherwise be nothing to measure. It is read once at script
    start, hence the reload.
    """
    base, slug, _ = live_words
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
    page.evaluate("() => localStorage.setItem('tc:sync-offset', '1000')")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a && a.readyState >= 1 && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )
    yield page
    context.close()


def _lit_word_start(page, manifest) -> float:
    """Where the lit word begins, in ms of the section's own clock."""
    state = page.evaluate(
        "() => { const b = document.querySelector('#doc .b.on');"
        " const w = document.querySelector('#doc .w.on');"
        " return b && w ? { block: b.id, word: Number(w.dataset.w) } : null; }"
    )
    assert state, "nothing is lit"
    block = next(b for b in manifest.sections[0].blocks if b.id == state["block"])
    # `word.start_ms` is already the section's own clock, not a delta against
    # the block -- only the JSON the page carries delta-encodes it.
    return block.words[state["word"]].start_ms


@pytest.mark.parametrize("rate", [1.0, 2.0])
def test_the_hold_back_is_media_time_so_the_rate_scales_it(trimmed_page, live_words, rate):
    """The output delay is wall-clock; the clock it is subtracted from is not.

    Whatever sits between the decoder and the speaker is a fixed amount of
    *real* time, so at 2x it is twice as much speech. Held back by the
    unscaled number, the highlight led by a whole trim's worth of media time
    at every rate above 1 -- half a second of words at 2x, and worse the
    faster you went. media-chrome's rate button offers 0.9 to 2.

    The trim is 1000 ms, so the lit word must start a second of media time
    behind the playhead at 1x and two seconds behind it at 2x.
    """
    _base, _slug, manifest = live_words
    trim_ms = 1000.0
    at_ms = 6000.0

    trimmed_page.evaluate("(r) => { document.getElementById('audio').playbackRate = r; }", rate)
    seek_to(trimmed_page, at_ms / 1000)
    trimmed_page.wait_for_function(
        "() => document.querySelector('#doc .w.on') !== null", timeout=10000
    )

    lit = _lit_word_start(trimmed_page, manifest)
    want = at_ms - trim_ms * rate

    # One word of slack: the lit word is the last one to have *started* by
    # then, so it begins at or just before the moment being asked about.
    assert want - 700 <= lit <= want + 50, (
        f"at {rate}x the lit word starts at {lit:.0f} ms; "
        f"a {trim_ms:.0f} ms hold-back puts it near {want:.0f} ms"
    )


def test_the_measured_latency_is_remembered_for_the_next_listen(live_words, browser):
    """`outputLatency` reads 0 until the output stream is open.

    Measuring takes up to a second and a bit after play, and starting from
    zero left the highlight running the whole latency ahead of the voice for
    that first second -- at the start of a listen, which is when somebody is
    looking at the words to find their place. The last answer this device
    gave is a better opening guess than nothing.
    """
    base, slug, _ = live_words
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")

    # Stand in for a measurement this device made on an earlier visit.
    page.evaluate("() => localStorage.setItem('tc:output-latency', '180')")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a && a.readyState >= 1 && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )

    # The sheet reports what the player is holding back, before any play.
    shown = page.evaluate("() => (document.getElementById('sync-detected') || {}).textContent || ''")
    context.close()

    assert "180" in shown, f"the remembered 180 ms was not picked up: {shown!r}"


def test_the_lock_screen_is_told_the_playback_rate(still_page):
    """Without a rate the OS draws its scrubber assuming 1x.

    `setPositionState` is what the lock screen extrapolates from between
    updates. Given no rate it assumes one, so at 1.75x its bar crawled a
    third of the way through a section while the audio finished it.
    """
    still_page.evaluate(
        "() => { window.__pos = null;"
        " navigator.mediaSession.setPositionState = (s) => { window.__pos = s; }; }"
    )
    seek_to(still_page, 4)
    still_page.evaluate("() => { document.getElementById('audio').playbackRate = 1.75; }")
    still_page.wait_for_function("() => window.__pos !== null", timeout=5000)

    state = still_page.evaluate("() => window.__pos")
    real = still_page.evaluate(
        "() => ({ d: document.getElementById('audio').duration,"
        " t: document.getElementById('audio').currentTime })"
    )

    assert state["playbackRate"] == 1.75
    assert state["duration"] == pytest.approx(real["d"], abs=0.01)
    assert state["position"] == pytest.approx(real["t"], abs=0.5)
    assert state["position"] <= state["duration"], "a position past the duration throws"


def test_the_lock_screen_scrubber_can_seek(live, browser):
    """A `seekto` handler, or the OS draws a scrubber that does nothing.

    The handler is caught as the page registers it, which is the only way to
    drive it: the real one is invoked by the operating system.
    """
    base, slug, _ = live
    context = browser.new_context()
    context.add_init_script("""
      (() => {
        window.__actions = {};
        const ms = navigator.mediaSession;
        if (!ms || !ms.setActionHandler) return;
        const real = ms.setActionHandler.bind(ms);
        ms.setActionHandler = (name, fn) => {
          window.__actions[name] = fn;
          try { real(name, fn); } catch (e) { /* unsupported action */ }
        };
      })();
    """)
    page = context.new_page()
    page.goto(f"{base}/a/{slug}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a && a.readyState >= 1 && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )
    try:
        page.evaluate("document.getElementById('audio').pause()")
        seek_to(page, 2)

        assert page.evaluate("() => typeof window.__actions.seekto === 'function'"), (
            "the lock screen was given no way to seek"
        )
        page.evaluate("() => window.__actions.seekto({ seekTime: 9 })")
        page.wait_for_function(
            "() => Math.abs(document.getElementById('audio').currentTime - 9) < 0.3",
            timeout=10000,
        )
        assert page.evaluate("document.getElementById('audio').paused"), (
            "seeking from the lock screen started playback on its own"
        )
    finally:
        context.close()


def test_the_build_checkboxes_line_up(still_page):
    """A `?` beside one checkbox must not lift it above the others.

    The one that carries a tip is wrapped in `.titled` so the box and the `?`
    sit side by side, and `.titled` has a bottom margin for its *other* use --
    a heading above a control, where the gap belongs underneath. Dropped
    straight into a `.row`, which centres what it is given, that margin lifted
    the checkbox by half of itself: 2.8 px, small enough to read as a mistake
    and large enough to see.
    """
    tops = still_page.evaluate(
        "() => { const row = document.querySelector("
        "'input[name=\"skip_word_highlight\"]').closest('.row');"
        " return Array.from(row.querySelectorAll('input[type=checkbox]'))"
        ".map(i => [i.name, Math.round(i.getBoundingClientRect().top * 10) / 10]); }"
    )

    assert len(tops) >= 4, f"expected the build checkboxes, found {tops}"
    assert len({top for _name, top in tops}) == 1, f"these do not line up: {tops}"
