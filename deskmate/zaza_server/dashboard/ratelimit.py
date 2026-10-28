"""Manager login rate limiting (in memory; one server process).

Failed sign-ins are counted in two sliding windows:

- per client address (the real address, taken from the trusted reverse
  proxy only — see ``http_security.py``), and
- per submitted username, whether or not the account exists, so the limit
  says nothing about which usernames are real and guessing one account
  from many addresses is slowed down too.

While either count is at its limit the login is refused with HTTP 429 and
``Retry-After`` before any password is checked. Each attempt is counted
atomically *before* its password check (:meth:`LoginRateLimiter.acquire`),
so a burst of parallel requests can't get past the limit together; a
successful sign-in clears both counts. State is bounded: entries expire with the window and the
total number of tracked keys is capped (oldest dropped first). Passwords
are never stored or logged here.

Scope (Phase 10F-A): this limiter is per PROCESS. It is effective for the
legacy single-server deployment (one process behind Caddy). It is NOT a
global limit across Cloud Run instances, and the legacy manager login is
not served on Cloud Run at all. The future SaaS login must use a limiter
with shared state (a database- or edge-backed implementation of
:class:`RateLimiter`) or an edge control such as Cloud Armor.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LoginLimits:
    max_failures_per_client: int = 10
    max_failures_per_username: int = 20
    window_seconds: int = 900
    max_tracked_keys: int = 10_000


class RateLimiter(Protocol):
    """The boundary a login route depends on. ``client`` is an opaque key
    from ``http_security.external_request(...).client`` (the constant
    "unverified" where the platform hides the client), ``username`` the
    submitted name. Implementations: :class:`LoginRateLimiter` (in memory,
    per process); a shared-state implementation for the SaaS login later."""

    def acquire(self, client: str, username: str) -> tuple[int | None, bool]: ...
    def retry_after(self, client: str, username: str) -> int | None: ...
    def failure(self, client: str, username: str) -> bool: ...
    def success(self, client: str, username: str) -> None: ...


def _username_key(username: str) -> str:
    return (username or "").strip().lower()[:64]


class LoginRateLimiter:
    def __init__(self, limits: LoginLimits | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits or LoginLimits()
        self.clock = clock
        self._failures: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _keys(self, client: str, username: str) -> list[tuple[tuple[str, str], int]]:
        keys = [(("client", client or "unknown"), self.limits.max_failures_per_client)]
        if name := _username_key(username):
            keys.append((("username", name), self.limits.max_failures_per_username))
        return keys

    def _expire(self, now: float) -> None:
        """Drop entries whose newest failure is outside the window. The dict
        is ordered by last failure, so only the front needs checking."""
        cutoff = now - self.limits.window_seconds
        while self._failures:
            key, times = next(iter(self._failures.items()))
            if times and times[-1] > cutoff:
                break
            del self._failures[key]

    def _wait(self, now: float, client: str, username: str) -> int | None:
        wait = 0.0
        for key, limit in self._keys(client, username):
            times = self._failures.get(key)
            if times is None:
                continue
            while times and times[0] <= now - self.limits.window_seconds:
                times.popleft()
            if len(times) >= limit:
                wait = max(wait, times[0] + self.limits.window_seconds - now)
        return max(1, math.ceil(wait)) if wait > 0 else None

    def _record(self, now: float, client: str, username: str) -> bool:
        reached = False
        for key, limit in self._keys(client, username):
            times = self._failures.pop(key, None)
            if times is None:
                times = deque(maxlen=limit)
            times.append(now)
            self._failures[key] = times  # re-inserted at the end: most recent
            reached = reached or len(times) >= limit
        while len(self._failures) > self.limits.max_tracked_keys:
            self._failures.popitem(last=False)
        return reached

    def acquire(self, client: str, username: str) -> tuple[int | None, bool]:
        """Atomically check and count one sign-in attempt.

        Returns ``(wait, reached)``: ``wait`` is the seconds until another
        attempt is allowed (the attempt is refused and NOT counted), or
        ``None`` if it may proceed — it is then already counted as a
        failure, and :meth:`success` clears it. Counting before the password
        check means parallel requests can't slip past the limit together.
        ``reached`` is ``True`` when this attempt used up a limit."""
        with self._lock:
            now = self.clock()
            self._expire(now)
            wait = self._wait(now, client, username)
            if wait is not None:
                return wait, False
            return None, self._record(now, client, username)

    def retry_after(self, client: str, username: str) -> int | None:
        """Seconds until another attempt is allowed, or ``None`` if allowed now."""
        with self._lock:
            now = self.clock()
            self._expire(now)
            return self._wait(now, client, username)

    def failure(self, client: str, username: str) -> bool:
        """Record a failed sign-in. ``True`` if this failure reached a limit."""
        with self._lock:
            now = self.clock()
            self._expire(now)
            return self._record(now, client, username)

    def success(self, client: str, username: str) -> None:
        with self._lock:
            for key, _ in self._keys(client, username):
                self._failures.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)
