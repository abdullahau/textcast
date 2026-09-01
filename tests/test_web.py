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

    # The Add page no longer chooses a voice; the article page does.
    page = client.get("/a/follows-the-default").text
    assert "Default (bm_george)" in page
    assert '<option value="1.2" selected>' in page


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


def test_adding_something_does_not_start_a_build(client, conn):
    """Adding text and deciding how to read it are two jobs, not one form."""
    response = client.post(
        "/api/ingest",
        data={"kind": "text", "title": "Just added", "text": "A note.\n\nWith two paragraphs in it."},
    )

    assert response.status_code == 200
    article_id = response.json()["id"]
    assert response.json()["job"] is None
    assert db.active_jobs(conn) == [], "nothing was queued"
    assert db.get_article(article_id, conn)["status"] == "new"


def test_a_new_article_offers_the_build_above_its_text(client, conn, monkeypatch):
    monkeypatch.setattr(web, "_voices", lambda: [])
    client.post("/api/ingest", data={"kind": "text", "title": "Fresh", "text": "A note.\n\nTwo paragraphs."})

    body = client.get("/a/fresh").text
    doc = body.index('class="doc"')

    assert body.index("Modify article") < body.index("Build the audio") < doc, \
        "summarise or re-parse first, then build, both before the text"
    assert "sections and" in body or "section and" in body, "it says what the parser found"
    assert "Summarise" in body[:doc]


def test_an_article_that_already_has_summaries_is_not_offered_them_again(client, conn, monkeypatch):
    """And the modify card goes back below the text, leaving only the build."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda: [])
    doc = Article(title="Already done", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.SUMMARY, text="In short, this."),
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/already-done").text
    text = body.index('class="doc"')

    assert "Summarise" not in body[:text], "it already has one"
    assert body.index("Build the audio") < text, "the build is still pending"
    assert body.index("Modify article") > text, "so modify goes back to the bottom"


def test_the_two_dark_palettes_stay_identical():
    """Dark is defined twice: once for the system preference and once for the
    switch. They must carry the same tokens, or a colour set in one place is
    missing when you get there the other way."""
    import re
    from pathlib import Path

    css = Path("src/textcast/web/static/app.css").read_text(encoding="utf-8")

    def tokens(selector: str) -> dict[str, str]:
        start = css.index(selector)
        body = css[css.index("{", start) + 1 : css.index("}", start)]
        return {k: v.strip() for k, v in re.findall(r"(--[\w-]+):\s*([^;]+);", body)}

    by_media = tokens(':root:not([data-theme="light"])')
    by_switch = tokens(':root[data-theme="dark"]')

    assert by_media, "the media-query dark palette moved"
    assert by_media == by_switch, (
        f"only in one: {set(by_media) ^ set(by_switch)}; "
        f"differing: {[k for k in by_media if by_media[k] != by_switch.get(k)]}"
    )


def test_the_author_can_be_corrected_by_hand(client, conn, monkeypatch):
    """A publication puts a byline in its head; a pasted note has nowhere to
    find one, so the field is editable either way."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda: [])
    doc = Article(title="Anonymous note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    assert db.get_article(article_id, conn)["author"] == ""

    client.post(f"/api/articles/{article_id}/tags",
                data={"tags": "Notes", "author": "  Matt Levine  "})

    assert db.get_article(article_id, conn)["author"] == "Matt Levine"
    assert db.tags_for(article_id, conn) == ["Notes"]
    assert 'value="Matt Levine"' in client.get("/a/anonymous-note").text


def test_tags_can_still_be_saved_without_touching_the_author(client, conn):
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Keeps its byline", author="George Hammond",
                  sections=[Section(title="One", blocks=[
                      Block(kind=BlockKind.PARA, text="The body of it."),
                  ])]).renumber()
    article_id = db.save_article(doc, conn)

    client.post(f"/api/articles/{article_id}/tags", data={"tags": "Reading"})

    assert db.get_article(article_id, conn)["author"] == "George Hammond"


def test_the_library_row_reads_star_title_length_status_then_who(client, conn):
    """Every status shows, including ready, each in its own colour."""
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="A starred piece", author="Matt Levine", source="Bloomberg",
                  sections=[Section(title="One", blocks=[
                      Block(kind=BlockKind.PARA, text="The body of it."),
                  ])]).renumber()
    article_id = db.save_article(doc, conn)
    db.set_tags(article_id, ["Money Stuff"], conn)
    db.set_flag(article_id, "starred", True, conn)

    page = client.get("/").text
    # The tag filter names every tag too, so take the row itself, not the page.
    start = page.index('href="/a/a-starred-piece"')
    row = page[start : page.index("</a>", start)]

    assert '<span class="star"' in row and row.index("star") < row.index("A starred piece")
    assert '<span class="badge new">new</span>' in row, "ready is not the only status shown"
    order = [row.index(x) for x in ("Matt Levine", "Bloomberg", "words", "Money Stuff")]
    assert order == sorted(order), "author, publication, word count, then tags"


def test_a_batch_upload_keeps_going_past_a_file_it_cannot_read(client, conn):
    """The promise of a batch is that one bad file does not cost the rest."""
    good = ("note.md", b"# A note\n\nA paragraph of prose, long enough to parse.\n", "text/markdown")
    bad = ("broken.pdf", b"not a pdf at all", "application/pdf")
    other = ("second.md", b"# Another\n\nA different paragraph, also long enough.\n", "text/markdown")

    response = client.post(
        "/api/ingest",
        data={"kind": "file"},
        files=[("files", good), ("files", bad), ("files", other)],
    )

    body = response.json()
    assert response.status_code == 200
    assert sorted(body["added"]) == ["a-note", "another"]
    assert len(body["failed"]) == 1 and "broken.pdf" in body["failed"][0]


def test_one_unreadable_file_is_a_bad_request_not_a_crash(client, conn):
    response = client.post(
        "/api/ingest",
        data={"kind": "file"},
        files={"files": ("broken.pdf", b"not a pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert "could not be read" in response.json()["error"]
