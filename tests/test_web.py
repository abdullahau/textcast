"""The web app: access control, the service worker, and the offline endpoint."""

from __future__ import annotations

import pytest

from textcast import __version__, db

pytest.importorskip("fastapi", reason="the web extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

from textcast.web import app as web  # noqa: E402


@pytest.fixture
def client(settings, monkeypatch):
    """A client whose app reads this test's settings, and never loads a model."""
    monkeypatch.setattr(web, "settings", settings)
    # Listing voices builds an engine. No test here needs the voice picker.
    monkeypatch.setattr(web, "_voices", lambda: [])
    db.init(settings.db_path)
    with TestClient(web.app) as running:
        yield running


def sign_in_required(settings, token: str = "open-sesame") -> None:
    settings.require_auth = True
    settings.auth_token = token


def test_the_service_worker_is_stamped_with_the_package_version(client):
    """Its cache names carry BUILD, and bumping that by hand was forgotten once."""
    web._service_worker_source.cache_clear()

    body = client.get("/sw.js").text

    assert f'const BUILD = "{__version__}"' in body
    assert '"dev"' not in body


def test_the_worker_caches_every_shell_file_the_pages_ask_for(client):
    body = client.get("/sw.js").text
    shell = [line for line in body.splitlines() if line.strip().startswith('"/static/')]

    for asset in ("app.css", "player.js", "progress.js", "tags.js"):
        assert any(asset in line for line in shell), f"{asset} is loaded but never cached"


def test_without_auth_the_library_is_simply_open(client):
    assert client.get("/").status_code == 200


def test_a_browser_is_sent_to_sign_in_and_an_api_call_is_refused(client, settings):
    sign_in_required(settings)

    page = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    api = client.get("/api/tags", headers={"accept": "application/json"})

    assert page.status_code == 303
    assert page.headers["location"].startswith("/login?next=")
    assert api.status_code == 401


def test_signing_in_opens_the_library_and_signing_out_closes_it(client, settings):
    sign_in_required(settings)
    browser = {"accept": "text/html"}

    client.post("/login", data={"token": "open-sesame", "next": "/"}, follow_redirects=False)
    assert client.get("/", headers=browser).status_code == 200

    client.post("/logout", follow_redirects=False)
    assert client.get("/", headers=browser, follow_redirects=False).status_code == 303


def test_a_wrong_token_is_rejected_without_setting_a_cookie(client, settings):
    sign_in_required(settings)

    response = client.post("/login", data={"token": "guess", "next": "/"})

    assert "does not match" in response.text
    assert web.COOKIE not in client.cookies


def test_sign_in_never_redirects_off_this_host(client, settings):
    sign_in_required(settings)

    response = client.post(
        "/login",
        data={"token": "open-sesame", "next": "//evil.example.com/"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_the_login_page_says_so_when_no_token_is_configured(client, settings):
    settings.require_auth = True
    settings.auth_token = ""

    assert "No token is set" in client.get("/login").text


def test_the_summaries_page_says_when_nothing_is_configured(client, monkeypatch):
    for name in ("TEXTCAST_SUMMARY_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    body = client.get("/summaries").text

    assert "not configured" in body
    assert "generativelanguage.googleapis.com" in body, "an endpoint is offered, not demanded"


def test_saving_the_model_alone_keeps_the_stored_key(client, conn):
    from textcast import summarize

    summarize.save_config(conn, api_key="secret")

    client.post("/summaries", data={"model": "a-model", "keep_key": "true", "api_key": ""})

    assert summarize.config(conn).api_key == "secret"
    assert summarize.config(conn).model == "a-model"


def test_the_normal_reading_pace_is_not_stored_as_an_option(client):
    """1.0 is the default, so writing it down would only be noise on every build."""
    assert web._build_options("", "", False, speed="1.0") == {}
    assert web._build_options("", "", False, speed="1.2") == {"speed": 1.2}
    assert web._build_options("", "", False, speed="nonsense") == {}
    assert web._build_options("", "", False, speed="9") == {}, "outside what a voice can do"


def test_the_reading_pace_picker_opens_at_the_normal_pace(client, conn, monkeypatch):
    """%g wrote 1.0 as "1", which matched no option, so the picker opened at 0.8x."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda: [])
    doc = Article(title="A paced note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)

    body = client.get("/a/a-paced-note").text
    assert '<option value="1.0" selected>' in body.replace(' selected ', ' selected')

    db.set_build_options(article_id, {"speed": 1.2}, conn)
    body = client.get("/a/a-paced-note").text
    assert '<option value="1.2" selected>' in body.replace(' selected ', ' selected')


def test_the_quote_voice_hint_is_a_button_not_a_wall_of_text(client, conn, monkeypatch):
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda: [])
    doc = Article(title="A hinted note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/a-hinted-note").text

    assert body.count('<span class="tip">') == 2, "one by quote voice, one by the actions"
    assert "Start quote" in body
    # Hover and focus, not click: a details element took two taps on a phone.
    assert "<details" not in body
    # A label may not contain interactive content of its own.
    assert '<label class="titled"' not in body


def test_the_reader_offers_star_and_archive_above_the_article(client, conn, monkeypatch):
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda: [])
    doc = Article(title="A note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/a-note").text
    head = body[: body.index('class="doc"')]

    assert 'aria-pressed="false"' in head, "star and archive sit in the header"
    assert head.count('/flag') == 2
    assert "Modify article" in body


def test_block_text_for_the_offline_cache_can_skip_footnotes(client, conn):
    from textcast.document import Article, Block, BlockKind, Section

    article = Article(
        title="Offline",
        sections=[Section(title="One", blocks=[
            Block(kind=BlockKind.PARA, text="The body of it."),
            Block(kind=BlockKind.FOOTNOTE, text="An aside."),
        ])],
    ).renumber()
    article_id = db.save_article(article, conn)

    everything = client.get(f"/api/blocks/{article_id}").json()["blocks"]
    prose = client.get(f"/api/blocks/{article_id}?kinds=para").json()["blocks"]

    assert len(everything) == 2
    assert [b["text"] for b in prose] == ["The body of it."]


def test_the_voice_page_saves_a_default_without_queueing_anything(client, conn):
    """Changing the default must not rebuild the library behind your back."""
    from textcast import prefs
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Untouched", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    client.post("/pronunciations/defaults",
                data={"voice": "bm_george", "quote_voice": "af_heart", "speed": "1.2"})

    chosen = prefs.voice_defaults(conn)
    assert (chosen.voice, chosen.quote_voice, chosen.speed_label) == ("bm_george", "af_heart", "1.2")
    assert db.active_jobs(conn) == [], "no build was queued"


def test_a_saved_default_reaches_the_pages_that_offer_it(client, conn):
    from textcast import prefs
    from textcast.document import Article, Block, BlockKind, Section

    prefs.save_voice_defaults(conn, voice="bm_george", speed="1.2")
    doc = Article(title="Follows the default", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    assert "Default (bm_george)" in client.get("/add").text
    assert '<option value="1.2" selected>' in client.get("/a/follows-the-default").text


def test_a_build_uses_the_saved_default_over_the_environment(conn, settings, monkeypatch):
    from textcast import prefs
    from textcast.jobs import Worker
    from textcast.service import ingest

    monkeypatch.setattr(settings, "voice", "af_heart")
    prefs.save_voice_defaults(conn, voice="bm_george", speed="1.2")
    stored = ingest(text="A note.\n\nWith two paragraphs.", title="A note")

    seen = {}

    class Recorder:
        name, sample_rate = "fake", 24000

        def voices(self):
            return []

        def synthesize(self, text, voice=None, speed=1.0, lang="en"):
            import numpy as np

            seen["voice"], seen["speed"] = voice, speed
            return __import__("textcast.tts.base", fromlist=["Clip"]).Clip(
                samples=np.zeros(24000, dtype=np.float32), sample_rate=24000
            )

    worker = Worker(settings)
    worker.engines_for = lambda name: [Recorder()]
    worker.step()

    assert seen == {"voice": "bm_george", "speed": 1.2}
    assert db.get_article(stored.article_id, conn)["status"] in ("ready", "failed")
