"""Section summaries: configuration, the block they become, and the queue."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from textcast import db, summarize
from textcast.document import Article, Block, BlockKind, Section
from textcast.summarize import Config, SummaryError, summarize_article, summarize_text


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


def test_a_key_is_read_from_any_of_the_usual_variables(conn, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini")
    assert summarize.config(conn).api_key == "from-gemini"


def test_a_config_without_a_key_is_not_ready(conn, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TEXTCAST_SUMMARY_API_KEY", raising=False)

    assert summarize.config(conn).ready is False


def test_saving_nothing_leaves_the_stored_key_alone(conn):
    summarize.save_config(conn, api_key="secret")
    summarize.save_config(conn, model="another-model")

    assert summarize.config(conn).api_key == "secret"


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


def test_a_failing_endpoint_is_reported_rather_than_raised_raw():
    client = FakeClient(reply=None)

    with pytest.raises(SummaryError, match="failed"):
        summarize_text("Anything at all.", Config(api_key="k"), client)


def test_input_is_capped_so_one_long_section_cannot_run_away():
    client = FakeClient()

    summarize_text("word " * 20000, Config(api_key="k"), client)

    assert len(client.prompts[0]) < summarize.MAX_INPUT_CHARS + len(summarize.DEFAULT_PROMPT)


# -- the block -------------------------------------------------------------


def test_a_summary_becomes_the_first_block_of_its_section():
    doc = article()
    client = FakeClient()

    added = summarize_article(doc, Config(api_key="k"), client)

    assert added == 2
    first = doc.sections[0].blocks[0]
    assert first.kind is BlockKind.SUMMARY
    assert first.id == "b0-0", "ids are renumbered, so the audio has to follow"
    assert doc.sections[0].blocks[1].id == "b0-1"


def test_a_section_that_already_has_one_is_left_alone():
    doc = article()
    doc.sections[0].blocks.insert(0, Block(kind=BlockKind.SUMMARY, text="Already done."))
    doc.renumber()
    client = FakeClient()

    added = summarize_article(doc, Config(api_key="k"), client)

    assert added == 1, "only the section without a summary is sent"
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

    monkeypatch.setenv("TEXTCAST_SUMMARY_API_KEY", "k")
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

    for name in ("TEXTCAST_SUMMARY_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    stored = ingest(text="A note.\n\nWith two paragraphs.", title="A note", build=False)

    with pytest.raises(IngestError, match="API key"):
        queue_summary(stored.article_id)
