from __future__ import annotations

import pytest

from textcast import db
from textcast.audio import AudioManifest, BlockTiming, SectionAudio
from textcast.document import Article, Block, BlockKind, Section


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


def test_a_detected_newsletter_becomes_an_ordinary_tag(conn):
    """Newsletters are not a separate concept; they are just a tag."""
    article_id = db.save_article(make_article(), conn)
    assert db.tags_for(article_id, conn) == ["Money Stuff"]

    rows = db.list_tags(conn)
    assert [r["name"] for r in rows] == ["Money Stuff"]
    assert rows[0]["articles"] == 1
    assert [a["id"] for a in db.list_articles(conn, tag="Money Stuff")] == [article_id]


def test_tags_are_replaced_wholesale_and_created_on_demand(conn):
    article_id = db.save_article(make_article(), conn)
    applied = db.set_tags(article_id, ["Finance", " Reading list ", "Finance", ""], conn)

    assert applied == ["Finance", "Reading list"], "trimmed, deduplicated, blanks dropped"
    assert db.tags_for(article_id, conn) == ["Finance", "Reading list"]
    assert "Money Stuff" not in db.tags_for(article_id, conn), "set replaces, it does not add"

    db.add_tag(article_id, "Later", conn)
    assert "Later" in db.tags_for(article_id, conn)
    db.remove_tag(article_id, "Later", conn)
    assert "Later" not in db.tags_for(article_id, conn)


def test_deleting_a_tag_keeps_the_articles(conn):
    article_id = db.save_article(make_article(), conn)
    db.set_tags(article_id, ["Finance"], conn)
    db.delete_tag("Finance", conn)

    assert db.tags_for(article_id, conn) == []
    assert db.get_article(article_id, conn) is not None


def test_build_options_are_per_article(conn):
    first = db.save_article(make_article(), conn)
    other = make_article(title="Another Issue")
    other.sections[0].blocks[0].text = "A different opening paragraph entirely."
    second = db.save_article(other, conn)

    db.set_build_options(first, {"voice": "af_heart", "skip_footnotes": True}, conn)

    assert db.get_build_options(first, conn) == {"voice": "af_heart", "skip_footnotes": True}
    assert db.get_build_options(second, conn) == {}, "settings do not leak between articles"

    # Blanks mean "use the default" and are not stored.
    db.set_build_options(first, {"voice": "", "quote_voice": "bm_george"}, conn)
    assert db.get_build_options(first, conn) == {"quote_voice": "bm_george"}


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

    db.save_position(article_id, section_idx=1, ms=6000, conn=conn)
    position = db.get_position(article_id, conn)
    assert position["ms"] == 6000 and position["section_idx"] == 1

    assert [r["id"] for r in db.continue_listening(conn)] == [article_id]

    # Finished articles drop off the list rather than nagging.
    db.save_position(article_id, section_idx=1, ms=6000, finished=True, conn=conn)
    assert db.continue_listening(conn) == []


def test_a_position_at_the_end_is_finished_even_without_the_flag(conn):
    """The player carries the flag, and a lost one left an article nagging.

    The player's clock runs on the decoded audio and the library's total comes
    from the manifest, so a fully played article can report a position a few
    seconds past its own duration. That showed as "-1:59:57 left" in
    "Continue listening", on an article that had been played to the end.
    """
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)  # 9,000 ms

    db.save_position(article_id, section_idx=1, ms=9003, conn=conn)

    assert db.get_position(article_id, conn)["finished"] == 1, "played to the end"
    assert db.continue_listening(conn) == [], "so it is not still being listened to"


def test_scrubbing_back_from_the_end_is_unfinished_again(conn):
    """The rule reads the position, so it un-reads it too."""
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)
    db.save_position(article_id, section_idx=1, ms=9003, conn=conn)

    db.save_position(article_id, section_idx=0, ms=6000, conn=conn)

    assert db.get_position(article_id, conn)["finished"] == 0
    assert [r["id"] for r in db.continue_listening(conn)] == [article_id]


def test_a_short_note_is_not_finished_five_seconds_in(conn):
    """A flat five-second window is most of a twenty-second note."""
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)  # 9,000 ms

    db.save_position(article_id, section_idx=0, ms=5500, conn=conn)

    assert db.get_position(article_id, conn)["finished"] == 0
    assert [r["id"] for r in db.continue_listening(conn)] == [article_id]


def test_clearing_a_position_takes_the_article_out_of_continue_listening(conn):
    """Stopping an article has to leave nothing behind. A row set back to zero
    would still resume the reader at the top and still read as unfinished."""
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)
    db.save_position(article_id, section_idx=1, ms=6000, conn=conn)
    assert [r["id"] for r in db.continue_listening(conn)] == [article_id]

    db.clear_position(article_id, conn)

    assert db.get_position(article_id, conn) is None
    assert db.continue_listening(conn) == []


def test_completed_asks_the_position_row_not_the_article_status(conn):
    """`article.status` describes the audio and never says "completed". The
    filter reads it off the saved position instead."""
    listened = db.save_article(make_article(), conn)
    unheard = db.save_article(make_article(title="Another Trade"), conn)
    for article_id in (listened, unheard):
        db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)

    db.save_position(listened, section_idx=1, ms=9000, finished=True, conn=conn)
    db.save_position(unheard, section_idx=0, ms=1000, finished=False, conn=conn)

    assert [r["id"] for r in db.list_articles(conn, status="completed")] == [listened]
    assert conn.execute(
        "SELECT status FROM article WHERE id = ?", (listened,)
    ).fetchone()["status"] == "ready", "the article row is untouched"

    ready = [r["id"] for r in db.list_articles(conn, status="ready")]
    assert set(ready) == {listened, unheard}, "completed articles still have audio"


def test_a_completed_article_needs_its_audio_to_still_exist(conn):
    """Dropping the audio leaves the position behind. Without the ready check
    the article would keep claiming it had been listened to."""
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)
    db.save_position(article_id, section_idx=1, ms=9000, finished=True, conn=conn)

    db.forget_audio(article_id, conn)

    assert db.list_articles(conn, status="completed") == []


def test_the_article_list_and_its_count_agree_on_every_filter(conn):
    """They are one helper because they were two, and a count that disagrees
    with the rows under it divides a pager by the wrong total."""
    first = db.save_article(make_article(), conn)
    second = db.save_article(make_article(title="Another Trade", series=None), conn)
    db.save_manifest(first, manifest_for(), audio_bytes=1, conn=conn)
    db.save_position(first, section_idx=1, ms=9000, finished=True, conn=conn)
    db.set_flag(second, "starred", True, conn)

    filters = [
        {},
        {"status": "ready"},
        {"status": "completed"},
        {"status": "new"},
        {"tag": "Money Stuff"},
        {"tag": "Money Stuff", "status": "completed"},
        {"starred": True},
        {"query": "Another"},
        {"archived": True},
    ]
    for kwargs in filters:
        rows = db.list_articles(conn, limit=500, **kwargs)
        assert len(rows) == db.count_articles(conn, **kwargs), kwargs

    assert db.count_articles(conn, status="completed") == 1
    assert db.count_articles(conn, starred=True) == 1


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


def test_forgetting_audio_leaves_the_text_and_takes_the_timings(conn):
    """Media deleted by hand leaves the database pointing at files that are gone."""
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1234, conn=conn)
    assert db.get_article(article_id, conn)["status"] == "ready"

    db.forget_audio(article_id, conn)

    row = db.get_article(article_id, conn)
    assert (row["status"], row["audio_ms"], row["audio_bytes"]) == ("new", 0, 0)
    assert conn.execute(
        "SELECT COUNT(*) c FROM block WHERE article_id = ? AND start_ms IS NOT NULL", (article_id,)
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM section WHERE article_id = ? AND file IS NOT NULL", (article_id,)
    ).fetchone()["c"] == 0
    assert db.load_article(article_id, conn).word_count > 0, "the text is untouched"


def test_a_hyphen_in_a_search_is_not_a_syntax_error(conn):
    """Raw input went to FTS5 MATCH, so searching for a word in one of your own
    titles — Drug-Trial, roll-up — answered with a 500."""
    db.save_article(make_article(title="Drug-Trial Stock Sale"), conn)

    for query in ("Drug-Trial", "roll-up", "AT&T", "C++", "levine OR", 'say "hello"', "NEAR("):
        db.search(query, conn)  # must not raise

    assert [h["slug"] for h in db.search("Drug-Trial", conn)] == ["drug-trial-stock-sale"]


def test_search_covers_the_article_as_well_as_its_blocks(conn):
    """Title, byline, publication and tags are not blocks, so block_fts could
    never find them however the query was written."""
    article_id = db.save_article(
        make_article(title="A Quiet Week", series=None), conn
    )
    conn.execute("UPDATE article SET author='Matt Levine', source='Bloomberg' WHERE id=?", (article_id,))
    db.set_tags(article_id, ["Money Stuff"], conn)

    for query in ("Matt Levine", "Bloomberg", "Money Stuff", "Quiet Week"):
        kinds = {h["kind"] for h in db.search(query, conn)}
        assert "article" in kinds, f"{query!r} found no article-level hit"

    hit = db.search("Matt Levine", conn)[0]
    assert hit["block_id"] is None, "an article hit opens at the top, not at a block"
    assert "<mark>Matt Levine</mark>" in hit["snippet"]


def test_a_search_still_finds_the_words_inside_a_block(conn):
    article_id = db.save_article(make_article(), conn)
    db.save_manifest(article_id, manifest_for(), audio_bytes=1, conn=conn)

    hits = [h for h in db.search("biotech", conn) if h["kind"] != "article"]

    assert hits, "block text is still indexed"
    assert hits[0]["block_id"] and hits[0]["start_ms"] is not None


def test_rules_survive_a_round_trip_through_a_file(conn):
    """The rules are the part worth carrying between machines."""
    db.add_pronunciation("word", "SOFR", "sofer", conn, note="mine")
    db.add_pronunciation("word", "LIBOR", "", conn, misaki="lˈIbɔɹ")
    before = db.export_pronunciations(conn)

    conn.execute("DELETE FROM pronunciation")
    result = db.import_pronunciations(before, conn)

    assert result["added"] == len(before["rules"]) and result["skipped"] == 0
    assert db.export_pronunciations(conn) == before, "what came out went back in unchanged"


def test_importing_the_same_file_twice_changes_nothing(conn):
    payload = db.export_pronunciations(conn)

    first = db.import_pronunciations(payload, conn)
    second = db.import_pronunciations(payload, conn)

    assert first["added"] == 0 and first["updated"] == len(payload["rules"])
    assert second["updated"] == len(payload["rules"]) and second["added"] == 0
    assert len(db.export_pronunciations(conn)["rules"]) == len(payload["rules"])


def test_importing_merges_by_default_and_replaces_when_asked(conn):
    conn.execute("DELETE FROM pronunciation")  # the seeded builtins are not the subject
    db.add_pronunciation("word", "GAAP", "gap", conn)
    incoming = {"rules": [{"kind": "word", "pattern": "EBITDA", "replacement": "ee bitda"}]}

    db.import_pronunciations(incoming, conn)
    assert {r["pattern"] for r in db.export_pronunciations(conn)["rules"]} == {"GAAP", "EBITDA"}

    db.import_pronunciations(incoming, conn, replace=True)
    assert {r["pattern"] for r in db.export_pronunciations(conn)["rules"]} == {"EBITDA"}


def test_a_rule_that_makes_no_sense_is_skipped_not_fatal(conn):
    conn.execute("DELETE FROM pronunciation")
    payload = {"rules": [
        {"kind": "word", "pattern": "OK", "replacement": "okay"},
        {"kind": "nonsense", "pattern": "X", "replacement": "y"},
        {"kind": "word", "pattern": "   ", "replacement": "y"},
        "not a rule at all",
    ]}

    result = db.import_pronunciations(payload, conn)

    assert result == {"added": 1, "updated": 0, "skipped": 3}


def test_a_file_with_no_rules_in_it_says_so(conn):
    import pytest as _pytest

    with _pytest.raises(ValueError, match="no list of rules"):
        db.import_pronunciations({"textcast": "pronunciations", "rules": "nope"}, conn)


def test_the_count_and_the_page_read_the_same_filter(conn):
    """Two filters written apart is a pager that divides the wrong total."""
    for i in range(5):
        doc = Article(title=f"Piece {i}", sections=[Section(title="One", blocks=[
            Block(kind=BlockKind.PARA, text="The body of it."),
        ])]).renumber()
        article_id = db.save_article(doc, conn)
        if i < 2:
            db.set_tags(article_id, ["Money Stuff"], conn)

    assert db.count_articles(conn) == 5
    assert db.count_articles(conn, tag="Money Stuff") == 2
    assert len(db.list_articles(conn, tag="Money Stuff", limit=25)) == 2
    assert db.count_articles(conn, status="ready") == 0


# --------------------------------------------------------------------------
# which articles a rule would change
# --------------------------------------------------------------------------


def _matching_the_slow_way(rule, conn):
    """What `articles_matching` did before SQL narrowed it: every block."""
    pattern = rule.compile()
    hits, seen = [], set()
    for row in conn.execute(
        "SELECT b.article_id, b.text FROM block b JOIN article a ON a.id = b.article_id"
        " WHERE a.status = 'ready' AND a.archived = 0 ORDER BY b.article_id"
    ):
        if row["article_id"] in seen:
            continue
        if pattern.search(row["text"]):
            seen.add(row["article_id"])
            hits.append(row["article_id"])
    return hits


def _ready(conn, title, *texts):
    from textcast.document import Article, Block, BlockKind, Section

    doc = Article(title=title, source="Bench", sections=[Section(title="S", blocks=[
        Block(kind=BlockKind.PARA, text=t) for t in texts
    ])]).renumber()
    article_id = db.save_article(doc, conn)
    conn.execute("UPDATE article SET status = 'ready' WHERE id = ?", (article_id,))
    return article_id


def test_narrowing_a_rule_in_sql_finds_the_same_articles(conn):
    """A word or a phrase rule is a literal inside guards, so the literal has
    to be in the text. `LIKE` is a superset of what the pattern would match,
    so SQLite can drop the rows that cannot match before Python sees them.

    A shortcut that changes an answer is not a shortcut. This compares it
    against the exhaustive scan rather than asserting on the result, and the
    cases are the ones where a literal test could go wrong: case, a substring
    of a longer word, a LIKE wildcard, and a regex with no literal at all.
    """
    from textcast.pronounce import Rule

    _ready(conn, "One", "The trade settled at 12x EBITDA today.")
    _ready(conn, "Two", "ebitda in lower case, and EBITDAX which is a longer word.")
    _ready(conn, "Three", "A 100% gain and a snake_case name.")
    _ready(conn, "Four", "Nothing here matches any of it.")

    rules = [
        Rule(kind="word", pattern="EBITDA", replacement="x"),
        Rule(kind="word", pattern="EBITDA", replacement="x", ignore_case=True),
        Rule(kind="word", pattern="ebitda", replacement="x", ignore_case=True),
        Rule(kind="phrase", pattern="12x EBITDA", replacement="x"),
        Rule(kind="phrase", pattern="100%", replacement="x"),
        Rule(kind="phrase", pattern="snake_case", replacement="x"),
        Rule(kind="regex", pattern=r"EBITDAX?(?!\w)", replacement="x"),
        Rule(kind="word", pattern="absent", replacement="x"),
    ]
    for rule in rules:
        fast = [row["id"] for row in db.articles_matching(rule, conn)]
        slow = _matching_the_slow_way(rule, conn)
        assert sorted(fast) == sorted(slow), f"{rule.kind} {rule.pattern!r}"


def test_a_like_wildcard_in_a_pattern_means_itself():
    """`%` and `_` are LIKE's own syntax, and a pattern may contain either.

    Not a correctness fix: a wildcard only ever *widens* a LIKE, so the answer
    stays right because the regex has the last word. It is what keeps the
    narrowing worth doing — an unescaped `%` in "100%" matches every block
    with "100" anywhere, and a rule whose pattern is a bare "%" would hand
    Python the whole library again.
    """
    from textcast.db import _like_literal

    assert _like_literal("100%") == r"100\%"
    assert _like_literal("snake_case") == r"snake\_case"
    assert _like_literal(r"back\slash") == r"back\\slash"
    assert _like_literal("EBITDA") == "EBITDA"

