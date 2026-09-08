"""Shared test setup.

One fixture owns the data directory, so no test can point the database at a
path the settings do not know about. That mismatch bit once: ``normalize``
read the seeded rules from ``settings.db_path`` while the test had written to
a file of its own, and the rules silently did nothing.
"""

from __future__ import annotations

import os
import random
import socket

import pytest

from textcast import db, pronounce
from textcast.settings import Settings, get_settings

#: The per-worker port slices, kept *below* the ephemeral range the kernel
#: hands out for outgoing connections -- 32768 on this box, and the lowest
#: default in use anywhere. A port above it could be taken from under the
#: probe by any unrelated socket between the probe and the server binding it.
#: 20000 clears the registered services that matter in practice.
_PORT_BASE = 20000
_PORT_CEILING = 32768
_PORT_SLICE = 200
#: 63 slices fit under the ceiling, which is more workers than `-n auto` asks
#: for on any box this runs on. A larger one wraps and two workers share a
#: slice -- still correct, because each port is probed, just no longer proof
#: against the race on its own.
_PORT_SLOTS = (_PORT_CEILING - _PORT_BASE) // _PORT_SLICE


def free_port() -> int:
    """A port no other xdist worker will pick.

    The obvious version binds port 0, reads the port back and closes the
    socket -- and then hands that number to a server that binds it a moment
    later. Nothing holds the port in between. Serially the gap never
    mattered; with `-n auto` two workers stand up their own uvicorn at the
    same time, and a suite that fails once a fortnight on "address already
    in use" is worse than no parallelism at all.

    So each worker draws from its own slice and the ranges cannot overlap.
    Within a slice the port is still probed, because a previous run's server
    may not have released it yet.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    index = int(worker[2:]) if worker[2:].isdigit() else 0
    low = _PORT_BASE + (index % _PORT_SLOTS) * _PORT_SLICE

    for _ in range(_PORT_SLICE):
        port = random.randrange(low, low + _PORT_SLICE)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"no free port in {low}-{low + _PORT_SLICE}")


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
