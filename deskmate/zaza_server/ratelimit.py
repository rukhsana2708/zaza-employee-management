"""Sync API rate limiting (design: docs/zaza/SYNC_RATE_LIMIT_HSTS_DESIGN.md, revision 2).

Cloud Armor is unavailable (project quota 0), so the protection is in the application. There are three layers.

**1. Authenticated device limit (the primary limit).** Key: a one-way digest of (organization, device), taken from the
control-plane route row that is read anyway to find the tenant; never the public IP and never the token. It runs after the
route lookup and before any tenant database is opened. Every authenticated request passes, in this order:

- the coarse per-IP safeguard (below);
- the **local emergency burst guard**: 40 requests per 10 s per device, per process. It can only *reject*. A request it
  admits is still counted by the shared counter, so it can never create an uncounted bypass;
- the **shared limit**: 120 requests per aligned 60 s window per device, counted in PostgreSQL for EVERY request that the
  burst guard admitted. Database time decides the window, so every Cloud Run instance agrees.

The shared limit is a FIXED window. It guarantees at most 120 admitted requests inside each database-aligned window; it is
NOT a rolling-window guarantee. A burst that straddles a window boundary can reach up to about 240 requests inside any
rolling 60 s span (120 at the end of one window plus 120 at the start of the next).

**2. Invalid or unauthenticated traffic, per verified client IP: PROCESS-LOCAL.** 20 authentication failures per 60 s and
10 per 10 s, per IP, per instance. Every Cloud Run instance keeps its own table, so with up to 3 instances the effective
limit for one IP is anywhere from 20 up to about 60 failures a minute depending on how the load balancer spreads the
requests, and the counters reset when an instance restarts. It is explicitly NOT a global 20 per minute. It is a stopgap until
Cloud Armor is available: its job is to stop one address from generating unlimited control-database reads, and a failed
request deliberately causes no database write. A small cache of recently authenticated routing digests lets known-good
credentials bypass it, so a hostile user on a shared public IP cannot lock out legitimate agents.

**3. Coarse safeguard for authenticated traffic per IP: PROCESS-LOCAL**, 1,800 requests per 60 s per IP per instance. It does
not replace the per-device limit.

IP-based layers need a VERIFIED client address (see ``http_security.trusted_client_ip``). With none, they are skipped and the
device limit still works.

Modes: ``off`` (nothing), ``shadow`` (decide and record, never reject) and ``enforce`` (HTTP 429). The DEVICE limiter and
the per-IP limiters have SEPARATE modes (``ZAZA_SYNC_LIMIT_MODE`` and ``ZAZA_SYNC_LIMIT_IP_MODE``): the IP limiters default to
``shadow`` whenever the limiter is on, and are switched to ``enforce`` only after the trusted client address has been verified
against Cloud Logging. If the shared counter fails, the request is served (a limiter fault must never lose telemetry), a local
per-device ceiling applies, and a warning is logged.

Nothing here logs or returns a token, a device id, an organization id, a raw IP address or a counter value to the client.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import os
import random
import threading
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .config import ConfigError

logger = logging.getLogger("zaza_server.ratelimit")

ENV_MODE = "ZAZA_SYNC_LIMIT_MODE"
ENV_IP_MODE = "ZAZA_SYNC_LIMIT_IP_MODE"
MODES = ("off", "shadow", "enforce")
DEVICE_BUCKET = "sync_device"  # login_attempt_counters.bucket (control migration c0004)
MAX_RETRY_AFTER = 60  # seconds, the most the server ever asks a client to wait
RATE_LIMIT_ERROR = "rate limit exceeded"  # the only text a client ever sees

_DEVICE_KEY_DOMAIN = b"zaza-sync-device-v1\x00"
_CLEANUP_EVERY = 200  # opportunistic expired-row cleanup: about 1 shared count in this many


class RateLimitExceeded(Exception):
    """Refuse the request with HTTP 429. ``retry_after`` is whole seconds until the request is expected to be eligible."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(RATE_LIMIT_ERROR)
        self.retry_after = retry_after


class CounterStore(Protocol):
    def count_attempt(self, bucket: str, key_hash: str, window_seconds: int) -> tuple[int, int]:
        """Atomically count one attempt in the CURRENT database-aligned window: (attempts so far, seconds left)."""


@dataclass(frozen=True)
class SyncLimitSettings:
    mode: str = "off"  # the DEVICE limiter (burst guard + shared counter) and the whole limiter's on/off switch
    ip_mode: str = "shadow"  # the per-IP limiters; only effective while ``mode`` is not off. Enforce only after verification.
    # authenticated device (shared, PostgreSQL, fixed window)
    device_window_seconds: int = 60
    device_per_window: int = 120
    # local emergency burst guard, per device per process (rejection only)
    burst_window_seconds: int = 10
    burst_limit: int = 40
    # invalid / unauthenticated traffic, per verified IP, process-local
    ip_fail_window_seconds: int = 60
    ip_fail_per_window: int = 20
    ip_fail_burst_window_seconds: int = 10
    ip_fail_burst_limit: int = 10
    # coarse safeguard for authenticated traffic, per verified IP, process-local, fixed 60 s window
    ip_coarse_per_minute: int = 1800
    # used only while the shared counter is failing (local, per device, fixed 60 s window)
    fallback_device_per_window: int = 240
    known_good_size: int = 10_000
    known_good_ttl_seconds: int = 120
    max_tracked_devices: int = 20_000
    max_tracked_ips: int = 50_000

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ConfigError(f"{ENV_MODE} must be one of {', '.join(MODES)}")
        if self.ip_mode not in MODES:
            raise ConfigError(f"{ENV_IP_MODE} must be one of {', '.join(MODES)}")
        for name in ("device_window_seconds", "device_per_window", "burst_window_seconds", "burst_limit",
                     "ip_fail_window_seconds", "ip_fail_per_window", "ip_fail_burst_window_seconds",
                     "ip_fail_burst_limit", "ip_coarse_per_minute", "fallback_device_per_window",
                     "known_good_size", "known_good_ttl_seconds", "max_tracked_devices", "max_tracked_ips"):
            if int(getattr(self, name)) < 1:
                raise ConfigError(f"sync limit setting {name} must be a positive integer")
        if self.device_window_seconds != 60:
            raise ConfigError("the shared device window is fixed at 60 seconds")

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    @property
    def ip_active(self) -> bool:
        return self.enabled and self.ip_mode != "off"

    @property
    def ip_enforcing(self) -> bool:
        return self.enabled and self.ip_mode == "enforce"


_ENV_OVERRIDES = {
    "ZAZA_SYNC_LIMIT_DEVICE_PER_MINUTE": "device_per_window",
    "ZAZA_SYNC_LIMIT_BURST_PER_10S": "burst_limit",
    "ZAZA_SYNC_LIMIT_IP_FAILURES_PER_MINUTE": "ip_fail_per_window",
    "ZAZA_SYNC_LIMIT_IP_FAILURES_PER_10S": "ip_fail_burst_limit",
    "ZAZA_SYNC_LIMIT_IP_COARSE_PER_MINUTE": "ip_coarse_per_minute",
}


def sync_limit_settings_from_env(env: dict | None = None) -> SyncLimitSettings:
    env = dict(os.environ if env is None else env)
    kwargs: dict = {"mode": (env.get(ENV_MODE) or "off").strip().lower()}
    raw_ip_mode = (env.get(ENV_IP_MODE) or "").strip().lower()
    if raw_ip_mode:
        kwargs["ip_mode"] = raw_ip_mode
    for name, field in _ENV_OVERRIDES.items():
        raw = (env.get(name) or "").strip()
        if raw:
            try:
                kwargs[field] = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer") from None
    return SyncLimitSettings(**kwargs)


def digest(text: str) -> str:
    """A short, non-reversible label for logs (never the identifier itself)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def device_key(organization_id: object, device_id: str | None, route_digest: str) -> str:
    """The 64-hex key of one device's shared counter (control table CHECK: 64 lowercase hex).

    The device id comes from the control-plane route row (server-validated). A router that cannot supply it
    (a test double) falls back to the credential's routing digest, which is just as server-side."""
    identity = f"{organization_id}\x00{device_id}" if device_id else f"{organization_id}\x00route:{route_digest}"
    return hashlib.sha256(_DEVICE_KEY_DOMAIN + identity.encode("utf-8")).hexdigest()


def ip_key(address: str | None) -> str | None:
    """Table key of a VERIFIED address (None = unverified). IPv6 is grouped by /64: one subscriber, one key."""
    if not address:
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


def _ceil_seconds(seconds: float) -> int:
    return max(1, min(MAX_RETRY_AFTER, math.ceil(seconds - 1e-9)))


class SyncRateLimiter:
    """Thread-safe (the sync API runs in a thread pool). One instance per process."""

    def __init__(self, settings: SyncLimitSettings | None = None, counters: CounterStore | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[], float] = random.random) -> None:
        self.settings = settings or SyncLimitSettings()
        self.counters = counters
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        self.stats: Counter[str] = Counter()  # in-process telemetry, also exposed through the logs
        self._burst: OrderedDict[str, deque] = OrderedDict()  # device key -> admitted request times
        self._shared_view: OrderedDict[str, tuple[int, float]] = OrderedDict()  # device key -> (count, window end)
        self._fallback: OrderedDict[str, list] = OrderedDict()  # device key -> [window index, count]
        self._ip_failures: OrderedDict[str, deque] = OrderedDict()  # ip key -> failure times
        self._ip_coarse: OrderedDict[str, list] = OrderedDict()  # ip key -> [window index, count]
        self._known_good: OrderedDict[str, float] = OrderedDict()  # routing digest -> expiry
        self._log_gate: OrderedDict[tuple, float] = OrderedDict()

    # ─── bookkeeping ───────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @staticmethod
    def _bounded(table: OrderedDict, key, factory, maxsize: int):  # noqa: ANN001, ANN202
        """Get-or-create an entry in an LRU table; the oldest entries are evicted (never a failed request)."""
        if key in table:
            table.move_to_end(key)
            return table[key]
        table[key] = factory()
        while len(table) > maxsize:
            table.popitem(last=False)
        return table[key]

    def _mode_for(self, limit: str) -> str:
        return self.settings.ip_mode if limit.startswith("ip_") else self.settings.mode

    def _event(self, event: str, limit: str, subject: str, *, retry_after: int | None = None) -> None:
        """Count the event and log it, at most about once per 30 s per (event, limit, subject). Safe text only."""
        self.stats[f"{limit}_{event}"] += 1
        now = self._clock()
        gate = (event, limit, subject)
        last = self._log_gate.get(gate)
        if last is not None and now - last < 30:
            return
        self._log_gate[gate] = now
        self._log_gate.move_to_end(gate)
        while len(self._log_gate) > 5000:
            self._log_gate.popitem(last=False)
        logger.log(logging.WARNING if event == "counter_failure" else logging.INFO,
                   "sync_rate_limit event=%s limit=%s mode=%s subject=%s retry_after=%s",
                   event, limit, self._mode_for(limit), subject, retry_after if retry_after is not None else "-")

    def _refuse(self, limit: str, subject: str, retry_after: int) -> None:
        """A limit was hit: record it; in enforce mode refuse the request, in shadow mode let it through."""
        retry_after = max(1, min(MAX_RETRY_AFTER, retry_after))
        if self._mode_for(limit) == "enforce":
            self._event("limited", limit, subject, retry_after=retry_after)
            raise RateLimitExceeded(retry_after)
        self._event("would_limit", limit, subject, retry_after=retry_after)

    # ─── known-good credentials ────────────────────────────────────────────
    def _is_known_good(self, route_digest: str | None, now: float) -> bool:
        if not route_digest:
            return False
        expiry = self._known_good.get(route_digest)
        if expiry is None:
            return False
        if expiry <= now:
            del self._known_good[route_digest]
            return False
        self._known_good.move_to_end(route_digest)
        return True

    def auth_succeeded(self, route_digest: str) -> None:
        """A credential authenticated: it bypasses the per-IP failure gate for a short while."""
        if not self.enabled:
            return
        with self._lock:
            self._known_good[route_digest] = self._clock() + self.settings.known_good_ttl_seconds
            self._known_good.move_to_end(route_digest)
            while len(self._known_good) > self.settings.known_good_size:
                self._known_good.popitem(last=False)

    # ─── invalid / unauthenticated traffic (process-local, per verified IP) ─
    def _ip_wait(self, key: str, now: float) -> float:
        events = self._ip_failures.get(key)
        if not events:
            return 0.0
        waits = [0.0]
        for window, limit in ((self.settings.ip_fail_burst_window_seconds, self.settings.ip_fail_burst_limit),
                              (self.settings.ip_fail_window_seconds, self.settings.ip_fail_per_window)):
            inside = [t for t in events if t > now - window]
            if len(inside) >= limit:
                waits.append(inside[len(inside) - limit] + window - now)
        return max(waits)

    def gate(self, client_ip: str | None, route_digest: str | None = None) -> None:
        """Before the route lookup: refuse (429) a verified IP that is over its authentication-failure budget, unless the
        credential is known good. ``route_digest`` None means the request carried no usable credential."""
        if not self.settings.ip_active:
            return
        key = ip_key(client_ip)
        with self._lock:
            now = self._clock()
            if key is None:
                self.stats["ip_unverified_skipped"] += 1
                return
            if self._is_known_good(route_digest, now):
                return
            wait = self._ip_wait(key, now)
        if wait > 0:
            self._refuse("ip_failures", digest(key), _ceil_seconds(wait))

    def auth_failed(self, client_ip: str | None) -> None:
        """A request failed authentication (missing, malformed, unknown, revoked): count it against its verified IP."""
        if not self.settings.ip_active:
            return
        key = ip_key(client_ip)
        if key is None:
            return
        with self._lock:
            now = self._clock()
            events = self._bounded(self._ip_failures, key, lambda: deque(maxlen=max(
                self.settings.ip_fail_per_window, self.settings.ip_fail_burst_limit) + 1),
                                   self.settings.max_tracked_ips)
            events.append(now)

    # ─── authenticated traffic ─────────────────────────────────────────────
    def admit_device(self, organization_id: object, device_id: str | None, route_digest: str,
                     client_ip: str | None = None) -> None:
        """After the route lookup, before any tenant database: the coarse IP safeguard, the local burst guard, then the
        shared per-device counter. Raises :class:`RateLimitExceeded` in enforce mode."""
        if not self.enabled:
            return
        key = device_key(organization_id, device_id, route_digest)
        subject = digest(key)
        self._coarse_ip(client_ip)
        burst_wait = self._burst_guard(key, subject)
        self._shared(key, subject, burst_wait)

    def _coarse_ip(self, client_ip: str | None) -> None:
        if not self.settings.ip_active:
            return
        key = ip_key(client_ip)
        if key is None:
            with self._lock:
                self.stats["ip_unverified_skipped"] += 1
            return
        with self._lock:
            now = self._clock()
            window = int(now // 60)
            slot = self._bounded(self._ip_coarse, key, lambda: [window, 0], self.settings.max_tracked_ips)
            if slot[0] != window:
                slot[0], slot[1] = window, 0
            slot[1] += 1
            over = slot[1] > self.settings.ip_coarse_per_minute
            wait = (window + 1) * 60 - now
        if over:
            self._refuse("ip_coarse", digest(key), _ceil_seconds(wait))

    def _burst_guard(self, key: str, subject: str) -> float:
        """Rejection-only. Returns the wait (seconds) when the guard would reject, else 0 after recording the request."""
        s = self.settings
        with self._lock:
            now = self._clock()
            times = self._bounded(self._burst, key, deque, s.max_tracked_devices)
            while times and times[0] <= now - s.burst_window_seconds:
                times.popleft()
            if len(times) >= s.burst_limit:
                return max(0.0, times[0] + s.burst_window_seconds - now) or 0.001
            times.append(now)
            return 0.0

    def _shared(self, key: str, subject: str, burst_wait: float) -> None:
        s = self.settings
        if burst_wait > 0:
            # The burst guard refuses this request, so it is not counted (it is not processed). When the shared counter
            # was recently seen at its limit, the longer of the two waits is reported.
            with self._lock:
                seen = self._shared_view.get(key)
                now = self._clock()
                shared_wait = (seen[1] - now) if seen and seen[0] >= s.device_per_window else 0.0
            self._refuse("device_burst", subject, _ceil_seconds(max(burst_wait, shared_wait)))
            return
        try:
            if self.counters is None:
                raise RuntimeError("no counter store")
            attempts, retry_after = self.counters.count_attempt(DEVICE_BUCKET, key, s.device_window_seconds)
        except Exception as exc:  # noqa: BLE001 — fail open: a limiter fault must not lose telemetry
            self._event("counter_failure", "device_shared", subject)
            logger.debug("shared counter error type: %s", type(exc).__name__)
            self._fallback_ceiling(key, subject)
            return
        with self._lock:
            self._shared_view[key] = (attempts, self._clock() + retry_after)
            self._shared_view.move_to_end(key)
            while len(self._shared_view) > s.max_tracked_devices:
                self._shared_view.popitem(last=False)
        if attempts > s.device_per_window:
            self._refuse("device_shared", subject, _ceil_seconds(retry_after))
        self._maybe_cleanup()

    def _fallback_ceiling(self, key: str, subject: str) -> None:
        """Local protection while the shared counter is unavailable (fixed 60 s window, per process)."""
        with self._lock:
            now = self._clock()
            window = int(now // 60)
            slot = self._bounded(self._fallback, key, lambda: [window, 0], self.settings.max_tracked_devices)
            if slot[0] != window:
                slot[0], slot[1] = window, 0
            slot[1] += 1
            over = slot[1] > self.settings.fallback_device_per_window
            wait = (window + 1) * 60 - now
        if over:
            self._refuse("device_fallback", subject, _ceil_seconds(wait))

    def _maybe_cleanup(self) -> None:
        cleanup = getattr(self.counters, "cleanup_expired_counters", None)
        if cleanup is None or self._rng() >= 1.0 / _CLEANUP_EVERY:
            return
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001 — housekeeping must never affect a request
            logger.debug("counter cleanup failed: %s", type(exc).__name__)
