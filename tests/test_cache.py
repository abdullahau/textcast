"""The block cache: what a sweep may forget, extended to cover word timings.

`.i16` (the audio) and `.words.json` (its word-level timings) share one
cache key, because both are a function of exactly the same inputs. A sweep
that only understood the first suffix would delete every word-timing file
on its very next run, regardless of whether the audio beside it was still
wanted -- these tests exist because that was true until `cache._reachable`
learned the second shape.
"""

from __future__ import annotations

from textcast import cache
from textcast.audio import CACHE_SUFFIX, WORDS_CACHE_SUFFIX
from textcast.service import ingest

NOTE = "# One\n\nA paragraph with enough words to be a real block of text here."


def _write(settings, key: str, suffix: str) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / f"{key}{suffix}").write_bytes(b"\x00" * 8)


def test_a_wanted_key_keeps_both_its_audio_and_its_word_timings(conn, settings):
    result = ingest(text=NOTE, title="Kept", build=False)
    (key,) = cache.cache_keys(result.article_id, conn, settings)

    _write(settings, key, CACHE_SUFFIX)
    _write(settings, key, WORDS_CACHE_SUFFIX)

    removed, _freed = cache.sweep_cache(settings, conn, wanted={key})
    assert removed == 0
    assert (settings.cache_dir / f"{key}{CACHE_SUFFIX}").exists()
    assert (settings.cache_dir / f"{key}{WORDS_CACHE_SUFFIX}").exists()


def test_an_orphaned_key_loses_both_its_audio_and_its_word_timings(conn, settings):
    orphan = "a" * 64
    _write(settings, orphan, CACHE_SUFFIX)
    _write(settings, orphan, WORDS_CACHE_SUFFIX)

    removed, _freed = cache.sweep_cache(settings, conn, wanted=set())
    assert removed == 2
    assert not (settings.cache_dir / f"{orphan}{CACHE_SUFFIX}").exists()
    assert not (settings.cache_dir / f"{orphan}{WORDS_CACHE_SUFFIX}").exists()


def test_a_words_file_with_no_matching_audio_key_is_still_reachable_by_its_own_key(conn, settings):
    # Alignment can fail for a block whose audio synthesised fine, so the two
    # files are not always written together -- but a .words.json is
    # reachable by the same key test regardless of whether its .i16 sibling
    # happens to exist on disk at sweep time.
    key = "b" * 64
    _write(settings, key, WORDS_CACHE_SUFFIX)

    removed, _freed = cache.sweep_cache(settings, conn, wanted={key})
    assert removed == 0
    assert (settings.cache_dir / f"{key}{WORDS_CACHE_SUFFIX}").exists()


def test_cached_renders_lists_both_suffixes_per_key(conn, settings):
    result = ingest(text=NOTE, title="Deletable", build=False)
    (key,) = cache.cache_keys(result.article_id, conn, settings)

    paths = cache.cached_renders(result.article_id, conn, settings)
    names = {p.name for p in paths}
    assert f"{key}{CACHE_SUFFIX}" in names
    assert f"{key}{WORDS_CACHE_SUFFIX}" in names


def test_reachable_rejects_a_file_from_an_older_or_unknown_format(settings):
    stray = settings.cache_dir / "not-a-cache-file.txt"
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    stray.write_text("x")
    assert cache._reachable(stray, wanted={"anything"}) is False
