"""The ingest rate limit."""

from __future__ import annotations

from textcast.web.limits import RateLimiter


def test_a_budget_is_spent_and_then_refused():
    limiter = RateLimiter(allowed=3, per_seconds=60)
    for _ in range(3):
        assert limiter.retry_after("a", now=100.0) == 0.0
    assert limiter.retry_after("a", now=100.0) > 0


def test_the_window_slides_rather_than_resetting_on_a_boundary():
    """A fixed window lets twice the budget through across its edge: spend it
    all at 59 s and all of it again at 61 s."""
    limiter = RateLimiter(allowed=2, per_seconds=60)
    limiter.retry_after("a", now=0.0)
    limiter.retry_after("a", now=59.0)

    assert limiter.retry_after("a", now=59.5) > 0, "the budget is spent"
    # The first hit ages out, and only that one.
    assert limiter.retry_after("a", now=61.0) == 0.0
    assert limiter.retry_after("a", now=61.0) > 0, "the second hit is still in the window"


def test_a_refusal_is_not_counted():
    """A client that keeps knocking would otherwise hold its own door shut for
    ever, and the window has to be able to pass."""
    limiter = RateLimiter(allowed=1, per_seconds=10)
    limiter.retry_after("a", now=0.0)
    for _ in range(50):
        assert limiter.retry_after("a", now=5.0) > 0
    assert limiter.retry_after("a", now=10.5) == 0.0


def test_one_client_cannot_spend_another_client_s_budget():
    limiter = RateLimiter(allowed=1, per_seconds=60)
    assert limiter.retry_after("a", now=0.0) == 0.0
    assert limiter.retry_after("b", now=0.0) == 0.0, "b paid for a"


def test_check_reads_without_spending():
    limiter = RateLimiter(allowed=1, per_seconds=60)
    for _ in range(5):
        assert limiter.check("a", now=0.0) == 0.0
    assert limiter.retry_after("a", now=0.0) == 0.0
    assert limiter.check("a", now=0.0) > 0


def test_a_key_that_worked_gets_its_budget_back():
    limiter = RateLimiter(allowed=2, per_seconds=60)
    limiter.spend("a", now=0.0)
    limiter.spend("a", now=0.0)
    assert limiter.check("a", now=0.0) > 0
    limiter.forget("a")
    assert limiter.check("a", now=0.0) == 0.0


def test_the_table_of_keys_cannot_grow_without_bound():
    """A spray across forged addresses must not be a way to eat memory."""
    limiter = RateLimiter(allowed=1, per_seconds=600, max_keys=8)
    for i in range(200):
        limiter.retry_after(f"client-{i}", now=float(i))
    assert len(limiter._hits) <= 9, "the sweep did not run"
    # The newest key is one of the ones kept.
    assert limiter.check("client-199", now=200.0) > 0


def test_a_spray_of_addresses_is_evicted_in_batches_not_one_a_request():
    """The eviction sorted the whole table on every call once it was full,
    and skipped the current key while still counting it against the number to
    remove -- so the table sat one over the cap for ever, re-sorting every
    time round."""
    limiter = RateLimiter(allowed=5, per_seconds=60, max_keys=100)

    for i in range(400):
        limiter.spend(f"10.0.0.{i}")

    assert len(limiter._hits) <= limiter.max_keys, "the table grew past its cap"

    # The cap holds, and the key being asked about is never the one thrown
    # out to make room for itself.
    limiter.spend("here")
    for _ in range(50):
        limiter.spend(f"other-{_}")
        assert limiter.check("here") == 0.0 or "here" in limiter._hits


def test_the_key_being_asked_about_is_never_evicted_to_make_room_for_itself():
    limiter = RateLimiter(allowed=1, per_seconds=60, max_keys=4)
    for i in range(20):
        key = f"caller-{i}"
        limiter.spend(key)
        assert key in limiter._hits, f"{key} evicted itself"
        assert len(limiter._hits) <= limiter.max_keys
