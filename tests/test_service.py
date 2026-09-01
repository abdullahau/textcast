"""Ingestion, re-parsing and batched rebuilds."""

from __future__ import annotations

import pytest

from textcast import db
from textcast.pronounce import Rule
from textcast.service import IngestError, ingest, rebuild_many, reparse

NOTE = (
    "# Money Stuff\n\n"
    "The SEC asked about GAAP and the trade settled at 12x EBITDA.\n\n"
    "Nobody at the fund would say which desk booked it.\n"
)


def add_note(text: str = NOTE, title: str = "A note", tags=("Money Stuff",)):
    return ingest(text=text, title=title, build=False, tags=list(tags))


def test_reparse_keeps_the_tags_and_the_build_options(conn):
    first = add_note()
    db.set_build_options(first.article_id, {"voice": "bm_george"}, conn)

    again = reparse(first.article_id)

    assert again.slug == first.slug
    assert db.tags_for(again.article_id, conn) == ["Money Stuff"]
    assert db.get_build_options(again.article_id, conn) == {"voice": "bm_george"}


def test_a_failed_reparse_leaves_the_original_alone(conn, settings):
    """The old order deleted first, so a parse error lost the article entirely."""
    stored = add_note()
    (settings.source_dir / f"{stored.slug}.txt").write_bytes(b"   ")

    with pytest.raises(IngestError):
        reparse(stored.article_id)

    assert db.get_article(stored.article_id, conn) is not None
    assert db.tags_for(stored.article_id, conn) == ["Money Stuff"]


def test_a_missing_source_is_reported_rather_than_guessed(conn, settings):
    stored = add_note()
    (settings.source_dir / f"{stored.slug}.txt").unlink()

    with pytest.raises(IngestError, match="no stored source"):
        reparse(stored.article_id)


def test_a_rule_change_names_only_the_articles_that_use_the_word(conn):
    hit = add_note(title="Uses EBITDA")
    miss = add_note(text="A quiet week.\n\nNothing happened at all.\n", title="Quiet")
    for article in (hit, miss):
        db.set_status(article.article_id, "ready", conn)

    found = db.articles_matching(Rule(kind="word", pattern="EBITDA", replacement=""), conn)

    assert [row["id"] for row in found] == [hit.article_id]


def test_only_built_articles_are_offered_for_rebuild(conn):
    """An article with no audio is already queued or new; rebuilding it is noise."""
    unbuilt = add_note(title="Not built yet")

    found = db.articles_matching(Rule(kind="word", pattern="EBITDA", replacement=""), conn)

    assert [row["id"] for row in found] == []
    assert db.get_article(unbuilt.article_id, conn)["status"] == "new"


def test_rebuild_many_queues_each_article_once(conn):
    articles = [add_note(title=f"Note {n}", text=NOTE + f"\n\nBody {n}.\n") for n in range(3)]
    ids = [a.article_id for a in articles]

    queued = rebuild_many([*ids, 9999])

    assert queued == 3, "the article that does not exist is skipped, not raised on"
    assert {job["article_id"] for job in db.active_jobs(conn)} == set(ids)


def test_deleting_an_article_takes_its_audio_and_its_original_too(conn, settings):
    """The stored original used to be left orphaned in sources/ for ever."""
    from textcast.service import delete

    stored = add_note()
    source = settings.source_dir / f"{stored.slug}.txt"
    media = settings.media_dir / stored.slug
    media.mkdir(parents=True, exist_ok=True)
    (media / "section-000.opus").write_bytes(b"audio")
    assert source.exists()

    assert delete(stored.article_id) is True

    assert db.get_article(stored.article_id, conn) is None
    assert not source.exists(), "the original went with it"
    assert not media.exists(), "so did the audio"
    assert delete(stored.article_id) is False, "deleting it twice is not an error"


def test_reparse_keeps_the_original_it_is_about_to_read(conn, settings):
    """It deletes the article row, but the source is the thing it needs."""
    stored = add_note()
    source = settings.source_dir / f"{stored.slug}.txt"

    reparse(stored.article_id)

    assert source.exists()
