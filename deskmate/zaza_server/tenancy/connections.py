"""Tenant connection manager (Phase 10D): resolved TenantContext -> the
tenant's repository, with a small, bounded set of lazily opened pools.

::

    TenantContext (issued only by the resolver)
        -> TenantRepositoryManager.repository(context)
        -> tenant_database_settings(server base, context)   host/user/password
                                                            from SERVER config;
                                                            only the validated
                                                            database name varies
        -> PostgresRepository (the existing production repository)

Rules:

- **Lazy.** No pool exists because an organization exists; a pool is opened
  on the first request for that tenant.
- **Small.** Each tenant pool has ``min_size`` 0 and at most
  ``ZAZA_TENANT_POOL_MAX`` connections (default 2, hard limit 4).
- **Bounded.** At most ``ZAZA_TENANT_POOL_CACHE_MAX`` tenant pools are open
  per process (default 8, hard limit 64). Opening one more closes the least
  recently used pool that nobody is using; if every pool is in use the
  request fails fast (:class:`TenantPoolsExhausted`, HTTP 503) instead of
  exceeding the connection budget.
- **Idle eviction.** A pool unused for ``ZAZA_TENANT_POOL_IDLE_SECONDS``
  (default 300) is closed — on the next access, and by the optional reaper
  thread (:meth:`start_reaper`).
- **Failure isolation.** Pools are keyed by (organization id, database
  name) and every pool is built from that context alone. A tenant whose
  database fails is retired and rebuilt on its next use; no other tenant's
  pool is touched, and no tenant ever receives another tenant's pool.
- **Shutdown.** :meth:`close` closes every pool.

The cache is per process (per Cloud Run instance). It is an optimisation,
never coordination: correctness does not depend on it, and each instance
opens its own pools — see :func:`connection_budget`.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..config import ConfigError, DatabaseSettings
from ..repository import RepositoryUnavailable
from . import domain
from .context import TenantContext, tenant_database_settings
from .domain import TenancyError

logger = logging.getLogger("zaza_server.tenancy")

TENANT_POOL_MAX_LIMIT = 4
TENANT_POOL_CACHE_MAX_LIMIT = 64
TENANT_POOL_IDLE_MIN_SECONDS = 10
TENANT_POOL_IDLE_MAX_SECONDS = 3600
CONTROL_POOL_MAX = 2  # PostgresControlRepository never opens more


class TenantPoolsExhausted(RepositoryUnavailable):
    """Every cached tenant pool is in use; opening another would exceed the
    per-instance connection budget. Retry later (HTTP 503)."""


@dataclass(frozen=True)
class TenantPoolLimits:
    pool_max: int = 2          # connections per tenant pool (min is always 0)
    cache_max: int = 8         # open tenant pools per process
    idle_seconds: int = 300    # close a tenant pool unused for this long
    open_wait_seconds: float = 15.0  # wait for another thread opening the same pool

    def __post_init__(self) -> None:
        if not 1 <= self.pool_max <= TENANT_POOL_MAX_LIMIT:
            raise ConfigError(f"ZAZA_TENANT_POOL_MAX must be 1..{TENANT_POOL_MAX_LIMIT}")
        if not 1 <= self.cache_max <= TENANT_POOL_CACHE_MAX_LIMIT:
            raise ConfigError(f"ZAZA_TENANT_POOL_CACHE_MAX must be 1..{TENANT_POOL_CACHE_MAX_LIMIT}")
        if not TENANT_POOL_IDLE_MIN_SECONDS <= self.idle_seconds <= TENANT_POOL_IDLE_MAX_SECONDS:
            raise ConfigError(f"ZAZA_TENANT_POOL_IDLE_SECONDS must be "
                              f"{TENANT_POOL_IDLE_MIN_SECONDS}..{TENANT_POOL_IDLE_MAX_SECONDS}")
        if not 0 < self.open_wait_seconds <= 120:
            raise ConfigError("the tenant pool open wait must be 0..120 seconds")

    @property
    def connections_per_instance(self) -> int:
        """The most tenant connections one process can hold."""
        return self.pool_max * self.cache_max


def _int(env: dict, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def tenant_pool_limits_from_env(env: dict | None = None) -> TenantPoolLimits:
    env = dict(os.environ if env is None else env)
    return TenantPoolLimits(
        pool_max=_int(env, "ZAZA_TENANT_POOL_MAX", 2),
        cache_max=_int(env, "ZAZA_TENANT_POOL_CACHE_MAX", 8),
        idle_seconds=_int(env, "ZAZA_TENANT_POOL_IDLE_SECONDS", 300),
    )


# ─── connection budget ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectionBudget:
    """Worst case database connections of the whole Cloud Run service:

    ``max_instances × (cache_max × pool_max + control pool + other pools)``

    It must stay below what the database grants the application
    (``capacity``: max_connections minus the reserve for administration,
    migrations and Cloud SQL itself)."""

    max_instances: int
    tenant_per_instance: int
    control_per_instance: int
    other_per_instance: int
    capacity: int

    @property
    def per_instance(self) -> int:
        return self.tenant_per_instance + self.control_per_instance + self.other_per_instance

    @property
    def total(self) -> int:
        return self.max_instances * self.per_instance

    @property
    def ok(self) -> bool:
        return self.total <= self.capacity

    def describe(self) -> str:
        return (f"{self.max_instances} instance(s) x ({self.tenant_per_instance} tenant + {self.control_per_instance} "
                f"control + {self.other_per_instance} other) = {self.total} of {self.capacity} connections")


def connection_budget(*, max_instances: int, limits: TenantPoolLimits, capacity: int,
                      control_pool_max: int = CONTROL_POOL_MAX, other_per_instance: int = 0) -> ConnectionBudget:
    if max_instances < 1 or capacity < 1 or control_pool_max < 0 or other_per_instance < 0:
        raise ConfigError("connection budget values must be positive")
    return ConnectionBudget(max_instances, limits.connections_per_instance, control_pool_max, other_per_instance,
                            capacity)


# ─── the manager ───────────────────────────────────────────────────────────


class _Entry:
    __slots__ = ("key", "settings", "repo", "leases", "last_used", "ready", "failed", "retired", "closed")

    def __init__(self, key: tuple[uuid.UUID, str], settings: DatabaseSettings, now: float) -> None:
        self.key = key
        self.settings = settings
        self.repo = None
        self.leases = 0
        self.last_used = now
        self.ready = threading.Event()  # set when the repository is open (or opening failed)
        self.failed = False
        self.retired = False            # no new leases; closed when the last lease ends
        self.closed = False


def open_tenant_repository(settings: DatabaseSettings, *, actor_type: str = "SYSTEM",  # noqa: ANN201
                           actor_id: str = "saas"):
    """Default factory: the existing production repository on the tenant
    database, refusing an out-of-date schema, then checking that the server
    really connected to the database the context names. ``actor_*`` label
    the tenant audit entries it writes."""
    from ..postgres import PostgresRepository  # noqa: PLC0415 — optional dependency

    repo = PostgresRepository(settings, actor_type=actor_type, actor_id=actor_id, require_current_schema=True,
                              pool_name="zaza-tenant", check_connections=True, pool_workers=1)
    try:
        with repo.connection() as conn:
            actual = conn.execute("SELECT current_database() AS db").fetchone()["db"]
        if actual != settings.dbname:
            raise TenancyError("connected to an unexpected database")
    except BaseException:
        repo.close()
        raise
    return repo


class TenantRepositoryManager:
    """Thread-safe, bounded cache of per-tenant repositories.

    ``base`` is the SERVER's database configuration (host or Cloud SQL
    socket, runtime user, password from a secret file, timeouts). The only
    per-tenant input is a resolved :class:`TenantContext`."""

    def __init__(self, base: DatabaseSettings, limits: TenantPoolLimits | None = None, *,
                 repository_factory: Callable[[DatabaseSettings], object] = open_tenant_repository,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(base, DatabaseSettings):
            raise TenancyError("the tenant connection manager needs server database settings")
        self._base = base
        self.limits = limits or TenantPoolLimits()
        self._factory = repository_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[uuid.UUID, str], _Entry] = OrderedDict()  # LRU first
        self._closed = False
        self._reaper: threading.Thread | None = None
        self._reaper_stop = threading.Event()

    # ─── public API ───────────────────────────────────────────────────────
    @contextmanager
    def repository(self, context: TenantContext) -> Iterator[object]:
        """``with manager.repository(context) as repo:`` — the repository of
        the context's tenant database, leased for the block. A database
        failure inside the block retires that tenant's pool (it is rebuilt
        on next use); other tenants are not affected."""
        entry = self._acquire(context)
        broken = False
        try:
            yield entry.repo
        except RepositoryUnavailable:
            broken = True
            raise
        finally:
            self._release(entry, broken=broken)

    def settings_for(self, context: TenantContext) -> DatabaseSettings:
        """Connection settings for the context's tenant: the server's base
        settings, the validated database name, a small pool (min 0)."""
        settings = tenant_database_settings(self._base, context)
        return dataclasses.replace(settings, pool_min=0, pool_max=self.limits.pool_max)

    def evict_idle(self) -> int:
        """Close every tenant pool that has been unused for the idle limit.
        Returns how many were closed."""
        with self._lock:
            victims = self._take_idle_locked(self._clock())
        self._close_entries(victims)
        return len(victims)

    def discard(self, organization_id: object) -> int:
        """Close this organization's cached pool now (e.g. right after it was
        suspended); requests still using it finish first. A new pool needs a
        newly resolved context, which a suspended organization can't get."""
        org_id = domain.organization_id(organization_id)
        with self._lock:
            entries = [e for e in self._entries.values() if e.key[0] == org_id]
            for entry in entries:
                self._retire_locked(entry)
        self._close_entries(entries)
        return len(entries)

    def stats(self) -> dict[str, int]:
        """Counts only (no organization ids or database names)."""
        with self._lock:
            return {"open_pools": len(self._entries),
                    "in_use": sum(1 for e in self._entries.values() if e.leases > 0),
                    "cache_max": self.limits.cache_max, "pool_max": self.limits.pool_max}

    def start_reaper(self, interval: float | None = None) -> None:
        """Background thread that runs :meth:`evict_idle` periodically, so an
        idle instance releases its tenant connections without traffic."""
        interval = interval or max(5.0, min(60.0, self.limits.idle_seconds / 2))
        with self._lock:
            if self._closed or self._reaper is not None:
                return
            self._reaper = threading.Thread(target=self._reap, args=(interval,), name="zaza-tenant-pool-reaper",
                                            daemon=True)
            self._reaper.start()

    def close(self) -> None:
        """Close every tenant pool (application shutdown). Idempotent; no
        tenant repository can be obtained afterwards."""
        with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.retired = True
            reaper, self._reaper = self._reaper, None
        self._reaper_stop.set()
        if reaper is not None and reaper is not threading.current_thread():
            reaper.join(timeout=5)
        self._close_entries(entries)

    def __enter__(self) -> TenantRepositoryManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ─── internals ────────────────────────────────────────────────────────
    def _acquire(self, context: TenantContext) -> _Entry:
        if not isinstance(context, TenantContext):
            raise TenancyError("a tenant repository needs a resolved TenantContext")
        settings = self.settings_for(context)
        key = (domain.organization_id(context.organization_id), settings.dbname)
        victims: list[_Entry] = []
        try:
            with self._lock:
                if self._closed:
                    raise RepositoryUnavailable("tenant connections are shut down")
                victims = self._take_idle_locked(self._clock())
                entry = self._entries.get(key)
                creator = entry is None
                if creator:
                    if len(self._entries) >= self.limits.cache_max:
                        victim = next((e for e in self._entries.values()
                                       if e.leases == 0 and e.ready.is_set()), None)
                        if victim is None:
                            raise TenantPoolsExhausted("too many tenant databases are busy; retry shortly")
                        self._retire_locked(victim)
                        victims.append(victim)
                    entry = _Entry(key, settings, self._clock())
                    self._entries[key] = entry
                else:
                    self._entries.move_to_end(key)
                entry.leases += 1
        finally:
            self._close_entries(victims)

        if creator:
            self._open(entry)
        elif not entry.ready.wait(self.limits.open_wait_seconds) or entry.repo is None:
            self._release(entry, broken=False)
            raise RepositoryUnavailable("tenant database unavailable")
        # defence in depth: the pool really is the one for THIS context
        if entry.key != key or entry.settings.dbname != context.database.database_name:
            self._release(entry, broken=True)
            raise TenancyError("tenant pool mismatch")
        return entry

    def _open(self, entry: _Entry) -> None:
        try:
            repo = self._factory(entry.settings)
        except BaseException as exc:
            with self._lock:
                entry.failed = True
                entry.leases -= 1
                self._retire_locked(entry)
            entry.ready.set()
            if isinstance(exc, (RepositoryUnavailable, TenancyError)):
                raise
            if not isinstance(exc, Exception):
                raise
            logger.warning("tenant database could not be opened (%s)", type(exc).__name__)
            raise RepositoryUnavailable("tenant database unavailable") from None
        with self._lock:
            abandoned = entry.retired  # the manager was closed meanwhile
            if not abandoned:
                entry.repo = repo
        if abandoned:
            self._safe_close(repo)
            with self._lock:
                entry.leases -= 1
            entry.ready.set()
            raise RepositoryUnavailable("tenant connections are shut down")
        entry.ready.set()

    def _release(self, entry: _Entry, *, broken: bool) -> None:
        with self._lock:
            entry.leases -= 1
            entry.last_used = self._clock()
            if broken:
                self._retire_locked(entry)
            close_now = entry.retired and entry.leases == 0 and not entry.closed
        if close_now:
            self._close_entries([entry])

    def _retire_locked(self, entry: _Entry) -> None:
        entry.retired = True
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]

    def _take_idle_locked(self, now: float) -> list[_Entry]:
        idle = [e for e in self._entries.values()
                if e.leases == 0 and e.ready.is_set() and now - e.last_used >= self.limits.idle_seconds]
        for entry in idle:
            self._retire_locked(entry)
        return idle

    def _close_entries(self, entries: list[_Entry]) -> None:
        for entry in entries:
            with self._lock:
                if entry.closed or entry.leases > 0 and not self._closed:
                    continue  # still in use: closed when its last lease ends
                entry.closed = True
                repo, entry.repo = entry.repo, None
            if repo is not None:
                self._safe_close(repo)

    @staticmethod
    def _safe_close(repo: object) -> None:
        try:
            repo.close()
        except Exception as exc:  # noqa: BLE001 — closing one tenant must not affect the others
            logger.warning("closing a tenant pool failed (%s)", type(exc).__name__)

    def _reap(self, interval: float) -> None:
        while not self._reaper_stop.wait(interval):
            try:
                self.evict_idle()
            except Exception as exc:  # noqa: BLE001 — keep reaping
                logger.warning("tenant pool reaper: %s", type(exc).__name__)
