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


PASSWORD = "open-sesame"


def sign_in_required(settings, password: str = PASSWORD, username: str = "reader"):
    """Turn access control on and give the library its one account.

    Returns the account, because the tests need the two secrets on it: the
    session the cookie should end up holding, and the ingest key the
    bookmarklet carries.
    """
    from textcast import accounts

    settings.require_auth = True
    settings.username = username
    settings.auth_token = password
    conn = db.connect(settings.db_path)
    return accounts.get(conn) or accounts.create(conn, username, password)


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


def test_a_phone_can_find_an_icon(client):
    """An SVG was the only icon, and a phone has no use for one.

    iOS ignores an SVG `apple-touch-icon` entirely, and every browser asks for
    /favicon.ico whatever the page's link tags say — that path answered with
    the 404 page, so mobile drew nothing at all.
    """
    for path, kind in (
        ("/favicon.ico", "image/x-icon"),
        ("/apple-touch-icon.png", "image/png"),
        ("/apple-touch-icon-precomposed.png", "image/png"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"] == kind, path
        assert response.content, path


def test_the_manifest_offers_a_raster_icon_and_a_padded_maskable_one(client):
    """Chrome on Android will not install an app whose only icon is an SVG,
    and it read "any maskable" on a square that draws to its own edge as
    licence to crop the headband off."""
    icons = client.get("/manifest.webmanifest").json()["icons"]

    png = [i for i in icons if i["type"] == "image/png"]
    assert {i["sizes"] for i in png} >= {"192x192", "512x512"}

    maskable = [i for i in icons if "maskable" in i["purpose"]]
    assert len(maskable) == 1
    assert maskable[0]["purpose"] == "maskable", "the mark is cropped if it is also 'any'"
    assert "maskable" in maskable[0]["src"], "the maskable icon needs its own padded art"

    for icon in icons:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_without_auth_the_library_is_simply_open(client):
    assert client.get("/").status_code == 200


def test_a_browser_is_sent_to_sign_in_and_an_api_call_is_refused(client, settings):
    sign_in_required(settings)

    page = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    api = client.get("/api/tags", headers={"accept": "application/json"})

    assert page.status_code == 303
    assert page.headers["location"].startswith("/login?next=")
    assert api.status_code == 401


def test_health_stays_open_but_says_nothing_about_the_deployment(client, settings):
    """Orchestration and monitoring reach this unauthenticated by
    convention, so it must stay open on a locked-down instance too -- what
    it must not do is say what engine is in use or how big the library is
    to whoever asks, which it used to, to anyone."""
    body = client.get("/health").json()
    assert body == {"ok": True}

    sign_in_required(settings)
    locked_down = client.get("/health", headers={"accept": "application/json"})
    assert locked_down.status_code == 200
    assert locked_down.json() == {"ok": True}


def test_signing_in_opens_the_library_and_signing_out_closes_it(client, settings):
    sign_in_required(settings)
    browser = {"accept": "text/html"}

    client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert client.get("/", headers=browser).status_code == 200

    client.post("/logout", follow_redirects=False)
    assert client.get("/", headers=browser, follow_redirects=False).status_code == 303


def test_a_wrong_password_is_rejected_without_setting_a_cookie(client, settings):
    sign_in_required(settings)

    response = client.post("/login", data={"username": "reader", "password": "guess", "next": "/"})

    assert "do not match" in response.text
    assert web.COOKIE not in client.cookies


def test_a_wrong_username_reads_exactly_like_a_wrong_password(client, settings):
    """Or the two can be told apart by trying, which halves the guessing."""
    sign_in_required(settings)

    wrong_name = client.post("/login", data={"username": "nobody", "password": PASSWORD})
    wrong_word = client.post("/login", data={"username": "reader", "password": "guess"})

    assert "do not match" in wrong_name.text
    assert wrong_name.text == wrong_word.text


def test_guessing_the_password_stops_being_free(client, settings):
    """/login grants the whole account, and only scrypt's own cost stood
    between an internet-facing instance and unlimited guessing before."""
    from textcast.web import limits

    sign_in_required(settings)
    limits.reset_all()

    codes = []
    for _ in range(limits.LOGIN_ATTEMPTS.allowed + 3):
        codes.append(
            client.post("/login", data={"username": "reader", "password": "guess"}).status_code
        )

    assert codes[0] == 200, "a wrong password still just shows the form"
    assert 429 in codes, "guessing was never refused"
    assert codes.count(200) == limits.LOGIN_ATTEMPTS.allowed


def test_a_correct_sign_in_does_not_spend_the_failure_budget(client, settings):
    from textcast.web import limits

    sign_in_required(settings)
    limits.reset_all()

    client.post("/login", data={"username": "reader", "password": "guess"})
    client.post("/login", data={"username": "reader", "password": PASSWORD})

    assert limits.LOGIN_ATTEMPTS.check("testclient") == 0.0


def test_a_wrong_username_still_pays_the_scrypt_cost(client, settings, monkeypatch):
    """`or` used to short-circuit on the username, so a wrong username came
    back in a plain string compare and a wrong password (right username)
    came back after scrypt -- a timing oracle for the username alone."""
    from textcast import accounts

    sign_in_required(settings)
    calls = []
    real_verify = accounts.verify_password

    def spy(password, stored):
        calls.append(stored)
        return real_verify(password, stored)

    monkeypatch.setattr(accounts, "verify_password", spy)

    client.post("/login", data={"username": "nobody", "password": "guess"})

    assert calls, "verify_password was never reached for a wrong username"


def test_the_cookie_carries_a_session_and_never_the_password(client, settings):
    """It used to carry the credential itself, on every request."""
    account = sign_in_required(settings)

    client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )

    assert client.cookies[web.COOKIE] == account.session
    assert PASSWORD not in client.cookies[web.COOKIE]


def test_changing_the_password_signs_every_other_browser_out(client, settings):
    from textcast import accounts

    sign_in_required(settings)
    client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200

    accounts.set_password(db.connect(settings.db_path), "a-longer-secret")

    assert client.get(
        "/", headers={"accept": "text/html"}, follow_redirects=False
    ).status_code == 303


@pytest.mark.parametrize("bait", ["//evil.example.com/", "/\\evil.example.com/", "/\\/evil.example.com/"])
def test_sign_in_never_redirects_off_this_host(client, settings, bait):
    sign_in_required(settings)

    response = client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": bait},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_the_login_page_says_so_when_no_account_has_been_seeded(client, settings):
    settings.require_auth = True
    settings.auth_token = ""

    assert "No account has been set up" in client.get("/login").text


def test_the_summaries_page_says_when_nothing_is_configured(client):
    body = client.get("/summaries").text

    assert "not configured" in body
    assert "generativelanguage.googleapis.com" in body, "an endpoint is offered, not demanded"


def test_choosing_a_model_cannot_touch_a_key(client, conn):
    """Two boxes, two routes. Saving a model must not be able to wipe a key."""
    from textcast import summarize

    summarize.save_credential("G", provider="gemini", api_key="secret", conn=conn)
    client.post("/summaries", data={"credential": "G"})

    client.post("/summaries", data={"credential": "G", "model": "a-model"})

    assert summarize.config(conn).api_key == "secret"
    assert summarize.config(conn).model == "a-model"


def test_two_keys_for_one_provider_are_both_kept(client, conn):
    """The reason a key is named. Two accounts with Gemini are two keys."""
    from textcast import summarize

    client.post("/summaries/key", data={
        "name": "Work Gemini", "provider": "gemini", "api_key": "the-work-key",
    })
    client.post("/summaries/key", data={
        "name": "Home Gemini", "provider": "gemini", "api_key": "the-home-key",
    })

    assert summarize.credential("Work Gemini", conn).api_key == "the-work-key"
    assert summarize.credential("Home Gemini", conn).api_key == "the-home-key"


def test_storing_a_key_does_not_start_using_it(client, conn):
    """Typing a Groq key is not a decision to summarise with Groq."""
    from textcast import summarize

    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)
    client.post("/summaries", data={"credential": "G", "model": "gemini-2.5-flash"})

    client.post("/summaries/key", data={
        "name": "Groq", "provider": "groq", "api_key": "groq-key",
    })

    assert summarize.config(conn).credential == "G", "still the key that was chosen"
    assert summarize.credential("Groq", conn).api_key == "groq-key"


def test_a_key_with_no_name_is_refused_and_says_so(client, conn):
    from textcast import summarize

    body = client.post("/summaries/key", data={"provider": "gemini", "api_key": "k"}).text

    assert "needs a name" in body
    assert summarize.credentials(conn) == []


def test_a_local_provider_is_chosen_by_name_like_any_other(client, conn):
    """Ollama needs no key, and still needs naming to be chosen."""
    from textcast import summarize

    client.post("/summaries/key", data={"name": "Ollama", "provider": "ollama", "api_key": ""})
    client.post("/summaries", data={"credential": "Ollama", "model": "llama3.2"})

    cfg = summarize.config(conn)
    assert cfg.api_key == "" and cfg.base_url == "http://127.0.0.1:11434/v1/"
    assert cfg.ready is True


def test_the_summaries_page_never_carries_a_whole_key(client, conn):
    """It shows the tail so two keys can be told apart. A key itself in the
    page would be readable by anyone at it."""
    from textcast import summarize

    summarize.save_credential("D", provider="deepseek", api_key="sk-secret-12345678", conn=conn)

    body = client.get("/summaries").text

    assert "sk-secret-12345678" not in body
    assert "5678" in body, "the tail identifies it without revealing it"
    assert "DeepSeek" in body


def test_forgetting_one_key_leaves_the_others(client, conn):
    from textcast import summarize

    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)
    summarize.save_credential("D", provider="deepseek", api_key="deepseek-key", conn=conn)

    client.post("/summaries/forget-key", data={"name": "D"})

    assert summarize.credential("D", conn) is None
    assert summarize.credential("G", conn).api_key == "gemini-key"


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


def test_a_finished_article_wears_a_completed_badge(client, conn):
    """Playback reaching the end is not an article status. The badge is derived
    from the saved position, and the article row still says ready."""
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Heard it all", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    conn.execute("UPDATE article SET status='ready', audio_ms=9000 WHERE id=?", (article_id,))

    # "Continue listening" links the same article above the library, so the
    # library row is the last link to it, not the first.
    def library_row(page: str) -> str:
        row = page[page.rindex('href="/a/heard-it-all"') :]
        return row[: row.index("</a>")]

    # Part heard, and well short of the end: a position at the end counts as
    # the end whatever the flag says.
    db.save_position(article_id, section_idx=0, ms=6000, finished=False, conn=conn)
    assert '<span class="badge ready">ready</span>' in library_row(client.get("/").text)

    db.save_position(article_id, section_idx=0, ms=9000, finished=True, conn=conn)
    row = library_row(client.get("/").text)
    assert '<span class="badge completed">completed</span>' in row
    assert "badge ready" not in row, "one badge, not two"
    assert db.get_article(article_id, conn)["status"] == "ready"


def test_the_status_filter_offers_completed_and_finds_it(client, conn):
    from textcast.document import Article, Block, BlockKind, Section

    for title in ("Heard it all", "Half heard"):
        doc = Article(title=title, sections=[Section(title="One", blocks=[
            Block(kind=BlockKind.PARA, text=f"The body of {title}."),
        ])]).renumber()
        article_id = db.save_article(doc, conn)
        conn.execute("UPDATE article SET status='ready', audio_ms=9000 WHERE id=?", (article_id,))
        heard_it_all = title == "Heard it all"
        db.save_position(article_id, section_idx=0, ms=9000 if heard_it_all else 6000,
                         finished=heard_it_all, conn=conn)

    assert '<option value="completed"' in client.get("/").text

    page = client.get("/?status=completed").text
    assert 'href="/a/heard-it-all"' in page
    assert 'href="/a/half-heard"' not in page


def test_stopping_an_article_clears_its_saved_position(client, conn):
    """The player's stop button. The row goes, so the article leaves
    "Continue listening" and the reader stops resuming it."""
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Enough of that", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    conn.execute("UPDATE article SET status='ready', audio_ms=9000 WHERE id=?", (article_id,))
    db.save_position(article_id, section_idx=0, ms=6000, conn=conn)

    response = client.post(f"/api/articles/{article_id}/position/clear")

    assert response.status_code == 204
    assert db.get_position(article_id, conn) is None
    assert "Continue listening" not in client.get("/").text


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


def test_an_oversized_upload_is_refused_before_it_is_parsed(client, conn, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_MAX", 10)
    response = client.post(
        "/api/ingest",
        data={"kind": "file"},
        files={"files": ("note.md", b"# far more than ten bytes of markdown", "text/markdown")},
    )

    assert response.status_code == 400
    assert "40 MB" in response.json()["error"]


def test_an_oversized_file_in_a_batch_is_skipped_not_a_crash(client, conn, monkeypatch):
    good_body = b"# A note\n\nA paragraph of prose, long enough to parse.\n"
    monkeypatch.setattr(web, "UPLOAD_MAX", len(good_body) + 10)
    good = ("note.md", good_body, "text/markdown")
    big = ("big.md", good_body + b"x" * 1000, "text/markdown")

    response = client.post(
        "/api/ingest",
        data={"kind": "file"},
        files=[("files", good), ("files", big)],
    )

    body = response.json()
    assert response.status_code == 200
    assert body["added"] == ["a-note"]
    assert len(body["failed"]) == 1 and "big.md" in body["failed"][0]


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


def test_two_articles_with_one_title_do_not_collide_in_a_zip(client, conn, settings):
    """A newsletter that names every issue the same is the ordinary case.

    Two entries of one name in a zip hand the reader whichever the archiver
    picks, and the other is silently lost. The slug is what tells two articles
    apart, so the second one carries it.
    """
    from textcast.document import Article, Block, BlockKind, Section

    _exportable(conn, settings)
    for body in ("A second issue, same name.", "A third issue, same name."):
        doc = Article(title="A Drug-Trial Stock Sale", source="Bloomberg",
                      sections=[Section(title="INMB", blocks=[
                          Block(kind=BlockKind.PARA, text=body),
                      ])]).renumber()
        article_id = db.save_article(doc, conn)
        slug = db.get_article(article_id, conn)["slug"]
        (settings.source_dir / f"{slug}.html").write_bytes(b"<html>another</html>")

    for route in ("/api/export/sources.zip", "/api/export/text.zip"):
        names = _zip_names(client.get(route))
        assert len(names) == 3, route
        assert len(set(names)) == 3, f"{route} lost an article to a duplicate name: {names}"
        assert "A Drug-Trial Stock Sale.html" in names or \
               "A Drug-Trial Stock Sale.md" in names, "the first one keeps the plain title"


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


# --------------------------------------------------------------------------
# paging and order
# --------------------------------------------------------------------------


def _library_of(conn, n: int, **columns) -> list[int]:
    """``n`` articles, added one second apart so their order is not a tie."""
    from textcast.document import Article, Block, BlockKind, Section

    ids = []
    for i in range(n):
        doc = Article(title=f"Article {i:02d}", sections=[Section(title="One", blocks=[
            Block(kind=BlockKind.PARA, text=f"The body of number {i}."),
        ])]).renumber()
        article_id = db.save_article(doc, conn)
        conn.execute(
            "UPDATE article SET added_at = ? WHERE id = ?",
            (f"2026-01-01T00:00:{i:02d}+00:00", article_id),
        )
        ids.append(article_id)
    return ids


def _titles_on(page: str) -> list[str]:
    import re as _re

    return _re.findall(r'<span>(Article \d\d)</span>', page)


def test_the_library_shows_twenty_five_and_pages_the_rest(client, conn):
    """The page used to grow without limit and then stop at 200, silently."""
    _library_of(conn, 30)

    first = client.get("/").text
    second = client.get("/?page=2").text

    assert len(_titles_on(first)) == 25
    assert len(_titles_on(second)) == 5
    assert "Page 1 of 2" in first and "Page 2 of 2" in second
    assert set(_titles_on(first)) & set(_titles_on(second)) == set(), "a row is on one page only"


def test_the_library_is_ordered_by_when_it_was_added(client, conn):
    """Newest arrival first, whatever date the publication put on it."""
    _library_of(conn, 3)
    conn.execute("UPDATE article SET published_at = '2019-01-01' WHERE title = 'Article 02'")

    assert _titles_on(client.get("/").text) == ["Article 02", "Article 01", "Article 00"]


def test_the_pager_carries_the_filter_it_was_opened_with(client, conn):
    """Paging away from a filtered library used to hand back the whole thing."""
    ids = _library_of(conn, 30)
    for article_id in ids:
        db.set_tags(article_id, ["Money Stuff"], conn)

    page = client.get("/?tag=Money+Stuff").text

    # Jinja escapes the separator, which is correct HTML and parses the same.
    assert "tag=Money+Stuff&amp;page=2" in page


def test_a_page_number_past_the_end_shows_the_last_page(client, conn):
    """A bookmark outlives the filter that made it, and must not show nothing."""
    _library_of(conn, 30)

    page = client.get("/?page=99").text

    assert "Page 2 of 2" in page
    assert len(_titles_on(page)) == 5


def test_the_pager_is_there_at_one_page_of_one(client, conn):
    """A control that appears only sometimes is one you have to look for."""
    _library_of(conn, 5)

    page = client.get("/").text

    assert 'class="pager"' in page
    assert "Page 1 of 1" in page
    assert page.count('class="btn ghost sm narrow off"') == 2, "both ends are spent"


def test_continue_listening_belongs_to_the_library_not_to_a_page_of_it(client, conn):
    """It is a shortcut back in, and repeating it on every page is noise."""
    ids = _library_of(conn, 30)
    conn.execute("UPDATE article SET audio_ms = 60000, status = 'ready' WHERE id = ?", (ids[-1],))
    db.save_position(ids[-1], 0, 30_000, False, conn)

    assert "Continue listening" in client.get("/").text
    assert "Continue listening" not in client.get("/?page=2").text


def test_the_count_names_the_range_and_the_whole(client, conn):
    _library_of(conn, 30)

    assert "26–30 of 30" in client.get("/?page=2").text


# --------------------------------------------------------------------------
# messages you can put away
# --------------------------------------------------------------------------


def _article_with_job(conn, state: str, kind: str = "summarise") -> tuple[int, str, int]:
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Half done", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    job_id = db.enqueue(article_id, kind, conn=conn)
    db.update_job(job_id, conn, state=state, error="429 rate limited")
    return article_id, db.get_article(article_id, conn)["slug"], job_id


def _status_card(page: str) -> str:
    """The opening tag of the build card. The script that wires the cross names
    the same attributes, so a search over the whole page always finds them."""
    start = page.index('id="build-status"')
    return page[page.rindex("<div", 0, start) : page.index(">", start) + 1]


def test_a_failed_job_card_can_be_put_away(client, conn):
    """A build stays failed, so the message stayed until something replaced it."""
    _article_id, slug, job_id = _article_with_job(conn, "failed")

    card = _status_card(client.get(f"/a/{slug}").text)

    assert "data-dismissible" in card
    assert f'data-dismiss-key="job-{job_id}"' in card, "keyed by the job, so the next failure shows"


def test_a_running_job_card_cannot_be_put_away(client, conn):
    """It is about to change, and hiding it would hide the progress with it."""
    _article_id, slug, _job_id = _article_with_job(conn, "running")

    page = client.get(f"/a/{slug}").text

    assert "Writing summaries…" in page
    assert "data-dismissible" not in _status_card(page)


def _article_with_summary(conn):
    """An article carrying a summary block, which is all a hand-written one is."""
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Summarised once", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.SUMMARY, text="What it is about, briefly."),
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    return article_id, db.get_article(article_id, conn)["slug"]


def test_the_reader_says_which_model_wrote_the_summaries(client, conn):
    article_id, slug = _article_with_summary(conn)
    job_id = db.enqueue(article_id, "summarise", conn=conn, options={
        "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1/",
    })
    db.update_job(job_id, conn, state="done")

    page = client.get(f"/a/{slug}").text

    assert "Summarised with" in page
    assert "deepseek-chat" in page
    assert "DeepSeek" in page, "the provider is named, not just the endpoint"


def test_the_reader_names_no_model_for_a_summary_it_did_not_make(client, conn):
    """A summary block carries no origin, so one written by hand looks exactly
    like a generated one. Saying nothing beats naming the wrong model."""
    _article_id, slug = _article_with_summary(conn)

    page = client.get(f"/a/{slug}").text

    assert "Summarised with" not in page


def test_the_bookmarklet_posts_to_the_address_the_outside_world_uses(client, settings):
    """Behind a proxy the request's own host is the proxy's back end.

    The bookmarklet and the Shortcut are written once and kept, so a host
    taken from whichever request drew the page is the wrong one.
    """
    settings.public_url = "https://textcast.example.ts.net"

    body = client.get("/add").text

    assert 'var origin = "https://textcast.example.ts.net"' in body
    assert "https://textcast.example.ts.net/api/ingest" in body


def test_without_a_public_url_the_add_page_trusts_the_request(client, settings):
    settings.public_url = ""

    body = client.get("/add").text

    assert 'var origin = "http://testserver"' in body


def test_the_bookmarklet_gets_in_on_its_key_in_the_body(client, settings):
    """Its POST is cross-site, so the SameSite=Lax cookie is never sent.

    The key rides in the form instead, as it rides in the Shortcut's header.
    """
    account = sign_in_required(settings)

    response = client.post(
        "/api/ingest",
        data={
            "kind": "text",
            "title": "From the bookmarklet",
            "text": "A paragraph the browser had and the server could not fetch.",
            "token": account.ingest_key,
        },
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/a/from-the-bookmarklet"


def test_the_ingest_key_reaches_ingest_and_nothing_else(client, settings, conn):
    """A key sitting in clear in a bookmarks bar must not be able to delete.

    It was the session token until now, which is exactly what it could do.
    """
    from textcast.document import Article, Block, BlockKind, Section

    account = sign_in_required(settings)
    doc = Article(title="A note to keep", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)

    refused = client.post(
        f"/api/articles/{article_id}/delete",
        data={"token": account.ingest_key},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert refused.status_code == 303
    assert refused.headers["location"].startswith("/login?next=")
    assert db.get_article(article_id, conn) is not None, "the key deleted an article"


def test_the_sign_in_session_is_not_accepted_as_an_ingest_key(client, settings):
    """Two secrets, two jobs. Neither stands in for the other."""
    account = sign_in_required(settings)

    response = client.post(
        "/api/ingest",
        data={"kind": "text", "text": "A paragraph.", "token": account.session},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.headers["location"].startswith("/login?next=")


def test_a_key_in_the_body_hands_back_a_session(client, settings):
    """Otherwise the redirect to the new article bounces straight to /login."""
    account = sign_in_required(settings)

    response = client.post(
        "/api/ingest",
        data={"kind": "text", "title": "Session please", "text": "A paragraph.",
              "token": account.ingest_key},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.cookies.get("textcast_token") == account.session


def test_a_wrong_key_in_the_body_is_refused(client, settings):
    sign_in_required(settings)

    response = client.post(
        "/api/ingest",
        data={"kind": "text", "text": "A paragraph.", "token": "not-the-token"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_no_key_reaches_a_page_while_access_control_is_off(client, settings):
    """The bookmarklet needs none then, and the page must not carry one."""
    account = sign_in_required(settings)
    settings.require_auth = False

    assert account.ingest_key not in client.get("/add").text


def test_the_add_page_carries_the_ingest_key_and_not_the_session(client, settings):
    account = sign_in_required(settings)
    client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )

    body = client.get("/add").text

    assert account.ingest_key in body
    assert account.session not in body


def test_a_stored_instant_is_handed_to_the_browser_to_format(client, conn):
    """Everything is stored in UTC, and the reader is not in UTC.

    The zone cannot be a server setting: one library is read from a phone
    abroad and a laptop at home. So the page carries the instant.
    """
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="A timed note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    db.enqueue(db.save_article(doc, conn), conn=conn)

    body = client.get("/jobs").text

    assert 'data-when="datetime"' in body
    assert "UTC" in body


def test_a_bare_date_is_not_moved_into_a_zone(client):
    """A publication date has no time in it, and converting it loses a day."""
    assert 'data-when' not in web.when("2026-09-03")
    assert "2026-09-03" in web.when("2026-09-03")


def test_the_reader_shows_a_table_and_a_picture_where_the_prose_cites_them(client, conn, monkeypatch):
    """A visual block is a block, so it keeps its id, its gutter and its seek handle.

    `data-visual` is what the player stops at, which is the whole reason the
    parsers keep these: a chart you can pause on and look at.
    """
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="A charted note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="You can do a little ready reckoner."),
        Block(kind=BlockKind.TABLE, text="Table: Ker-CHING",
              media={"rows": [["Life", "$7bn"], ["Two years", "160%"]], "header": True,
                     "foot": "FTAV"}),
        Block(kind=BlockKind.FIGURE, text="Figure: Colossus I",
              media={"src": "https://images.test/c.png", "alt": "Colossus I"}),
        Block(kind=BlockKind.FIGURE, text="Chart: Contract length",
              media={"src": "https://public.flourish.studio/visualisation/30134862/thumbnail",
                     "frame": "https://flo.uri.sh/visualisation/30134862/embed"}),
    ])]).renumber()
    db.save_article(doc, conn)

    body = client.get("/a/a-charted-note").text

    assert '<figure class="b table" id="b0-1"' in body
    assert 'data-visual="1"' in body
    assert '<th scope="col">Life</th>' in body
    assert '<img src="https://images.test/c.png"' in body
    assert 'referrerpolicy="no-referrer"' in body
    # A chart is a picture now, and no third party is contacted to draw one.
    assert "<iframe" not in body
    assert "Load the chart" not in body
    assert "flo.uri.sh" not in body


def test_skipping_the_figure_captions_is_a_build_option(client):
    assert web._build_options("", "", False, skip_visuals=True) == {"skip_visuals": True}
    assert "skip_visuals" not in web._build_options("", "", False)


# --------------------------------------------------------------- the account


def signed_in_client(client, settings):
    account = sign_in_required(settings)
    client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    return account


def test_the_bar_carries_a_profile_mark_with_settings_and_sign_out(client, settings):
    signed_in_client(client, settings)

    body = client.get("/").text

    assert 'id="profile-toggle"' in body
    assert 'href="/settings"' in body
    assert "Sign out" in body
    # Nobody in particular until a photo is uploaded.
    assert 'class="avatar ' in body
    assert '<img src="/avatar"' not in body


def test_the_username_can_be_changed_and_is_what_signs_you_in(client, settings):
    signed_in_client(client, settings)

    client.post("/settings/profile", data={"username": "abdullah"}, follow_redirects=False)
    client.post("/logout", follow_redirects=False)

    stale = client.post("/login", data={"username": "reader", "password": PASSWORD})
    assert "do not match" in stale.text

    fresh = client.post(
        "/login",
        data={"username": "abdullah", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert fresh.status_code == 303


def test_changing_the_password_needs_the_current_one(client, settings):
    from textcast import accounts

    signed_in_client(client, settings)

    refused = client.post(
        "/settings/password",
        data={"current_password": "guess", "new_password": "a-longer-secret",
              "confirm_password": "a-longer-secret"},
    )

    assert "not the current password" in refused.text
    account = accounts.get(db.connect(settings.db_path))
    assert accounts.verify_password(PASSWORD, account.password_hash)


def test_changing_the_password_keeps_this_browser_signed_in(client, settings):
    """It signs out every *other* one, which is the point of changing it."""
    signed_in_client(client, settings)

    client.post(
        "/settings/password",
        data={"current_password": PASSWORD, "new_password": "a-longer-secret",
              "confirm_password": "a-longer-secret"},
        follow_redirects=False,
    )

    assert client.get("/", headers={"accept": "text/html"}).status_code == 200


def test_a_short_password_is_refused(client, settings):
    signed_in_client(client, settings)

    response = client.post(
        "/settings/password",
        data={"current_password": PASSWORD, "new_password": "short", "confirm_password": "short"},
    )

    assert "at least" in response.text


def test_regenerating_the_ingest_key_stops_the_old_one_working(client, settings):
    old = signed_in_client(client, settings).ingest_key

    client.post("/settings/ingest-key", follow_redirects=False)
    # Drop the session, or it would let the request through whatever key it
    # carried — which is the bookmarklet's situation, not a browser's.
    client.cookies.clear()

    response = client.post(
        "/api/ingest",
        data={"kind": "text", "title": "With the old key", "text": "A paragraph.", "token": old},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("/login?next=")


def test_a_profile_picture_is_stored_and_served(client, settings):
    from textcast import accounts

    signed_in_client(client, settings)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    client.post(
        "/settings/avatar",
        files={"photo": ("me.png", png, "image/png")},
        follow_redirects=False,
    )

    account = accounts.get(db.connect(settings.db_path))
    assert account.has_photo
    served = client.get(f"/avatar/{account.avatar}")
    assert served.status_code == 200
    assert served.content == png
    assert f'src="/avatar/{account.avatar}"' in client.get("/").text


def test_a_file_that_is_not_a_picture_is_refused(client, settings):
    signed_in_client(client, settings)

    response = client.post(
        "/settings/avatar",
        files={"photo": ("notes.pdf", b"%PDF-1.7", "application/pdf")},
    )

    assert "not a picture" in response.text


def test_the_settings_page_shows_the_ingest_key_and_never_the_password(client, settings):
    account = signed_in_client(client, settings)

    body = client.get("/settings").text

    assert account.ingest_key in body
    assert PASSWORD not in body
    assert account.password_hash not in body
    assert account.session not in body


def test_the_sign_in_page_names_nobody(client, settings):
    """It is the one page an unauthenticated stranger can read.

    The account was handed to every template, so `/login` carried the username
    twice and an `<img src="/avatar">`. On a public address that is the account
    name given to anyone who asks for the page.
    """
    account = sign_in_required(settings, username="abdullah")

    body = client.get("/login", headers={"accept": "text/html"}).text

    assert "abdullah" not in body
    assert account.ingest_key not in body
    assert "/avatar/" not in body
    assert 'id="profile-toggle"' not in body


def test_a_signed_out_reader_cannot_fetch_the_picture(client, settings):
    signed_in_client(client, settings)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    client.post("/settings/avatar", files={"photo": ("me.png", png, "image/png")},
                follow_redirects=False)
    from textcast import accounts

    name = accounts.get(db.connect(settings.db_path)).avatar
    assert client.get(f"/avatar/{name}").status_code == 200

    client.cookies.clear()

    assert client.get(f"/avatar/{name}", follow_redirects=False).status_code in (303, 401)


def test_the_profile_mark_returns_once_signed_in(client, settings):
    """The fix must not cost the bar its mark on an ordinary page."""
    signed_in_client(client, settings)

    body = client.get("/").text

    assert 'id="profile-toggle"' in body
    assert "reader" in body


def test_the_old_avatar_address_still_answers(client, settings):
    """A page held offline carries the markup it was saved with."""
    from textcast import accounts

    signed_in_client(client, settings)
    client.post("/settings/avatar",
                files={"photo": ("me.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
                follow_redirects=False)
    name = accounts.get(db.connect(settings.db_path)).avatar

    hop = client.get("/avatar", follow_redirects=False)

    assert hop.status_code == 302
    assert hop.headers["location"] == f"/avatar/{name}"
    assert hop.headers["cache-control"] == "no-cache"


def test_signing_out_everywhere_ends_other_sessions_and_keeps_this_one(client, settings):
    """Signing out only clears the cookie in the browser doing it.

    That is right — a phone should not be signed out because a laptop was —
    but it left a cookie copied off a machine working, and the only way to
    stop it was to change the password. This ends every session on its own,
    and writes the browser that asked a fresh one, because signing yourself
    out of the page you are on is not what the button says.
    """
    from textcast import accounts

    account = sign_in_required(settings)
    stolen = account.session

    client.post(
        "/login",
        data={"username": "reader", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    client.post("/settings/sign-out-everywhere", follow_redirects=False)

    current = accounts.get(db.connect(settings.db_path))
    assert current.session != stolen, "the old session still opens the library"
    assert current.password_hash == account.password_hash, "the password was not touched"
    assert client.cookies[web.COOKIE] == current.session
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200


def test_a_session_that_was_signed_out_everywhere_no_longer_opens_the_library(client, settings):
    from textcast import accounts

    account = sign_in_required(settings)
    accounts.rotate_session(db.connect(settings.db_path))

    client.cookies.set(web.COOKIE, account.session)

    assert client.get(
        "/", headers={"accept": "text/html"}, follow_redirects=False
    ).status_code == 303


def test_guessing_the_ingest_key_stops_being_free(client, settings):
    """It is the one route that takes a credential in a body from anywhere on
    the internet, and it used to cost a wrong guess nothing at all."""
    from textcast.web import limits

    sign_in_required(settings)
    limits.reset_all()

    codes = []
    for _ in range(limits.INGEST_ATTEMPTS.allowed + 3):
        codes.append(client.post("/api/ingest", data={
            "kind": "text", "title": "Guess", "text": "A body.", "token": "wrong",
        }).status_code)

    assert codes[0] == 401, "a wrong key is still a wrong key"
    assert 429 in codes, "guessing was never refused"
    assert codes.count(401) == limits.INGEST_ATTEMPTS.allowed


def test_the_refusal_says_what_to_do_and_when(client, settings):
    from textcast.web import limits

    sign_in_required(settings)
    limits.reset_all()
    for _ in range(limits.INGEST_ATTEMPTS.allowed):
        client.post("/api/ingest", data={"kind": "text", "text": "x", "token": "wrong"})

    refused = client.post("/api/ingest", data={"kind": "text", "text": "x", "token": "wrong"})

    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    # A sentence. The Shortcut has no screen, and `{"detail": "unauthorised"}`
    # is what it reported success over nine times running.
    assert refused.json()["detail"].endswith(".")
    assert "Try again in" in refused.json()["detail"]


def test_a_key_that_works_does_not_spend_the_failure_budget(client, settings):
    """Otherwise a run of real adds would lock the owner out of their own
    bookmarklet, which is a worse fault than the one being fixed."""
    from textcast.web import limits

    account = sign_in_required(settings)
    limits.reset_all()

    client.post("/api/ingest", data={"kind": "text", "text": "x", "token": "wrong"})
    for i in range(3):
        client.post("/api/ingest", data={
            "kind": "text", "title": f"Real {i}", "text": "A paragraph of it.",
            "token": account.ingest_key,
        })

    assert limits.INGEST_ATTEMPTS.check("testclient") == 0.0


def test_accepted_calls_are_bounded_too(client, settings):
    """A leaked key must not be able to make the server fetch and parse for
    ever. This is a different budget from the one guarding the door."""
    from textcast.web import limits

    account = sign_in_required(settings)
    limits.reset_all()

    codes = []
    for i in range(limits.INGEST_WORK.allowed + 2):
        codes.append(client.post("/api/ingest", data={
            "kind": "text", "title": f"Piece {i}", "text": "A paragraph of it.",
            "token": account.ingest_key,
        }).status_code)

    assert codes[-1] == 429
    assert codes.count(429) == 2


def test_the_offline_cache_is_not_named_after_the_release(client):
    """Naming it after the build made `activate` throw away everything the
    reader had marked to keep, on every deploy, silently. It is safe to keep
    across releases only because every media URL carries `?b=<built_at>`."""
    body = client.get("/sw.js").text

    assert 'const OFFLINE = "textcast-offline"' in body
    assert f"textcast-offline-{__version__}" not in body
    # The shell is the opposite case and must still move with the release.
    assert "const SHELL = `textcast-shell-${BUILD}`" in body


def test_every_audio_address_carries_the_build_it_belongs_to(client, settings, conn):
    """`section-000.opus` is rewritten by every build and the path does not
    change, so `immutable` was a lie: a browser holding the old file played it
    against the new timing map."""
    from textcast.audio import AudioManifest, BlockTiming, SectionAudio
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title="Stamped", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    db.save_manifest(
        article_id,
        AudioManifest(engine="tone", voice="t1", sample_rate=24000, bitrate="48k",
                      total_ms=1000,
                      sections=[SectionAudio(
                          idx=0, title="One", file="section-000.opus",
                          track="section-000.vtt", duration_ms=1000,
                          blocks=[BlockTiming(id="b0-0", kind="para", start_ms=0,
                                              dur_ms=1000, speech_ms=900)])]),
        audio_bytes=10,
        conn=conn,
    )

    payload = client.get(f"/api/articles/{article_id}/manifest").json()
    section = payload["sections"][0]

    assert "?b=" in section["file"], "the audio address has no build in it"
    assert "?b=" in section["track"], "the timing map moves with the audio"
    assert section["file"].split("?b=")[1] == section["track"].split("?b=")[1]
    assert int(section["file"].split("?b=")[1]) > 0

    # And the route itself still answers, because the query names a version
    # rather than selecting one.
    media = settings.media_dir / db.get_article(article_id, conn)["slug"]
    media.mkdir(parents=True, exist_ok=True)
    (media / "section-000.opus").write_bytes(b"OggS")
    served = client.get(f"/media/{db.get_article(article_id, conn)['slug']}/{section['file']}")
    assert served.status_code == 200
    assert served.content == b"OggS"


def _running_job(conn, article_id: int) -> None:
    """Put this article's build in the state a claimed one is really in."""
    db.enqueue(article_id, "build", conn=conn)
    conn.execute("UPDATE job SET state = 'running' WHERE article_id = ?", (article_id,))


@pytest.mark.parametrize(
    "path",
    ["/api/articles/{id}/delete", "/api/articles/{id}/reparse", "/api/articles/{id}/audio/delete"],
)
def test_a_running_build_answers_409_whichever_route_asked(client, conn, monkeypatch, path):
    """One meaning, one code.

    Each route mapped `IngestError` to whatever its own failure meant --
    400 for bad input, 404 for no such article -- so the running-job refusal
    inherited three different codes, and the delete-audio one said 404 about
    an article that plainly exists.
    """
    from textcast.document import Article, Block, BlockKind, Section

    monkeypatch.setattr(web, "_voices", lambda *a: [])
    doc = Article(title="A busy note", sections=[Section(title="One", blocks=[
        Block(kind=BlockKind.PARA, text="The body of it."),
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    _running_job(conn, article_id)

    reply = client.post(path.format(id=article_id), headers={"accept": "application/json"})

    assert reply.status_code == 409
    assert "running" in reply.json()["detail"]
