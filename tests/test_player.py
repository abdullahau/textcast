"""The read-along player, driven in a real browser.

Sync correctness cannot be asserted from Python: it depends on the browser's
WebVTT "time marches on" algorithm firing `cuechange` against a real decoded
audio file. So this starts the app, builds an article with a stub engine, and
drives Chromium.

Skipped unless playwright and its Chromium build are present:

    uv pip install playwright && playwright install chromium
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

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
            Section(title="Linqto", blocks=[para(2), para(4)]),
        ],
    ).renumber()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        " return a && a.textTracks.length && a.textTracks[0].cues"
        " && a.textTracks[0].cues.length > 0; }",
        timeout=20000,
    )
    yield page
    context.close()


def active_id(page):
    return page.evaluate("(document.querySelector('#doc .b.on') || {}).id || null")


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

    page.evaluate(f"document.getElementById('audio').currentTime = {at}")
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

    page.evaluate(f"document.getElementById('audio').currentTime = {first.start_ms / 1000}")
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


def test_blocks_are_paragraphs_not_buttons(page):
    """A <button> wrapper is what broke selection in the first place."""
    tags = page.evaluate(
        "Array.from(document.querySelectorAll('#doc .b')).map(e => e.tagName)"
    )
    assert set(tags) == {"P"}


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

        audio_url = f"/media/{slug}/{manifest.sections[0].file}"
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
    still_page.evaluate(f"document.getElementById('audio').currentTime = {target.start_ms / 1000}")
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
        still_page.evaluate(f"document.getElementById('audio').currentTime = {target.start_ms / 1000}")
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
    still_page.evaluate(f"document.getElementById('audio').currentTime = {target.start_ms / 1000 + 0.3}")
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
