"""Ingestion, re-parsing and batched rebuilds."""

from __future__ import annotations

import pytest

from textcast import db
from textcast.document import Block
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


def test_deleting_audio_keeps_the_article_and_takes_the_cache(conn, settings):
    """There was no way back from a build except deleting the whole article."""
    from textcast.service import cached_renders, delete_audio

    stored = add_note()
    media = settings.media_dir / stored.slug
    media.mkdir(parents=True, exist_ok=True)
    (media / "section-000.opus").write_bytes(b"audio")
    (media / "section-000.vtt").write_text("WEBVTT")
    for path in cached_renders(stored.article_id, conn, settings):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 8)
    conn.execute("UPDATE article SET status='ready', audio_ms=9000 WHERE id=?", (stored.article_id,))
    conn.execute("UPDATE block SET start_ms=0, dur_ms=1 WHERE article_id=?", (stored.article_id,))

    removed = delete_audio(stored.article_id)

    row = db.get_article(stored.article_id, conn)
    assert removed >= 3, "the media files and the cached renders"
    assert (row["status"], row["audio_ms"]) == ("new", 0)
    assert not media.exists()
    assert list(settings.cache_dir.glob("*.f32")) == []
    assert db.load_article(stored.article_id, conn).word_count > 0, "the text stays"
    assert (settings.source_dir / f"{stored.slug}.txt").exists(), "so does the original"


def test_deleting_summaries_keeps_the_article_and_drops_the_audio(conn, settings):
    from textcast.document import BlockKind
    from textcast.service import delete_summaries
    from textcast.summarize import Config, summarize_article

    stored = add_note()
    article = db.load_article(stored.article_id, conn)

    class Fake:
        def __init__(self):
            from types import SimpleNamespace
            reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="In short."))])
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply))

    summarize_article(article, Config(api_key="k"), Fake())
    db.replace_blocks(stored.article_id, article, conn)
    assert any(b.kind is BlockKind.SUMMARY for _s, b in db.load_article(stored.article_id, conn).blocks())

    dropped = delete_summaries(stored.article_id)

    after = db.load_article(stored.article_id, conn)
    assert dropped > 0
    assert not any(b.kind is BlockKind.SUMMARY for _s, b in after.blocks())
    assert after.sections[0].blocks[0].id == "b0-0", "ids close up behind them"
    assert delete_summaries(stored.article_id) == 0, "nothing left to remove"


def test_deleting_summaries_keeps_the_renders_of_the_blocks_it_keeps(conn, settings):
    """A rebuild after this is an encode, not a trip back to the model.

    This called `delete_audio`, which also takes every render only this
    article wants — so dropping one summary cost the article every other
    block's synthesis too, minutes of it, for text that had not changed.
    Removing a block is a hand edit by another name, and `edit_blocks` has
    always kept the cache for exactly this reason.
    """
    from textcast.document import Block, BlockKind
    from textcast.service import cached_renders, delete_summaries

    stored = add_note()
    article = db.load_article(stored.article_id, conn)
    article.sections[0].blocks.insert(
        0, Block(kind=BlockKind.SUMMARY, text="In short, a summary.")
    )
    db.replace_blocks(stored.article_id, article, conn)

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    renders = cached_renders(stored.article_id, conn, settings)
    for path in renders:
        path.write_bytes(b"\0" * 8)
    media = settings.media_dir / stored.slug
    media.mkdir(parents=True, exist_ok=True)
    (media / "section-000.opus").write_bytes(b"audio")

    assert delete_summaries(stored.article_id) == 1

    assert not media.exists(), "the audio no longer lines up, so it goes"
    kept = [p for p in renders if p.exists()]
    assert len(kept) >= len(renders) - 1, "only the summary's own render may go"
    assert kept, "the surviving blocks keep theirs"


def test_a_summary_pass_records_the_model_it_asked(conn, settings):
    """The job is the only durable record of how an article was summarised. A
    summary is a block like any other and says nothing about where it came
    from, so one written by hand is indistinguishable from a generated one."""
    from textcast import summarize
    from textcast.service import summarize as queue_summary

    summarize.save_credential("D", provider="deepseek", api_key="k", conn=conn)
    summarize.save_config(conn, credential_name="D", model="deepseek-chat")
    stored = add_note()

    job_id = queue_summary(stored.article_id, settings)
    db.update_job(job_id, conn, state="done")

    record = db.last_summary(stored.article_id, conn)
    assert record["model"] == "deepseek-chat"
    assert record["base_url"] == "https://api.deepseek.com/v1/"


def test_an_article_nobody_summarised_records_nothing(conn, settings):
    """Which is what the reader shows for a summary written by hand: silence
    beats naming a model that did not write it."""
    stored = add_note()

    assert db.last_summary(stored.article_id, conn) == {}


def test_a_swept_cache_keeps_only_what_a_block_can_still_reach(conn, settings):
    """Nothing collected these before.

    A rule change, a text edit, a re-parse or a deleted article each left
    their renders behind, and the key is a hash so nothing overwrote them.
    Measured on the real library before this existed: 363 of 691 files,
    0.96 GB, 43% of the cache, unreachable.
    """
    from textcast.audio import CACHE_SUFFIX
    from textcast.service import cache_keys, sweep_cache

    result = ingest(text="# A note\n\nA paragraph to render.", title="Sweepable")
    keys = cache_keys(result.article_id, conn, settings)
    assert keys, "the article has blocks, so it wants renders"

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (settings.cache_dir / f"{key}{CACHE_SUFFIX}").write_bytes(b"\x00\x01")
    orphan = settings.cache_dir / f"{'f' * 64}{CACHE_SUFFIX}"
    orphan.write_bytes(b"\x00" * 100)
    stale_format = settings.cache_dir / f"{next(iter(keys))}.f32"
    stale_format.write_bytes(b"\x00" * 50)

    removed, freed = sweep_cache(settings, conn)

    assert not orphan.exists(), "a render no block can reach must go"
    assert not stale_format.exists(), "a file in the old format is unreachable too"
    assert removed == 2 and freed == 150
    for key in keys:
        assert (settings.cache_dir / f"{key}{CACHE_SUFFIX}").exists()


def test_deleting_an_article_takes_its_renders_with_it(conn, settings):
    """They used to stay on disk for ever, unreachable and uncounted."""
    from textcast.audio import CACHE_SUFFIX
    from textcast.service import cache_keys
    from textcast.service import delete as delete_article

    result = ingest(text="# Going\n\nA paragraph that will not survive.", title="Going")
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        settings.cache_dir / f"{key}{CACHE_SUFFIX}"
        for key in cache_keys(result.article_id, conn, settings)
    ]
    for path in paths:
        path.write_bytes(b"\x00" * 10)

    delete_article(result.article_id, settings)

    assert paths and not any(p.exists() for p in paths)


def test_a_render_two_articles_share_survives_one_of_them(conn, settings):
    """The key is a hash of the text, so a quoted paragraph is one file."""
    from textcast.audio import CACHE_SUFFIX
    from textcast.service import cache_keys
    from textcast.service import delete as delete_article

    body = "# Shared\n\nThe very same paragraph, in two pieces."
    first = ingest(text=body, title="First copy")
    second = ingest(text=body, title="Second copy")
    shared = cache_keys(first.article_id, conn, settings) & cache_keys(
        second.article_id, conn, settings
    )
    assert shared, "the same text under the same settings is the same key"

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    for key in shared:
        (settings.cache_dir / f"{key}{CACHE_SUFFIX}").write_bytes(b"\x00" * 10)

    delete_article(first.article_id, settings)

    for key in shared:
        assert (settings.cache_dir / f"{key}{CACHE_SUFFIX}").exists(), (
            "the other article still wants it"
        )


def test_the_keys_follow_the_article_engine_not_a_hardcoded_one(conn, settings):
    """It said "kokoro", which was right when there was one engine.

    Thirteen of fourteen articles here are kokoro-onnx, so the keys named
    files that did not exist. Worse, for an article built under kokoro once
    and rebuilt under ONNX they named the *old* files, so "Delete audio"
    removed those and left the ones in use.
    """
    from textcast.service import cache_keys

    result = ingest(text="# Engines\n\nOne paragraph, two engines.", title="Engines")

    db.set_build_options(result.article_id, {"engine": "kokoro"}, conn)
    as_kokoro = cache_keys(result.article_id, conn, settings)
    db.set_build_options(result.article_id, {"engine": "kokoro-onnx"}, conn)
    as_onnx = cache_keys(result.article_id, conn, settings)

    assert as_kokoro and as_onnx
    assert as_kokoro.isdisjoint(as_onnx), "one engine's render is not the other's"


def test_dropping_audio_spares_a_render_another_article_wants(conn, settings):
    """cached_renders is what "Delete audio" deletes, and a file can be shared."""
    from textcast.service import cache_keys, cached_renders

    body = "# Twice\n\nA paragraph that appears in two articles."
    first = ingest(text=body, title="Once")
    second = ingest(text=body, title="Twice")

    mine = {p.stem for p in cached_renders(first.article_id, conn, settings)}

    assert mine.isdisjoint(cache_keys(second.article_id, conn, settings))


def test_re_parsing_keeps_the_summaries_a_model_wrote(settings, conn):
    """A summary is a block, and no stored source ever held one.

    So re-parsing used to delete every summary in the library without saying
    so — thirty-five of them here, each one a call to a model.
    """
    from textcast import service
    from textcast.document import BlockKind

    html = (
        "<html><head><title>A charted note</title></head><body><article>"
        "<h1>A charted note</h1>"
        "<h2>The first part</h2>"
        f"<p>{'Sentence about the market. ' * 12}</p>"
        f"<p>{'Another sentence entirely. ' * 12}</p>"
        "<h2>The second part</h2>"
        f"<p>{'A third kind of sentence. ' * 12}</p>"
        "</article></body></html>"
    )
    stored = service.ingest(html=html, url="https://x.test/p", build=False, settings=settings)

    article = db.load_article(stored.article_id, conn)
    for section in article.sections:
        section.blocks.insert(0, Block(kind=BlockKind.SUMMARY, text=f"Summary of {section.title}."))
    db.replace_blocks(stored.article_id, article.renumber(), conn)

    result = service.reparse(stored.article_id, settings=settings)

    assert result.summaries_kept == 2
    assert result.summaries_lost == 0
    back = db.load_article(result.article_id, conn)
    for section in back.sections:
        assert section.blocks[0].kind is BlockKind.SUMMARY, "not at the head of its section"
        assert section.blocks[0].text == f"Summary of {section.title}."


def test_a_summary_whose_section_is_gone_is_counted_not_hidden(settings, conn):
    """The section title is the only handle, and a parser fix can move one."""
    from textcast import service
    from textcast.document import BlockKind

    html = (
        "<html><head><title>A note</title></head><body><article><h1>A note</h1>"
        f"<p>{'Sentence about the market. ' * 14}</p>"
        f"<p>{'Another sentence entirely. ' * 14}</p>"
        "</article></body></html>"
    )
    stored = service.ingest(html=html, url="https://x.test/q", build=False, settings=settings)

    article = db.load_article(stored.article_id, conn)
    article.sections[0].blocks.insert(0, Block(kind=BlockKind.SUMMARY, text="Orphaned."))
    db.replace_blocks(stored.article_id, article.renumber(), conn)
    # Straight to the row: `replace_blocks` writes blocks, not section titles.
    conn.execute(
        "UPDATE section SET title = ? WHERE article_id = ? AND idx = 0",
        ("A title the parser will not produce again", stored.article_id),
    )
    conn.commit()

    result = service.reparse(stored.article_id, settings=settings)

    assert result.summaries_kept == 0
    assert result.summaries_lost == 1


def test_re_parsing_queues_nothing_and_says_so(settings, conn):
    """Replaying a parser fix over the library queued a build per article.

    That is the CPU for the rest of the day, and nobody asked for it. The
    audio is invalid either way; when to spend the machine is the owner's call.
    """
    from textcast import service

    stored = add_note()
    before = conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]

    result = service.reparse(stored.article_id, settings=settings)

    assert result.job_id is None
    assert conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == before


def test_a_re_parse_that_changes_nothing_leaves_the_article_alone(settings, conn):
    """The ids would not have moved, so the audio is still correct.

    Replacing anyway took the article out of `ready` and orphaned audio that
    was fine — which, over a library, reads as "re-parsing broke everything".
    """
    from textcast import service

    stored = add_note()
    conn.execute("UPDATE article SET status = 'ready', audio_ms = 12345 WHERE id = ?",
                 (stored.article_id,))
    conn.commit()

    result = service.reparse(stored.article_id, settings=settings)

    assert result.unchanged
    assert result.article_id == stored.article_id, "it replaced the row"
    row = db.get_article(stored.article_id, conn)
    assert row["status"] == "ready"
    assert row["audio_ms"] == 12345


def test_a_stored_picture_does_not_make_an_article_look_changed(settings, conn, monkeypatch):
    """`media["file"]` is written after the store, by the picture fetch.

    So the stored copy always carries one and a fresh parse never does.
    Compared, no article with a picture in it could ever be left alone, and
    re-parsing the library would replace them for ever.
    """
    from textcast import pictures, service

    monkeypatch.setattr(pictures, "_download", lambda url: (b"\x89PNG\r\n\x1a\n" + b"0" * 32, ".png"))
    html = (
        "<html><head><title>A charted note</title></head><body><article>"
        "<h1>A charted note</h1>"
        f"<p>{'Sentence about the market. ' * 14}</p>"
        '<figure><img src="https://images.test/chart.png" width="900">'
        "<figcaption>Inflation, year on year</figcaption></figure>"
        f"<p>{'Another sentence entirely. ' * 14}</p>"
        "</article></body></html>"
    )
    stored = service.ingest(html=html, url="https://x.test/r", build=False, settings=settings)
    figure = [b for _s, b in db.load_article(stored.article_id, conn).blocks() if b.media][0]
    assert figure.media["file"], "the picture was not stored"

    assert service.reparse(stored.article_id, settings=settings).unchanged
