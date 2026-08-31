from __future__ import annotations

import pytest

from textcast import db
from textcast.audio import AudioManifest, BlockTiming, SectionAudio
from textcast.document import Article, Block, BlockKind, Section


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXTCAST_DATA_DIR", str(tmp_path))
    from textcast.settings import get_settings

    get_settings(refresh=True)
    db.close()
    connection = db.init(tmp_path / "test.db")
    yield connection
    db.close()


def make_article(title: str = "A Drug-Trial Stock Sale", series: str | None = "Money Stuff") -> Article:
    return Article(
        title=title,
        subtitle="INmune, Linqto and the AI pay wars.",
        source="Bloomberg",
        series=series,
        adapter="bloomberg",
        sections=[
            Section(title="INMB", blocks=[
                Block(kind=BlockKind.PARA, text="Here is a weird little trade in biotech stock."),
                Block(kind=BlockKind.QUOTE, text="Showing no effects in the population."),
            ]),
            Section(title="Linqto", blocks=[
                Block(kind=BlockKind.PARA, text="Selling shares in private companies is hard."),
            ]),
        ],
    ).renumber()


def test_save_and_reload_roundtrips(conn):
    article_id = db.save_article(make_article(), conn)
    loaded = db.load_article(article_id, conn)

    assert loaded is not None
    assert loaded.title == "A Drug-Trial Stock Sale"
    assert loaded.series == "Money Stuff"
    assert [s.title for s in loaded.sections] == ["INMB", "Linqto"]
    assert [b.id for _s, b in loaded.blocks()] == ["b0-0", "b0-1", "b1-0"]
    assert [b.kind for _s, b in loaded.blocks()] == [BlockKind.PARA, BlockKind.QUOTE, BlockKind.PARA]


def test_reingesting_the_same_content_is_refused(conn):
    first = db.save_article(make_article(), conn)
    with pytest.raises(db.DuplicateArticle) as caught:
        db.save_article(make_article(), conn)
    assert caught.value.article_id == first


def test_different_articles_with_the_same_title_get_distinct_slugs(conn):
    a = make_article()
    b = make_article()
    b.sections[0].blocks[0].text = "Entirely different opening paragraph here."

    db.save_article(a, conn)
    db.save_article(b, conn)
    slugs = [r["slug"] for r in db.list_articles(conn)]
    assert len(set(slugs)) == 2


def test_saving_a_series_registers_it(conn):
    db.save_article(make_article(), conn)
    rows = db.list_series(conn)
    assert [r["name"] for r in rows] == ["Money Stuff"]
    assert rows[0]["issues"] == 1


def test_full_text_search_finds_a_block_and_locates_it(conn):
    article_id = db.save_article(make_article(), conn)
    hits = db.search("biotech", conn)

    assert len(hits) == 1
    assert hits[0]["article_id"] == article_id
    assert hits[0]["block_id"] == "b0-0"
    assert "<mark>" in hits[0]["snippet"]
    assert db.search("nonexistentword", conn) == []


def test_search_index_follows_a_deletion(conn):
    article_id = db.save_article(make_article(), conn)
    assert db.search("biotech", conn)
    db.delete_article(article_id, conn)
    assert db.search("biotech", conn) == []


def manifest_for() -> AudioManifest:
    return AudioManifest(
        engine="fake", voice="M1", sample_rate=44100, bitrate="32k", total_ms=9000,
        sections=[
            SectionAudio(idx=0, title="INMB", file="section-000.opus", duration_ms=6000, blocks=[
                BlockTiming(id="b0-0", kind="para", start_ms=0, dur_ms=3000, speech_ms=2650),
                BlockTiming(id="b0-1", kind="quote", start_ms=3000, dur_ms=3000, speech_ms=2650),
            ]),
            SectionAudio(idx=1, title="Linqto", file="section-001.opus", duration_ms=3000, blocks=[
                BlockTiming(id="b1-0", kind="para", start_ms=0, dur_ms=3000, speech_ms=2650),
            ]),
        ],
    )


def test_manifest_writes_timings_onto_the_existing_blocks(conn):
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=123456, conn=conn)

    row = db.get_article(article_id, conn)
    assert row["status"] == "ready"
    assert row["audio_ms"] == 9000
    assert row["audio_bytes"] == 123456
    assert row["voice"] == "M1"

    blocks = conn.execute(
        "SELECT block_id, start_ms, dur_ms FROM block WHERE article_id = ? ORDER BY section_idx, idx",
        (article_id,),
    ).fetchall()
    assert [(b["block_id"], b["start_ms"]) for b in blocks] == [("b0-0", 0), ("b0-1", 3000), ("b1-0", 0)]

    sections = conn.execute(
        "SELECT file, duration_ms FROM section WHERE article_id = ? ORDER BY idx", (article_id,)
    ).fetchall()
    assert [s["file"] for s in sections] == ["section-000.opus", "section-001.opus"]


def test_position_survives_and_feeds_continue_listening(conn):
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)

    db.save_position(article_id, section_idx=1, ms=42000, conn=conn)
    position = db.get_position(article_id, conn)
    assert position["ms"] == 42000 and position["section_idx"] == 1

    assert [r["id"] for r in db.continue_listening(conn)] == [article_id]

    # Finished articles drop off the list rather than nagging.
    db.save_position(article_id, section_idx=1, ms=42000, finished=True, conn=conn)
    assert db.continue_listening(conn) == []


def test_queue_claims_one_job_at_a_time(conn):
    article_id = db.save_article(make_article(), conn)
    job_id = db.enqueue(article_id, conn=conn, options={"voice": "F1"})

    assert db.get_article(article_id, conn)["status"] == "queued"

    claimed = db.claim_job(conn)
    assert claimed["id"] == job_id
    assert claimed["state"] == "queued", "the claimed row is the pre-update snapshot"
    assert db.get_job(job_id, conn)["state"] == "running"
    assert db.get_article(article_id, conn)["status"] == "building"

    assert db.claim_job(conn) is None, "the only job is already running"


def test_requeueing_replaces_a_pending_job(conn):
    article_id = db.save_article(make_article(), conn)
    first = db.enqueue(article_id, conn=conn)
    second = db.enqueue(article_id, conn=conn)

    assert db.get_job(first, conn) is None
    assert len(db.active_jobs(conn)) == 1
    assert db.active_jobs(conn)[0]["id"] == second


def test_archiving_hides_an_article_from_the_library(conn):
    article_id = db.save_article(make_article(), conn)
    assert len(db.list_articles(conn)) == 1

    db.set_flag(article_id, "archived", True, conn)
    assert db.list_articles(conn) == []
    assert len(db.list_articles(conn, archived=True)) == 1


def test_stats_summarise_the_library(conn):
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=500, conn=conn)

    summary = db.stats(conn)
    assert summary["articles"] == 1
    assert summary["ready"] == 1
    assert summary["audio_ms"] == 9000
    assert summary["words"] > 0
