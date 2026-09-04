"""The bookmarklet's actual path: a page on another origin posting here.

Every other test of this posts from the test client, which is same-site by
construction and therefore tests the one case that was never in doubt. What
had only ever been checked by hand is the real thing — a form on a different
origin, a POST, and a landing on the article — and that is the case the whole
design of the ingest key exists for: the session cookie is `SameSite=Lax`, so
a browser sends it on a top-level GET and never on a cross-site POST.

Two hostnames, not two ports. This was written with two ports first and every
assertion about the cookie passed for the wrong reason: **same-site is decided
by the registrable domain and ignores the port entirely**, so `127.0.0.1:8000`
and `127.0.0.1:9000` are cross-*origin* and same-*site*, and `SameSite=Lax`
never came into it. Chromium is started with `--host-resolver-rules`, so
`app.test` and `reader.test` both resolve to the loopback and are two sites as
far as the cookie is concerned.

Both origins are http here. The mixed-content rule an https page runs into is
the browser refusing to post to an http address at all, which no local server
can stand in for; what is testable is everything after that, and this is it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from textcast import db

pytest.importorskip("playwright", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

PASSWORD = "open-sesame"


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def guarded(tmp_path_factory):
    """The app with access control on, and the ingest key it seeded."""
    data = tmp_path_factory.mktemp("data")
    env = {
        **os.environ,
        "TEXTCAST_DATA_DIR": str(data),
        "TEXTCAST_WORKERS": "0",
        "TEXTCAST_REQUIRE_AUTH": "1",
        "TEXTCAST_USERNAME": "reader",
        "TEXTCAST_AUTH_TOKEN": PASSWORD,
    }
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "textcast.web.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("the app did not start")
    # The browser reaches it by name; the resolver rule below points the name
    # back here. Two names is what makes the two sites two sites.
    base = f"http://app.test:{port}"

    # The key the account was seeded with, read the way the Add page reads it.
    from textcast import accounts

    db.close()
    conn = db.connect(data / "textcast.db")
    account = accounts.get(conn)
    db.close()
    assert account is not None, "the account was not seeded from the environment"

    yield base, account.ingest_key
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def elsewhere():
    """A one-page site on a different *site*, which is the whole point.

    `reader.test` against the app's `app.test`. Not a different port: a port
    makes an origin and not a site, and a cookie does not care about it.
    """
    pages: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://reader.test:{port}", pages
    server.shutdown()


def form_page(action: str, fields: dict[str, str]) -> bytes:
    """What the bookmarklet builds: a form, posted, and never a fetch.

    A fetch would need CORS. A form POST does not, which is why the
    bookmarklet is written this way and why the answer has to be a redirect
    the browser can follow rather than JSON nobody reads.
    """
    inputs = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in fields.items()
    )
    return (
        "<!doctype html><meta charset=utf-8><title>Another site</title>"
        f'<form id="send" method="POST" action="{action}" '
        'enctype="application/x-www-form-urlencoded">'
        f"{inputs}</form>"
        '<button onclick="document.getElementById(\'send\').submit()">Send</button>'
    ).encode()


@pytest.fixture(scope="module")
def browser():
    try:
        pw = sync_playwright().start()
        # `.test` is a reserved TLD, so app.test and reader.test are two
        # registrable domains and therefore two sites. Both resolve here.
        launched = pw.chromium.launch(
            args=["--host-resolver-rules=MAP *.test 127.0.0.1"]
        )
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    yield launched
    launched.close()
    pw.stop()


def test_a_form_on_another_origin_lands_on_the_article(guarded, elsewhere, browser):
    """The path the bookmarklet actually takes, end to end.

    It posts from whatever site you are reading, so the request is cross-site
    and carries no cookie. The key in the body is what gets it in, and the
    redirect has to hand back a session or the GET that follows it bounces to
    the sign-in page — which is what it used to do.
    """
    base, key = guarded
    other, pages = elsewhere
    pages["/read.html"] = form_page(
        f"{base}/api/ingest",
        {
            "kind": "text",
            "title": "Sent from another origin",
            "text": "A paragraph the browser had and the server could not fetch.",
            "token": key,
        },
    )

    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{other}/read.html")
        assert not context.cookies(), "nothing has signed in, so there is no cookie"

        page.click("button")
        page.wait_for_url(f"{base}/a/sent-from-another-origin", timeout=20000)

        assert page.locator("#doc .b").count() > 0, "the article did not render"
        assert "A paragraph the browser had" in page.locator("#doc").inner_text()

        # The redirect handed back a session, so reading on works.
        page.goto(f"{base}/")
        assert "/login" not in page.url, "the browser was not given a session"
    finally:
        context.close()


def test_the_same_post_without_a_key_never_reaches_the_library(guarded, elsewhere, browser):
    """The other half. If a cross-site POST were enough on its own, every page
    on the internet could add to this library — and the reason the session
    cookie stays `Lax` is that it would then be able to delete from it."""
    base, _key = guarded
    other, pages = elsewhere
    pages["/nokey.html"] = form_page(
        f"{base}/api/ingest",
        {"kind": "text", "title": "Never stored", "text": "This should not land."},
    )

    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{other}/nokey.html")
        page.click("button")
        page.wait_for_load_state("domcontentloaded")

        assert "/a/never-stored" not in page.url
        body = page.content()
        assert "Never stored" not in body or "sign in" in body.lower()
    finally:
        context.close()


def test_a_signed_in_browser_still_does_not_lend_its_cookie_cross_site(
    guarded, elsewhere, browser
):
    """`SameSite=Lax` is the load-bearing part, so it is asserted rather than
    assumed. Signed in, in the same browser, on another origin, with no key —
    still refused. That is what lets `/a/<slug>/delete` stay a plain POST."""
    base, _key = guarded
    other, pages = elsewhere
    pages["/borrow.html"] = form_page(
        f"{base}/api/ingest",
        {"kind": "text", "title": "Borrowed cookie", "text": "Should not land either."},
    )

    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{base}/login")
        page.fill("input[name=username]", "reader")
        page.fill("input[name=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/**", timeout=20000)
        assert "/login" not in page.url, "the sign-in did not take"

        page.goto(f"{other}/borrow.html")
        page.click("button")
        page.wait_for_load_state("domcontentloaded")
        assert "/a/borrowed-cookie" not in page.url
    finally:
        context.close()
