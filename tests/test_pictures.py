"""Storing the pictures an article cites.

Nothing here reaches the network: `_download` is the seam, and every test
stands in for it. What is worth testing is the bookkeeping around it — where a
file lands, what it is called, when it is not fetched twice, and what happens
to it when the block that wanted it goes away.
"""

from __future__ import annotations

import pytest

from textcast import db, pictures, service
from textcast.document import Article, Block, BlockKind, Section

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
CHART = "https://images.test/chart.png"
PHOTO = "https://images.test/photo.jpg"


def article_with(*srcs: str) -> Article:
    blocks: list[Block] = [Block(kind=BlockKind.PARA, text="The body of it.")]
    for i, src in enumerate(srcs):
        blocks.append(
            Block(kind=BlockKind.FIGURE, text=f"Figure: number {i}", media={"src": src, "alt": ""})
        )
    return Article(title="A charted note", sections=[Section(title="One", blocks=blocks)]).renumber()


@pytest.fixture
def offline(monkeypatch):
    """Stand in for the network, and record what was asked for."""
    asked: list[str] = []

    def fake(url: str):
        asked.append(url)
        return (PNG, ".png") if url.endswith(".png") else (PNG, ".jpg")

    monkeypatch.setattr(pictures, "_download", fake)
    return asked


def test_a_picture_is_stored_beside_the_audio_and_the_block_learns_its_name(
    settings, conn, offline
):
    article_id = db.save_article(article_with(CHART), conn)

    stored = pictures.fetch_for(article_id, settings, conn)

    assert stored == 1
    name = pictures.stored_name(CHART, ".png")
    assert (pictures.images_dir("a-charted-note", settings) / name).read_bytes() == PNG

    figure = [b for _s, b in db.load_article(article_id, conn).blocks() if b.media][0]
    assert figure.media["file"] == name
    # The address it came from is kept, so a later attempt knows where to look.
    assert figure.media["src"] == CHART


def test_the_same_picture_twice_is_one_file_and_one_fetch(settings, conn, offline):
    article_id = db.save_article(article_with(CHART, CHART), conn)

    pictures.fetch_for(article_id, settings, conn)

    assert offline == [CHART], "asked for it twice"
    assert len(list(pictures.images_dir("a-charted-note", settings).iterdir())) == 1


def test_a_second_pass_downloads_nothing(settings, conn, offline):
    """Re-parse rebuilds every block, so none of them remembers its file.

    Without the check against the disk, re-parsing the library would fetch
    every picture in it again to write bytes that are already there.
    """
    article_id = db.save_article(article_with(CHART, PHOTO), conn)
    pictures.fetch_for(article_id, settings, conn)
    offline.clear()

    # A re-parse: same pictures, fresh blocks with no `file` on them.
    db.replace_blocks(article_id, article_with(CHART, PHOTO), conn)
    stored = pictures.fetch_for(article_id, settings, conn)

    assert offline == [], "went back to the network"
    assert stored == 0
    files = [b.media["file"] for _s, b in db.load_article(article_id, conn).blocks() if b.media]
    assert len(files) == 2, "the blocks did not find the stored copies"


def test_a_refused_download_leaves_the_block_pointing_at_the_publication(
    settings, conn, monkeypatch
):
    """A hotlinked picture is worse than a stored one and better than none."""
    monkeypatch.setattr(pictures, "_download", lambda url: None)
    article_id = db.save_article(article_with(CHART), conn)

    assert pictures.fetch_for(article_id, settings, conn) == 0

    figure = [b for _s, b in db.load_article(article_id, conn).blocks() if b.media][0]
    assert figure.media["src"] == CHART
    assert "file" not in figure.media


def test_a_picture_nothing_cites_any_more_is_swept(settings, conn, offline):
    article_id = db.save_article(article_with(CHART, PHOTO), conn)
    pictures.fetch_for(article_id, settings, conn)
    directory = pictures.images_dir("a-charted-note", settings)
    assert len(list(directory.iterdir())) == 2

    # The second figure is edited out of the article.
    db.replace_blocks(article_id, article_with(CHART), conn)
    pictures.fetch_for(article_id, settings, conn)

    assert [p.name for p in directory.iterdir()] == [pictures.stored_name(CHART, ".png")]


def test_deleting_the_article_takes_its_pictures(settings, conn, offline):
    article_id = db.save_article(article_with(CHART), conn)
    pictures.fetch_for(article_id, settings, conn)
    directory = pictures.images_dir("a-charted-note", settings)
    assert directory.is_dir()

    service.delete(article_id, settings)

    assert not directory.exists()
    assert not (settings.media_dir / "a-charted-note").exists()


def test_dropping_the_audio_does_not_drop_the_pictures(settings, conn, offline):
    """The audio can be built again from the blocks. A picture cannot.

    The page it came from may be gone, and `delete_audio` used to unlink
    everything in the media directory without asking what it was.
    """
    article_id = db.save_article(article_with(CHART), conn)
    pictures.fetch_for(article_id, settings, conn)
    audio = settings.media_dir / "a-charted-note" / "section-000.opus"
    audio.write_bytes(b"not really opus")

    service.delete_audio(article_id, settings)

    assert not audio.exists()
    assert (pictures.images_dir("a-charted-note", settings)).is_dir()
    figure = [b for _s, b in db.load_article(article_id, conn).blocks() if b.media][0]
    assert figure.media["file"]


def test_the_extension_comes_off_the_content_type_not_the_address():
    """An FT address ends in `?source=next-article` and has no extension."""
    ft = "https://images.ft.com/v3/image/raw/ftcms%3Ae69?source=next-article&width=1440"

    assert pictures._suffix_for(ft, "image/jpeg; charset=binary") == ".jpg"
    assert pictures._suffix_for(ft, "") == ".img"
    assert pictures._suffix_for("https://x.test/a.webp", "") == ".webp"


def test_the_reader_serves_a_stored_picture_and_refuses_a_path_climb(
    settings, conn, offline, monkeypatch
):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textcast.web import app as web

    monkeypatch.setattr(web, "settings", settings)
    monkeypatch.setattr(web, "_voices", lambda *a: [])
    article_id = db.save_article(article_with(CHART), conn)
    pictures.fetch_for(article_id, settings, conn)
    name = pictures.stored_name(CHART, ".png")

    with TestClient(web.app) as client:
        got = client.get(f"/media/a-charted-note/images/{name}")
        assert got.status_code == 200
        assert got.content == PNG
        assert got.headers["content-type"] == "image/png"
        assert client.get("/media/a-charted-note/images/nothing.png").status_code == 404

        body = client.get("/a/a-charted-note").text
        assert f'src="/media/a-charted-note/images/{name}"' in body
        assert CHART not in body, "still hotlinking the publication"
