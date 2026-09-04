"""Section summaries: configuration, the block they become, and the queue."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from textcast import db, summarize
from textcast.document import Article, Block, BlockKind, Section
from textcast.summarize import Config, SummaryError, summarize_article, summarize_text

_real_client = summarize._client


class FakeClient:
    """Stands in for any OpenAI-compatible endpoint."""

    def __init__(self, reply: str = "What the section is about, briefly.") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.models: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages):
        self.models.append(model)
        self.prompts.append(messages[0]["content"])
        if self.reply is None:
            raise RuntimeError("the endpoint said no")
        message = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FlakyClient(FakeClient):
    """Refuses any call whose text carries ``poison``.

    A free tier's rate limit refuses part of a burst and answers the rest,
    which is the failure this module has to survive.
    """

    def __init__(self, poison: str, reply: str = "What the section is about, briefly.") -> None:
        super().__init__(reply)
        self.poison = poison

    def _create(self, model, messages):
        if self.poison in messages[0]["content"]:
            raise RuntimeError("429 rate limit exceeded")
        return super()._create(model, messages)


def article() -> Article:
    return Article(
        title="Money Stuff",
        sections=[
            Section(title="INMB", blocks=[
                Block(kind=BlockKind.PARA, text="The drug trial failed and the stock fell."),
                Block(kind=BlockKind.PARA, text="An executive had sold the week before."),
            ]),
            Section(title="Linqto", blocks=[
                Block(kind=BlockKind.PARA, text="Private shares changed hands at a markup."),
            ]),
        ],
    ).renumber()


# -- configuration ---------------------------------------------------------


def test_the_environment_is_the_default_and_the_app_overrides_it(conn, monkeypatch):
    """Editing the model in the app must work even when the container sets one."""
    monkeypatch.setenv("TEXTCAST_SUMMARY_MODEL", "from-the-environment")
    assert summarize.config(conn).model == "from-the-environment"

    summarize.save_config(conn, model="chosen-in-the-app")

    assert summarize.config(conn).model == "chosen-in-the-app"


def test_the_key_variable_is_not_named_after_a_vendor(conn, monkeypatch):
    """It read GEMINI_API_KEY and OPENAI_API_KEY too, which said the endpoint
    was one of those two when any of a dozen will do."""
    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini")
    monkeypatch.setenv("OPENAI_API_KEY", "from-openai")
    monkeypatch.setenv("TEXTCAST_SUMMARY_API_KEY", "and-not-this-one-either")

    assert summarize.config(conn).api_key == "", "keys are typed on the page, not exported"


def test_a_config_without_a_key_is_not_ready(conn):
    assert summarize.config(conn).ready is False


def test_saving_a_model_leaves_the_stored_key_alone(conn):
    summarize.save_credential("G", provider="gemini", api_key="secret", conn=conn)
    summarize.save_config(conn, credential_name="G")

    summarize.save_config(conn, model="another-model")

    assert summarize.config(conn).api_key == "secret"


# -- named keys ------------------------------------------------------------

GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEEPSEEK = "https://api.deepseek.com/v1/"


def test_one_provider_can_hold_more_than_one_key(conn):
    """The whole point of naming them. Keyed by endpoint there was room for
    exactly one Gemini key, so a second account had nowhere to go."""
    summarize.save_credential("Work Gemini", provider="gemini", api_key="work-key", conn=conn)
    summarize.save_credential("Home Gemini", provider="gemini", api_key="home-key", conn=conn)

    assert [c.name for c in summarize.credentials(conn)] == ["Work Gemini", "Home Gemini"]
    assert summarize.credential("Work Gemini", conn).api_key == "work-key"
    assert summarize.credential("Home Gemini", conn).api_key == "home-key"
    assert summarize.credential("Home Gemini", conn).endpoint == GEMINI, "the same endpoint"


def test_choosing_a_key_chooses_its_endpoint(conn):
    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)
    summarize.save_credential("D", provider="deepseek", api_key="deepseek-key", conn=conn)

    summarize.save_config(conn, credential_name="G")
    assert summarize.config(conn).api_key == "gemini-key"
    assert summarize.config(conn).base_url == GEMINI

    summarize.save_config(conn, credential_name="D")
    assert summarize.config(conn).api_key == "deepseek-key"
    assert summarize.config(conn).base_url == DEEPSEEK


def test_a_listed_provider_takes_its_address_from_the_code(conn):
    """So a provider that moves its endpoint moves everyone with it. Only an
    address typed over the top is stored."""
    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)
    summarize.save_config(conn, credential_name="G", base_url=GEMINI)

    assert db.get_setting(summarize.KEY_BASE_URL, "", conn) == "", "nothing to pin it"
    assert summarize.config(conn).base_url == GEMINI


def test_an_endpoint_typed_over_the_top_sticks(conn):
    """A gateway may sit in front of a provider, and that is the user's call."""
    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)

    summarize.save_config(conn, credential_name="G", base_url="https://proxy.internal/v1/")

    assert summarize.config(conn).base_url == "https://proxy.internal/v1/"
    assert summarize.config(conn).api_key == "gemini-key", "still that key"


def test_a_custom_provider_keeps_the_address_it_was_given(conn):
    mine = "https://my-gateway.internal/v1/"
    summarize.save_credential("Mine", base_url=mine, api_key="a-key", conn=conn)

    assert summarize.credential("Mine", conn).endpoint == mine
    summarize.save_config(conn, credential_name="Mine")
    assert summarize.config(conn).base_url == mine


def test_a_custom_provider_needs_an_address(conn):
    with pytest.raises(summarize.SummaryError, match="endpoint"):
        summarize.save_credential("Nowhere", api_key="a-key", conn=conn)


def test_a_key_needs_a_name(conn):
    with pytest.raises(summarize.SummaryError, match="name"):
        summarize.save_credential("  ", provider="gemini", api_key="a-key", conn=conn)


def test_a_hosted_key_may_not_be_left_blank(conn):
    """A blank key on a hosted provider is a 401 waiting to happen, and the
    field is a password box, so a mistyped save would look like a save."""
    with pytest.raises(summarize.SummaryError, match="needs a key"):
        summarize.save_credential("Groq", provider="groq", conn=conn)


def test_a_local_key_may_be_left_blank(conn):
    """Ollama and LM Studio are not behind an account, and still need naming
    so the model box has something to choose."""
    summarize.save_credential("Ollama", provider="ollama", conn=conn)

    summarize.save_config(conn, credential_name="Ollama", model="qwen3")
    assert summarize.config(conn).api_key == ""
    assert summarize.config(conn).ready is True


def test_saving_a_key_again_keeps_it_when_the_box_is_left_blank(conn):
    """The field is a password box, so an untouched one posts empty."""
    summarize.save_credential("G", provider="gemini", api_key="the-key", conn=conn)

    summarize.save_credential("G", provider="openai", api_key="", conn=conn)

    assert summarize.credential("G", conn).api_key == "the-key"
    assert summarize.credential("G", conn).provider == "openai", "the provider did change"


def test_forgetting_a_key_leaves_the_others(conn):
    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)
    summarize.save_credential("D", provider="deepseek", api_key="deepseek-key", conn=conn)

    assert summarize.forget_credential("D", conn) is True

    assert summarize.credential("D", conn) is None
    assert summarize.credential("G", conn).api_key == "gemini-key"


def test_forgetting_the_key_in_use_stops_using_it(conn):
    """Otherwise the config names a key that is gone and reads as ready."""
    summarize.save_credential("G", provider="gemini", api_key="gemini-key", conn=conn)
    summarize.save_config(conn, credential_name="G", model="a-model")

    summarize.forget_credential("G", conn)

    assert summarize.config(conn).api_key == ""
    assert summarize.config(conn).ready is False


def test_the_model_is_remembered_per_key(conn):
    """Choosing a key again should bring back the model it was used with, not
    carry the previous provider's name into a 404."""
    summarize.save_credential("G", provider="gemini", api_key="k1", conn=conn)
    summarize.save_credential("D", provider="deepseek", api_key="k2", conn=conn)

    summarize.save_config(conn, credential_name="G", model="gemini-2.5-pro")
    summarize.save_config(conn, credential_name="D", model="deepseek-reasoner")

    assert summarize.credential("G", conn).model == "gemini-2.5-pro"
    assert summarize.credential("D", conn).model == "deepseek-reasoner"


def test_a_key_is_named_by_its_provider_where_it_has_one(conn):
    summarize.save_credential("Work", provider="gemini", api_key="k", conn=conn)
    summarize.save_credential("Mine", base_url="https://box.internal/v1/", api_key="k", conn=conn)

    assert summarize.credential("Work", conn).provider_name == "Google Gemini"
    assert summarize.credential("Mine", conn).provider_name == "box.internal"


def test_a_stored_key_shows_only_its_tail(conn):
    summarize.save_credential("G", provider="gemini", api_key="a-long-enough-key-1234", conn=conn)

    assert summarize.credential("G", conn).hint == "1234"


def test_the_environment_cannot_supply_a_key(conn, monkeypatch):
    """One variable standing behind every provider meant the page could not
    say whose key was in use, and the answer changed with the endpoint."""
    monkeypatch.setenv("TEXTCAST_SUMMARY_API_KEY", "from-the-environment")

    assert summarize.config(conn).api_key == ""

    summarize.save_credential("G", provider="gemini", api_key="stored-key", conn=conn)
    summarize.save_config(conn, credential_name="G")
    assert summarize.config(conn).api_key == "stored-key"


def test_a_model_on_this_machine_needs_no_key(conn):
    """Ollama and LM Studio are not behind an account."""
    summarize.save_config(conn, base_url="http://127.0.0.1:11434/v1/", model="qwen3")
    cfg = summarize.config(conn)

    assert cfg.needs_key is False
    assert cfg.ready is True, "no key, and still usable"


def test_a_hosted_endpoint_without_a_key_is_not_ready(conn):
    summarize.save_config(conn, base_url="https://api.groq.com/openai/v1/", model="a-model")

    assert summarize.config(conn).ready is False


def test_an_old_flat_key_becomes_a_named_one(conn):
    """Two migrations in sequence, which is what an old library meets: the
    one flat key is filed under its endpoint, then given the provider's name.
    It must come out chosen, or summaries stop the day of the upgrade."""
    from textcast import migrate

    db.set_setting(summarize.KEY_BASE_URL, DEEPSEEK, conn)
    db.set_setting(summarize.KEY_API_KEY, "the-old-key", conn)
    db.set_setting(summarize.KEY_MODEL, "deepseek-chat", conn)

    migrate._scope_summary_key(conn)
    migrate._name_summary_keys(conn)

    stored = summarize.credential("DeepSeek", conn)
    assert stored.api_key == "the-old-key"
    assert stored.model == "deepseek-chat"
    assert stored.endpoint == DEEPSEEK
    assert summarize.config(conn).api_key == "the-old-key", "and it is the one in use"
    assert db.get_setting(summarize.KEY_API_KEY, "", conn) == "", "the flat key is gone"


def test_an_endpoint_scoped_key_is_named_for_its_provider(conn):
    """A library from the version in between, where keys were per endpoint."""
    from textcast import migrate

    db.set_setting(summarize.PREFIX_API_KEY + summarize.endpoint_id(GEMINI), "g", conn)
    db.set_setting(summarize.PREFIX_API_KEY + "https://box.internal/v1", "m", conn)
    db.set_setting(summarize.KEY_BASE_URL, GEMINI, conn)

    migrate._name_summary_keys(conn)

    assert {c.name for c in summarize.credentials(conn)} == {"Google Gemini", "https://box.internal/v1"}
    assert summarize.credential("https://box.internal/v1", conn).endpoint == "https://box.internal/v1"
    assert summarize.config(conn).credential == "Google Gemini"
    assert db.get_setting(summarize.KEY_BASE_URL, "", conn) == "", "not pinned to the old address"


# -- the call --------------------------------------------------------------


def test_the_prompt_carries_the_text_and_names_the_model():
    client = FakeClient()
    cfg = Config(model="a-model", api_key="k")

    summarize_text("The drug trial failed.", cfg, client)

    assert "The drug trial failed." in client.prompts[0]
    assert client.models == ["a-model"]


def test_a_prompt_without_the_placeholder_still_sees_the_text():
    client = FakeClient()
    cfg = Config(prompt="Summarise this for a listener.", api_key="k")

    summarize_text("Private shares changed hands.", cfg, client)

    assert "Private shares changed hands." in client.prompts[0]


def test_a_prompt_may_contain_braces_of_its_own():
    """The prompt is editable on the Summaries page, and `format` read every
    brace in it. A prompt asking for JSON, with a `{"summary": "..."}` example
    in it, raised KeyError and every section of every article failed with that
    as the reason. `{text}` is the only placeholder there has ever been, so it
    is the only one substituted.
    """
    client = FakeClient()
    example = '{"summary": "..."}'
    cfg = Config(prompt=f"Reply as JSON like {example} for this:\n\n{{text}}", api_key="k")

    summarize_text("The fund closed the position.", cfg, client)

    assert "The fund closed the position." in client.prompts[0]
    assert example in client.prompts[0], "the example survives intact"


def test_a_failing_endpoint_is_reported_rather_than_raised_raw():
    client = FakeClient(reply=None)

    with pytest.raises(SummaryError, match="failed"):
        summarize_text("Anything at all.", Config(api_key="k"), client)


def test_a_malformed_reply_is_reported_rather_than_raised_raw():
    """Some "OpenAI-compatible" gateways answer with `message: null`."""

    class NullMessageClient(FakeClient):
        def _create(self, model, messages):
            self.models.append(model)
            self.prompts.append(messages[0]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(message=None)])

    with pytest.raises(SummaryError, match="failed"):
        summarize_text("Anything at all.", Config(api_key="k"), NullMessageClient())


def test_input_is_capped_so_one_long_section_cannot_run_away():
    client = FakeClient()

    summarize_text("word " * 20000, Config(api_key="k"), client)

    assert len(client.prompts[0]) < summarize.MAX_INPUT_CHARS + len(summarize.DEFAULT_PROMPT)


# -- the block -------------------------------------------------------------


def test_a_summary_becomes_the_first_block_of_its_section():
    doc = article()
    client = FakeClient()

    run = summarize_article(doc, Config(api_key="k"), client)

    assert run.added == 2
    first = doc.sections[0].blocks[0]
    assert first.kind is BlockKind.SUMMARY
    assert first.id == "b0-0", "ids are renumbered, so the audio has to follow"
    assert doc.sections[0].blocks[1].id == "b0-1"


def test_a_section_that_already_has_one_is_left_alone():
    doc = article()
    doc.sections[0].blocks.insert(0, Block(kind=BlockKind.SUMMARY, text="Already done."))
    doc.renumber()
    client = FakeClient()

    run = summarize_article(doc, Config(api_key="k"), client)

    assert run.added == 1, "only the section without a summary is sent"
    assert doc.sections[0].blocks[0].text == "Already done."


def test_the_heading_is_not_fed_back_as_content():
    doc = Article(sections=[Section(title="INMB", blocks=[
        Block(kind=BlockKind.HEADING, text="INMB"),
        Block(kind=BlockKind.PARA, text="The body of the section."),
    ])], title="T").renumber()
    client = FakeClient()

    summarize_article(doc, Config(api_key="k"), client)

    assert "The body of the section." in client.prompts[0]
    assert client.prompts[0].count("INMB") == 0


def test_a_heading_only_section_is_skipped_not_reported_as_a_failure():
    """Nothing to summarise means nothing sent, not a false model failure."""
    doc = Article(sections=[
        Section(title="Explore more", blocks=[Block(kind=BlockKind.HEADING, text="Explore more")]),
        Section(title="INMB", blocks=[Block(kind=BlockKind.PARA, text="The body of the section.")]),
    ], title="T").renumber()
    client = FakeClient()

    run = summarize_article(doc, Config(api_key="k"), client)

    assert run.added == 1 and run.errors == []
    assert len(client.prompts) == 1, "the model was never asked about the empty section"
    assert doc.sections[0].blocks[0].kind is BlockKind.HEADING, "left with no summary, not an error"


# -- storage and the queue -------------------------------------------------


def test_replacing_blocks_renumbers_and_drops_the_stale_audio(conn):
    doc = article()
    article_id = db.save_article(doc, conn)
    conn.execute("UPDATE article SET audio_ms = 60000, audio_bytes = 1000 WHERE id = ?", (article_id,))
    conn.execute("UPDATE section SET file = 'section-000.opus' WHERE article_id = ?", (article_id,))

    summarize_article(doc, Config(api_key="k"), FakeClient())
    db.replace_blocks(article_id, doc, conn)

    row = db.get_article(article_id, conn)
    stored = db.load_article(article_id, conn)
    assert row["audio_ms"] == 0, "the old timings no longer line up"
    assert stored.sections[0].blocks[0].kind is BlockKind.SUMMARY
    assert conn.execute(
        "SELECT file FROM section WHERE article_id = ? AND idx = 0", (article_id,)
    ).fetchone()["file"] is None


def test_search_finds_a_summary_like_any_other_block(conn):
    doc = article()
    article_id = db.save_article(doc, conn)
    summarize_article(doc, Config(api_key="k"), FakeClient("A memorable phrase about failure."))
    db.replace_blocks(article_id, doc, conn)

    hits = db.search("memorable", conn)

    assert [h["kind"] for h in hits] == ["summary", "summary"]


def test_an_article_with_a_summary_is_not_offered_again(conn):
    doc = article()
    article_id = db.save_article(doc, conn)
    assert [r["id"] for r in db.summarisable(conn)] == [article_id]

    summarize_article(doc, Config(api_key="k"), FakeClient())
    db.replace_blocks(article_id, doc, conn)

    assert db.summarisable(conn) == []


def test_summarising_queues_the_model_pass_not_the_build(conn, settings, monkeypatch):
    from textcast.service import ingest

    summarize.save_credential("G", provider="gemini", api_key="k", conn=conn)
    summarize.save_config(conn, credential_name="G")
    stored = ingest(
        text="A note.\n\nWith two paragraphs in it.",
        title="A note",
        options={"summarize": True},
    )

    job = db.get_job(stored.job_id, conn)
    assert job["kind"] == "summarise"


def test_summarising_is_refused_when_no_model_is_configured(conn, settings, monkeypatch):
    from textcast.service import IngestError, ingest
    from textcast.service import summarize as queue_summary

    stored = ingest(text="A note.\n\nWith two paragraphs.", title="A note", build=False)

    with pytest.raises(IngestError, match="API key"):
        queue_summary(stored.article_id)


# -- saving settings must never start work --------------------------------


def test_saving_a_model_does_not_summarise_anything(conn, settings):
    """The whole library is one confirm box away, so saving must not touch it."""
    import pytest as _pytest

    from textcast.web import app as web

    _pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    doc = article()
    article_id = db.save_article(doc, conn)

    def refuse(cfg):
        raise AssertionError("saving the settings called the model")

    web.settings = settings
    summarize._client = refuse
    try:
        with TestClient(web.app) as client:
            client.post("/summaries", data={
                "model": "gemini-2.5-flash",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "api_key": "a-real-looking-key",
                "prompt": summarize.DEFAULT_PROMPT,
                "keep_key": "true",
            })
    finally:
        summarize._client = _real_client

    assert summarize.config(conn).model == "gemini-2.5-flash"
    assert db.active_jobs(conn) == [], "no job was queued"
    assert conn.execute(
        "SELECT COUNT(*) c FROM block WHERE article_id=? AND kind='summary'", (article_id,)
    ).fetchone()["c"] == 0, "no summary block was written"



def test_summarising_again_replaces_what_is_there(conn):
    """Without this, the second pass sees a summary already present and stops."""
    doc = article()
    first = FakeClient("The first attempt.")
    summarize_article(doc, Config(api_key="k"), first)

    second = FakeClient("A better one.")
    run = summarize_article(doc, Config(api_key="k"), second, replace=True)

    assert run.added == 2
    heads = [s.blocks[0] for s in doc.sections]
    assert [b.kind for b in heads] == [BlockKind.SUMMARY, BlockKind.SUMMARY]
    assert [b.text for b in heads] == ["A better one.", "A better one."]
    assert len(second.prompts) == 2, "every section went back to the model"


def test_summarising_again_does_not_stack_up_blocks(conn):
    doc = article()
    summarize_article(doc, Config(api_key="k"), FakeClient())
    before = sum(len(s.blocks) for s in doc.sections)

    summarize_article(doc, Config(api_key="k"), FakeClient(), replace=True)

    assert sum(len(s.blocks) for s in doc.sections) == before


# -- one section failing ---------------------------------------------------


def test_a_section_that_fails_does_not_cost_the_ones_that_worked():
    """The pass raised on the first refusal, so nothing at all was stored —
    including the sections the model had already written."""
    doc = article()

    run = summarize_article(doc, Config(api_key="k"), FlakyClient("Private shares"))

    assert (run.added, run.failed, run.total) == (1, 1, 2)
    assert doc.sections[0].blocks[0].kind is BlockKind.SUMMARY
    assert doc.sections[1].blocks[0].kind is BlockKind.PARA, "the failed one is untouched"
    assert "Linqto" in run.errors[0], "the error names the section"
    assert "429" in run.errors[0], "and what the endpoint said"


def test_a_failed_section_keeps_the_summary_it_already_had():
    """Losing text you already had to a rate limit is the worst of both."""
    doc = article()
    summarize_article(doc, Config(api_key="k"), FakeClient("The first attempt."))

    run = summarize_article(
        doc,
        Config(api_key="k"),
        FlakyClient("Private shares", "A better one."),
        replace=True,
    )

    assert (run.added, run.failed) == (1, 1)
    assert doc.sections[0].blocks[0].text == "A better one."
    assert doc.sections[1].blocks[0].text == "The first attempt."


def test_an_empty_reply_is_a_failure_and_not_a_silent_pass():
    doc = article()

    run = summarize_article(doc, Config(api_key="k"), FakeClient(""))

    assert (run.added, run.failed) == (0, 2)
    assert "empty" in run.errors[0]


def test_every_section_is_reported_as_it_resolves():
    """The caller stores each summary as it lands, so it has to hear about
    them one at a time rather than about the pass at the end."""
    doc = article()
    seen: list = []

    summarize_article(
        doc, Config(api_key="k"), FlakyClient("Private shares"), on_section=seen.append
    )

    assert [o.done for o in seen] == [1, 2]
    assert {o.total for o in seen} == {2}
    assert (seen[-1].added, seen[-1].failed) == (1, 1)
    assert seen[-1].index in (0, 1), "the section it is about, not the call"
