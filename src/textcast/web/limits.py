"""A small per-client rate limiter, for the one route the internet can reach.

`/api/ingest` is the only route that takes a credential in a *body* from
anywhere, and it does real work per call: it parses, it fetches pictures, and
given a URL it makes the server go and get a page. Nothing counted, so
guessing the ingest key was free and a leaked one was an open tap.

In the process, and deliberately not in the database. A limiter that writes a
row per attempt hands the thing it is defending against a way to make the app
do work, which is the fault it exists to stop. The cost is that two web
workers keep two counts — the budget is then per worker, not per app. One
uvicorn process is what ships, and a limit that is loose by a factor of two is
still the difference between "bounded" and "free".

The key is `request.client.host`. uvicorn runs with `--proxy-headers`, so
behind the reverse proxy that is the real client and not the proxy — which
matters more than it looks: with every request landing on one key, a stranger
guessing keys would lock the owner out of their own bookmarklet.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Fixed budget in a sliding window, counted per key.

    ``allowed`` attempts in any ``per_seconds``. A refused attempt is not
    counted: a client that keeps knocking would otherwise hold its own door
    shut for ever, and the window has to be able to pass.
    """

    def __init__(self, allowed: int, per_seconds: float, *, max_keys: int = 4096) -> None:
        self.allowed = allowed
        self.per_seconds = per_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Seconds the caller must wait, or 0.0 when it may proceed.

        A successful call is recorded here, so the caller does not have to
        remember to; ``check`` below is the read-only form.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._sweep(key, now)
            if len(hits) < self.allowed:
                hits.append(now)
                return 0.0
            return max(0.0, self.per_seconds - (now - hits[0]))

    def check(self, key: str, *, now: float | None = None) -> float:
        """The same answer, without spending anything.

        Used where a route wants to refuse early — before reading a body, or
        before comparing a secret — and count only what it goes on to do.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._sweep(key, now)
            if len(hits) < self.allowed:
                return 0.0
            return max(0.0, self.per_seconds - (now - hits[0]))

    def spend(self, key: str, *, now: float | None = None) -> None:
        """Count one attempt whatever the budget says."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(key, now).append(now)

    def forget(self, key: str) -> None:
        """Give a key its whole budget back. What a correct password does."""
        with self._lock:
            self._hits.pop(key, None)

    def forget_all(self) -> None:
        with self._lock:
            self._hits.clear()

    def _sweep(self, key: str, now: float) -> deque[float]:
        """The live hits for one key, with the expired ones dropped.

        Called under the lock. Also caps how many keys are tracked at all, so
        a spray across forged addresses cannot grow this without bound: the
        keys with the oldest last hit go first, because they are the ones with
        the least left to say.
        """
        cutoff = now - self.per_seconds
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(self._hits) > self.max_keys:
            for stale, _ in sorted(
                ((k, v[-1] if v else 0.0) for k, v in self._hits.items()),
                key=lambda pair: pair[1],
            )[: len(self._hits) - self.max_keys]:
                if stale != key:
                    self._hits.pop(stale, None)
        return hits


#: Wrong or missing credentials on the ingest route. Small, because a person
#: typing a key wrong ten times in a quarter of an hour has a different
#: problem, and because this is the number that makes guessing expensive.
INGEST_ATTEMPTS = RateLimiter(allowed=10, per_seconds=900)

#: Accepted calls. Larger, and it exists for a different reason: a key that
#: has leaked should not be able to make the server fetch and parse for ever.
#: A batch of files is one request, so this is twenty *articles-worth* of
#: work per five minutes only in the worst case.
INGEST_WORK = RateLimiter(allowed=20, per_seconds=300)


def client_key(request) -> str:
    """Who is asking. Falls back to a single shared bucket, not to no limit."""
    client = getattr(request, "client", None)
    return (client.host if client and client.host else "unknown") or "unknown"


def reset_all() -> None:
    """Empty every budget. For tests, and for nothing else."""
    INGEST_ATTEMPTS.forget_all()
    INGEST_WORK.forget_all()
