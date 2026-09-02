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
    monkeypatch.setattr(web, "_voices", lambda *a: [])
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

    monkeypatch.setattr(web, "_voices", lambda *a: [])
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

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="A hinted note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/a-hinted-note").text

    # Two always, and a third by the engine picker when more than one engine
    # is installed — which depends on the machine, so this counts the floor.
    assert body.count('<span class="tip">') >= 2, "one by quote voice, one by the actions"
    assert "Start quote" in body
    # Hover and focus, not click: a details element took two taps on a phone.
    assert "<details" not in body
    # A label may not contain interactive content of its own.
    assert '<label class="titled"' not in body


def test_the_reader_offers_star_and_archive_above_the_article(client, conn, monkeypatch):
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="A note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/a-note").text
    head = body[: body.index('class="doc"')]

    assert 'aria-pressed="false"' in head, "star and archive sit in the header"
    assert head.count('/flag') == 2
    assert "Modify article" in body


def test_the_reader_polls_while_a_summary_runs(client, conn, monkeypatch):
    """It keyed off article.status, which a summary never touches, so the
    progress bar sat at zero until the page was reloaded by hand."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="Running", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    db.enqueue(article_id, kind="summarise", conn=conn)

    body = client.get("/a/running").text

    assert "progress.js" in body
    assert "Writing summaries…" in body
    assert 'id="build-meter"' in body, "the poller needs something to paint"


def test_every_page_carries_a_home_button(client):
    """The wordmark went home and nothing said so."""
    body = client.get("/summaries").text
    head = body[: body.index("</header>")]

    assert 'class="home"' in head
    assert head.count('href="/"') == 2, "the wordmark and the house, both home"


def test_the_model_field_is_typed_and_offers_no_menu(client):
    """A datalist drew a dropdown arrow on the field with nothing behind it
    until a provider was picked: a control that promises a menu and has none."""
    body = client.get("/summaries").text

    assert "<datalist" not in body
    assert 'list="model-suggestions"' not in body
    assert 'name="model"' in body, "the field itself stays, typed by hand"


def test_a_failed_summary_says_so_on_the_article(client, conn, monkeypatch):
    """It only reached the worker's log. A summary leaves article.status alone,
    and the card was tied to that, so the page showed nothing at all."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="Half done", sections=[
        Section(title="One", blocks=[
            Block(kind=BlockKind.SUMMARY, text="In short."),
            Block(kind=BlockKind.PARA, text="The body of it."),
        ]),
        Section(title="Two", blocks=[Block(kind=BlockKind.PARA, text="The rest of it.")]),
    ]).renumber()
    article_id = db.save_article(doc, conn)
    job_id = db.enqueue(article_id, kind="summarise", conn=conn)
    db.update_job(job_id, conn, state="failed", error="1 of 2 sections summarised, 1 failed. Two: 429")

    body = client.get("/a/half-done").text

    assert "Some summaries did not arrive" in body
    assert "429" in body
    assert "Summarise the other 1" in body, "and a way to ask for only what is missing"


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
    # The label names its engine when there is more than one to choose, and
    # whether there is depends on the machine — so match the stem.
    assert "Default (bm_george" in page
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
    monkeypatch.setattr(web, "_voices", lambda *a: [])
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

    monkeypatch.setattr(web, "_voices", lambda *a: [])
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


def test_a_rule_can_be_added_with_any_of_its_three_replacements(client, conn):
    """Plain text, misaki IPA, espeak IPA. All optional, one required — and
    the IPA flag follows the fields rather than a checkbox beside them."""
    from textcast import db

    client.post("/pronunciations/add", data={
        "kind": "word", "pattern": "Onlyspeak", "replacement": "",
        "replacement_espeak": "ˈoʊnli", "note": "espeak only",
    })
    client.post("/pronunciations/add", data={
        "kind": "word", "pattern": "Bothways", "replacement": "both ways",
        "replacement_misaki": "bˈOθ", "note": "a respelling and phonemes",
    })

    rules = {r.pattern: r for r in db.list_pronunciations(conn)}
    assert rules["Onlyspeak"].espeak == "ˈoʊnli"
    assert rules["Onlyspeak"].is_phonemes is True
    assert rules["Onlyspeak"].fires_for("misaki") is False, "nothing to say there"
    assert rules["Bothways"].fires_for("espeak") is True, "it falls back to the text"


def test_a_rule_with_nothing_to_say_is_refused(client, conn):
    body = client.post("/pronunciations/add", data={
        "kind": "word", "pattern": "Empty", "replacement": "",
    }).text

    assert "something to say" in body


def test_the_voice_picker_never_offers_one_value_twice(client, conn, monkeypatch):
    """Both engines carry `af_heart`. Rendering both engines' options into one
    select put the same value in it twice, and nothing selecting by value
    could tell them apart — so the page carries one engine's list and a
    payload, and rebuilds it when the engine changes."""
    import json as _json
    import re

    from textcast.document import Article, Block, BlockKind, Section
    from textcast.tts import catalogue

    # The fixture empties the voice list; this is the one test about it.
    # `catalogue` reads a table, so it still loads no model.
    monkeypatch.setattr(web, "_voices", lambda engine=None: list(catalogue(engine or "kokoro")))
    doc = Article(title="Picker", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/picker").text
    select = body[body.index('<select name="voice"'):]
    select = select[: select.index("</select>")]
    values = re.findall(r'<option value="([^"]*)"', select)

    assert len(values) == len(set(values)), f"repeated: {values}"

    payload = re.search(r'id="engine-voices"[^>]*>(.*?)</script>', body, re.S)
    if payload:  # only rendered when a second engine is installed
        by_engine = _json.loads(payload.group(1))
        assert len(by_engine) > 1
        # One list per engine, so the payload is what keeps them apart — not
        # a label on every voice.
        for voices in by_engine.values():
            ids = [v["id"] for v in voices]
            assert len(ids) == len(set(ids))


def test_every_control_in_the_bar_is_one_height():
    """They were 32, 29 and 26 px standing on the same row, which reads as
    three sizes of nothing in particular. The search field set the height."""
    from pathlib import Path

    css = Path("src/textcast/web/static/app.css").read_text(encoding="utf-8")

    for selector in (".find input", ".home", ".theme-switch", "button.sm, .btn.sm", ".nav-toggle"):
        body = css.split(selector + " {", 1)[1].split("}", 1)[0]
        assert "var(--bar-control)" in body, f"{selector} sets its own height"


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

    monkeypatch.setattr(web, "_voices", lambda *a: [])
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


def test_the_text_can_be_edited_by_hand(client, conn, monkeypatch):
    """The parser keeps things it should not, and gets things wrong. Ids do not
    move, so the audio and its timings stay valid — only out of date."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="Needs a fix", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="Frist paragrpah with typos."),
        Block(kind=BlockKind.PARA, text="A line the page should not have kept."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)

    assert 'name="text:b0-0"' in client.get("/a/needs-a-fix?edit=1").text

    response = client.post(
        f"/api/articles/{article_id}/blocks",
        data={"text:b0-0": "First paragraph, spelled properly.", "kind:b0-0": "quote",
              "text:b0-1": "A line the page should not have kept."},
    )

    assert response.json()["changed"] == 1, "only the block that differs"
    after = db.load_article(article_id, conn)
    assert after.sections[0].blocks[0].text == "First paragraph, spelled properly."
    assert after.sections[0].blocks[0].kind is BlockKind.QUOTE
    assert [b.id for _s, b in after.blocks()] == ["b0-0", "b0-1"], "ids do not move"
    assert [h["block_id"] for h in db.search("spelled properly", conn) if h["kind"] != "article"] == ["b0-0"]


def test_an_edit_that_empties_a_block_is_ignored(client, conn):
    """Blank would leave the audio with nothing to say and the id still there."""
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Keep something", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The only line."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)

    client.post(f"/api/articles/{article_id}/blocks", data={"text:b0-0": "   "})

    assert db.load_article(article_id, conn).sections[0].blocks[0].text == "The only line."


def test_a_block_can_be_removed_outright(client, conn, monkeypatch):
    """The parser keeps bylines, datelines and subscription pitches. Removing
    one moves every id after it, so the audio has to go with it."""
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="Has junk in it", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="Sign up for our newsletter here."),
        Block(kind=BlockKind.PARA, text="The first real paragraph of the piece."),
        Block(kind=BlockKind.PARA, text="The second real paragraph of the piece."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    conn.execute("UPDATE article SET status='ready', audio_ms=9000 WHERE id=?", (article_id,))
    conn.execute("UPDATE block SET start_ms=0 WHERE article_id=?", (article_id,))

    assert 'name="remove:b0-0"' in client.get("/a/has-junk-in-it?edit=1").text

    response = client.post(
        f"/api/articles/{article_id}/blocks",
        data={
            "remove:b0-0": "1",
            "text:b0-0": "Sign up for our newsletter here.",
            "text:b0-1": "The first real paragraph of the piece.",
            "text:b0-2": "The second real paragraph of the piece.",
        },
    )

    assert response.json() == {"changed": 2, "removed": 1}
    after = db.load_article(article_id, conn)
    assert [b.text for _s, b in after.blocks()] == [
        "The first real paragraph of the piece.",
        "The second real paragraph of the piece.",
    ]
    assert [b.id for _s, b in after.blocks()] == ["b0-0", "b0-1"], "the ids close up"
    row = db.get_article(article_id, conn)
    assert (row["status"], row["audio_ms"]) == ("new", 0), "the audio no longer describes this"
    assert conn.execute(
        "SELECT COUNT(*) c FROM block WHERE article_id=? AND start_ms IS NOT NULL", (article_id,)
    ).fetchone()["c"] == 0


def test_removing_every_block_is_refused(client, conn):
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="All of it", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The only line there is."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)

    response = client.post(f"/api/articles/{article_id}/blocks", data={"remove:b0-0": "1"})

    assert response.status_code == 400
    assert "nothing in it" in response.json()["detail"]
    assert db.load_article(article_id, conn).sections[0].blocks[0].text == "The only line there is."


def test_removing_a_block_keeps_the_cache_for_the_rebuild(conn, settings):
    """It is keyed by the text, so every surviving block is still a hit."""
    from textcast.service import cached_renders, edit_blocks, ingest

    stored = ingest(text="# One\n\nFirst paragraph here.\n\nSecond paragraph here.\n",
                    title="Cached", build=False)
    for path in cached_renders(stored.article_id, conn, settings):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 8)
    before = len(list(settings.cache_dir.glob("*.f32")))

    edit_blocks(stored.article_id, {}, {"b0-1"})

    assert len(list(settings.cache_dir.glob("*.f32"))) == before, "nothing goes back to the model"


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def _exportable(conn, settings):
    """An article with a stored original, a summary and built audio beside it."""
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="A Drug-Trial Stock Sale", author="Matt Levine", source="Bloomberg",
                  sections=[Section(title="INMB", blocks=[
                      Block(kind=BlockKind.SUMMARY, text="What the section says, shorter."),
                      Block(kind=BlockKind.PARA, text="The body of it."),
                  ])]).renumber()
    article_id = db.save_article(doc, conn)
    slug = db.get_article(article_id, conn)["slug"]
    (settings.source_dir / f"{slug}.html").write_bytes(b"<html>the original</html>")
    media = settings.media_dir / slug
    media.mkdir(parents=True, exist_ok=True)
    (media / "section-000.opus").write_bytes(b"OggS")
    (media / "section-000.vtt").write_text("WEBVTT\n")
    return article_id, slug


def _zip_names(response) -> list[str]:
    import io
    import zipfile

    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content)).namelist()


def test_each_export_is_named_for_the_article_not_its_slug(client, conn, settings):
    """A zip is read by a person, and the library shows titles."""
    _exportable(conn, settings)

    assert _zip_names(client.get("/api/export/sources.zip")) == ["A Drug-Trial Stock Sale.html"]
    assert _zip_names(client.get("/api/export/text.zip")) == ["A Drug-Trial Stock Sale.md"]
    assert _zip_names(client.get("/api/export/audio.zip")) == [
        "A Drug-Trial Stock Sale/section-000.opus",
        "A Drug-Trial Stock Sale/section-000.vtt",
    ]


def test_the_text_export_carries_the_summaries(client, conn, settings):
    """A summary is a block, so it comes out wherever it sits."""
    import io
    import zipfile

    _exportable(conn, settings)

    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export/text.zip").content))
    text = archive.read("A Drug-Trial Stock Sale.md").decode()

    assert "# A Drug-Trial Stock Sale" in text
    assert "Matt Levine · Bloomberg" in text
    assert text.index("What the section says") < text.index("The body of it.")


def test_an_export_with_nothing_in_it_is_a_404_not_an_empty_zip(client, conn):
    """A zip of nothing downloads and opens to nothing, which reads as a bug."""
    assert client.get("/api/export/audio.zip").status_code == 404


def test_a_source_left_behind_by_a_deleted_article_is_not_exported(client, conn, settings):
    """Its name would have to come from its slug, and nothing links it back."""
    _exportable(conn, settings)
    (settings.source_dir / "gone.html").write_bytes(b"<html>orphan</html>")

    assert _zip_names(client.get("/api/export/sources.zip")) == ["A Drug-Trial Stock Sale.html"]


def test_the_library_offers_all_three_exports_with_their_sizes(client, conn, settings):
    _exportable(conn, settings)

    page = client.get("/").text

    assert "/api/export/sources.zip" in page
    assert "/api/export/text.zip" in page
    assert "/api/export/audio.zip" in page


def test_markdown_export_keeps_each_kind_of_block_distinct():
    """The export is read by a person, so a quote must not read as a paragraph."""
    from textcast.document import Article, Block, BlockKind, Section, to_markdown

    doc = Article(title="A piece", subtitle="A standfirst", author="Matt Levine",
                  source="Bloomberg", url="https://example.com/x",
                  sections=[Section(title="One", blocks=[
                      Block(kind=BlockKind.SUMMARY, text="The gist."),
                      Block(kind=BlockKind.HEADING, text="A heading"),
                      Block(kind=BlockKind.PARA, text="A paragraph."),
                      Block(kind=BlockKind.QUOTE, text="A quotation."),
                      Block(kind=BlockKind.LIST_ITEM, text="An item."),
                      Block(kind=BlockKind.FOOTNOTE, text="An aside.", footnote_ref="1"),
                  ])]).renumber()

    text = to_markdown(doc)

    assert text.startswith("# A piece\n\n*A standfirst*\n\nMatt Levine · Bloomberg")
    assert "<https://example.com/x>" in text
    assert "## One" in text and "### A heading" in text
    assert "**Summary.** The gist." in text
    assert "> A quotation." in text
    assert "- An item." in text
    assert "[1] An aside." in text
    assert text.endswith("\n")
