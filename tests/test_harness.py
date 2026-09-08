"""Tests for the test harness itself.

`free_port` is the only part of `conftest` with logic worth checking. It is
here rather than in `conftest` because a helper that hands two xdist workers
the same port fails as a rare "address already in use" in some *other* file,
which is the hardest kind of failure to trace back.
"""

from __future__ import annotations

import socket

import pytest
from conftest import _PORT_BASE, _PORT_CEILING, _PORT_SLICE, _PORT_SLOTS, free_port


@pytest.mark.parametrize("worker", ["gw0", "gw1", "gw7"])
def test_each_worker_draws_from_its_own_range(worker, monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    index = int(worker[2:])
    low = _PORT_BASE + index * _PORT_SLICE

    port = free_port()

    assert low <= port < low + _PORT_SLICE


@pytest.mark.parametrize("worker", ["gw0", "gw3", f"gw{_PORT_SLOTS}", "gw999"])
def test_no_worker_is_given_a_port_the_kernel_may_also_hand_out(worker, monkeypatch):
    """Slices stay under the ephemeral range, however many workers there are.

    A port above it can be taken by any outgoing connection in the gap
    between the probe closing and the server binding, which is the race the
    slices exist to remove.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)

    port = free_port()

    assert _PORT_BASE <= port < _PORT_CEILING


def test_two_workers_cannot_be_handed_the_same_port(monkeypatch):
    """The whole point: the ranges may not overlap."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    first = {free_port() for _ in range(50)}
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    second = {free_port() for _ in range(50)}

    assert not first & second


def test_a_serial_run_still_gets_a_port(monkeypatch):
    """Without xdist there is no worker variable, and `gw0` is assumed."""
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    port = free_port()

    assert _PORT_BASE <= port < _PORT_BASE + _PORT_SLICE


def test_a_port_already_taken_is_not_handed_out(monkeypatch):
    """A server from a previous run may not have released its port yet."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    taken = free_port()

    with socket.socket() as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", taken))
        held.listen(1)
        assert all(free_port() != taken for _ in range(200))
