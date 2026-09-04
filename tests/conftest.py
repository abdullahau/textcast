"""Shared test setup.

One fixture owns the data directory, so no test can point the database at a
path the settings do not know about. That mismatch bit once: ``normalize``
read the seeded rules from ``settings.db_path`` while the test had written to
a file of its own, and the rules silently did nothing.
"""

from __future__ import annotations

import pytest

from textcast import db, pronounce
from textcast.settings import Settings, get_settings


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    """A private data directory, with the settings pointed at it."""
    monkeypatch.setenv("TEXTCAST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEXTCAST_WORKERS", "0")
    current = get_settings(refresh=True)
    current.ensure_dirs()
    db.close()
    pronounce.invalidate()
    yield current
    db.close()
    pronounce.invalidate()
    get_settings(refresh=True)


@pytest.fixture
def conn(settings):
    """An initialised database at the path the app itself would use."""
    return db.init(settings.db_path)


@pytest.fixture(autouse=True)
def _fresh_budgets():
    """Give every test the whole rate limit.

    The ingest budgets live in the process and outlive a test, so without this
    the twenty-first ingest in a run fails wherever it happens to fall — and
    which test that is depends on the order they ran in.
    """
    from textcast.web import limits

    limits.reset_all()
    yield
    limits.reset_all()
