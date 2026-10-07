"""Background sync worker: local SQLite -> sync API.

Runs on its own thread with its own SQLite connection, so a slow or dead
server can never block activity sampling. Each cycle:

1. Collects PENDING/FAILED records from the four summarized tables in
   device order (``local_seq``), up to ``batch_size`` per request and
   ``max_batches_per_cycle`` requests per cycle. Open records (the current
   session/period/idle period) are included so the server stays current;
   their version moves on every tick, so they are simply re-sent.
2. Sends one batch per request and applies the per-record results:

   - accepted / updated / already_current -> ``mark_synced(id, sent_version)``.
     Version-checked: a record changed while the request was in flight
     stays PENDING and its newer version goes out next time.
   - stale    -> only valid with ``server_version > sent version``: keep the
     server's copy and move the local version number up to match
     (``adopt_server_version``, version-checked, so a record changed in
     flight stays PENDING). A stale reply with a missing, equal or lower
     ``server_version`` is a protocol fault -> ``mark_sync_failed``; it is
     never marked synced.
   - conflict -> same version, different content; bump the local version
     (``requeue_record``) so the device's current content is re-sent.
   - rejected, or no result for a record -> ``mark_sync_failed`` (retried).

3. On a transport-level failure nothing is marked: every record stays
   exactly as it was, and the cycle stops. Sync health records why.

Nothing here deletes data. Sync state is never fed into activity
classification: being offline is not employee inactivity.
"""

from __future__ import annotations

import enum
import json
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import tzinfo

from .. import __version__
from ..logger import get
from ..storage import ActivityStore
from ..timeutil import iso_utc
from .backoff import Backoff
from .protocol import CONFIRMED_STATUSES, MAX_RECORDS_PER_BATCH, RECORD_TABLES, TABLE_RECORD_TYPES
from .serialize import to_wire
from .transport import AuthError, NetworkError, PayloadTooLarge, RateLimited, SyncTransport, SyncTransportError

logger = get("sync")

_STATE_KEY = "sync.health"


class SyncState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    BACKLOG = "BACKLOG"
    OFFLINE = "OFFLINE"
    AUTH_ERROR = "AUTH_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class SyncHealth:
    state: SyncState = SyncState.NOT_CONFIGURED
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    next_attempt_at: str | None = None
    pending_count: int = 0
    failed_count: int = 0
    open_count: int = 0

    def summary(self) -> str:
        parts = [f"sync {self.state.value}", f"pending={self.pending_count}", f"failed={self.failed_count}"]
        if self.last_success_at:
            parts.append(f"last_success={self.last_success_at}")
        if self.last_error and self.state not in (SyncState.HEALTHY,):
            parts.append(f"last_error={self.last_error}")
        return " ".join(parts)


@dataclass
class CycleResult:
    ok: bool
    batches: int = 0
    sent: int = 0
    confirmed: int = 0
    stale: int = 0
    conflicts: int = 0
    failed: int = 0
    more_pending: bool = False
    error_kind: str | None = None
    error: str | None = None
    retry_after: float | None = None
    statuses: dict[str, int] = field(default_factory=dict)


_STATE_FOR_ERROR = {
    "network": SyncState.OFFLINE,
    "auth": SyncState.AUTH_ERROR,
}


class SyncWorker:
    def __init__(
        self,
        store: ActivityStore,
        transport: SyncTransport,
        *,
        device_id: str,
        employee_id: str | None,
        batch_size: int = 100,
        max_batches_per_cycle: int = 20,
        interval_seconds: float = 60.0,
        auth_retry_seconds: float = 900.0,
        include_open: bool = True,
        backoff: Backoff | None = None,
        wall_clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        tz: tzinfo | None = None,
        on_state_change: Callable[[SyncHealth], None] | None = None,
    ) -> None:
        if not 1 <= batch_size <= MAX_RECORDS_PER_BATCH:
            raise ValueError(f"batch_size must be 1..{MAX_RECORDS_PER_BATCH}")
        self.store = store
        self.transport = transport
        self.device_id = device_id
        self.employee_id = employee_id
        self.batch_size = batch_size
        self.max_batches_per_cycle = max_batches_per_cycle
        self.interval_seconds = interval_seconds
        self.auth_retry_seconds = auth_retry_seconds
        self.include_open = include_open
        self.backoff = backoff or Backoff(rng=rng)
        self._wall = wall_clock
        self._rng = rng
        self.tz = tz
        self._on_state_change = on_state_change
        self._cycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ─── health ────────────────────────────────────────────────────────────
    def _load_health(self) -> SyncHealth:
        raw = self.store.get_sync_state(_STATE_KEY)
        if not raw:
            return SyncHealth()
        try:
            data = json.loads(raw)
            data["state"] = SyncState(data["state"])
            return SyncHealth(**{k: v for k, v in data.items() if k in SyncHealth.__dataclass_fields__})
        except (ValueError, KeyError, TypeError):
            return SyncHealth()

    def _save_health(self, health: SyncHealth) -> None:
        data = asdict(health)
        data["state"] = health.state.value
        self.store.set_sync_state(_STATE_KEY, json.dumps(data), at=self._wall())

    def health(self) -> SyncHealth:
        health = self._load_health()
        counts = self.store.sync_counts()
        health.pending_count = counts["pending"]
        health.failed_count = counts["failed"]
        health.open_count = counts["open"]
        return health

    def _set_health(self, **changes) -> SyncHealth:  # noqa: ANN003
        health = self._load_health()
        previous = health.state
        for key, value in changes.items():
            setattr(health, key, value)
        self._save_health(health)
        if health.state != previous:
            level = logger.info if health.state in (SyncState.HEALTHY, SyncState.BACKLOG) else logger.warning
            level("sync state %s -> %s%s", previous.value, health.state.value,
                  f" ({health.last_error})" if health.last_error and health.state != SyncState.HEALTHY else "")
            if self._on_state_change is not None:
                try:
                    self._on_state_change(self.health())
                except Exception:  # noqa: BLE001
                    pass
        return health

    # ─── one cycle ─────────────────────────────────────────────────────────
    def _collect(self, after_seq: int | None) -> list[tuple[str, dict]]:
        rows: list[tuple[str, dict]] = []
        for table, _ in RECORD_TABLES.values():
            for row in self.store.pending_sync(
                table, limit=self.batch_size, include_open=self.include_open, after_seq=after_seq
            ):
                rows.append((table, row))
        rows.sort(key=lambda item: item[1]["local_seq"])
        return rows[: self.batch_size]

    def _payload(self, rows: list[tuple[str, dict]]) -> dict:
        return {
            "batch_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "agent_version": __version__,
            "sent_at": iso_utc(self._wall()),
            "records": [to_wire(table, row, employee_id=self.employee_id, tz=self.tz) for table, row in rows],
        }

    def _apply(self, rows: list[tuple[str, dict]], response, result: CycleResult) -> None:  # noqa: ANN001
        by_key = {(r.record_type, r.record_id): r for r in response.results}
        now = self._wall()
        for table, row in rows:
            _, id_col = RECORD_TABLES[TABLE_RECORD_TYPES[table]]
            record_id, version = row[id_col], row["record_version"]
            res = by_key.get((TABLE_RECORD_TYPES[table], record_id))
            status = res.status if res is not None else "missing"
            result.statuses[status] = result.statuses.get(status, 0) + 1
            if res is None or res.record_version != version:
                self.store.mark_sync_failed(table, [record_id], error="no acknowledgement for this version", at=now)
                result.failed += 1
            elif res.status in CONFIRMED_STATUSES:
                self.store.mark_synced(table, [(record_id, version)], at=now)
                result.confirmed += 1
            elif res.status == "stale":
                if res.server_version is None or res.server_version <= version:
                    # "stale" must come with a strictly newer server version.
                    # Anything else is a protocol fault: never mark it synced,
                    # keep it queued for retry.
                    self.store.mark_sync_failed(
                        table, [record_id],
                        error=f"protocol: stale without newer server_version ({res.server_version})", at=now,
                    )
                    result.failed += 1
                    continue
                # Version-checked: if the local record changed while the request
                # was in flight, nothing is updated and the newer local version
                # stays PENDING for the next batch.
                self.store.adopt_server_version(
                    table, record_id, sent_version=version, server_version=res.server_version, at=now
                )
                logger.warning("server holds a newer version of %s %s (sent v%s, server v%s)",
                               table, record_id, version, res.server_version)
                result.stale += 1
            elif res.status == "conflict":
                self.store.requeue_record(table, record_id, expected_version=version, at=now)
                result.conflicts += 1
            else:
                self.store.mark_sync_failed(table, [record_id], error=f"rejected: {res.error or ''}"[:300], at=now)
                result.failed += 1

    def run_once(self) -> CycleResult:
        with self._cycle_lock:
            started = self._wall()
            self._set_health(last_attempt_at=iso_utc(started))
            result = CycleResult(ok=True)
            cursor: int | None = None
            while result.batches < self.max_batches_per_cycle:
                rows = self._collect(cursor)
                if not rows:
                    break
                try:
                    response = self.transport.send_batch(self._payload(rows))
                except PayloadTooLarge:
                    if self.batch_size > 1:
                        self.batch_size = max(1, self.batch_size // 2)
                        logger.warning("server rejected batch size; reducing to %d", self.batch_size)
                        continue
                    return self._failed(result, SyncTransportError("a single record exceeds the server limit"))
                except SyncTransportError as exc:
                    return self._failed(result, exc)
                self._apply(rows, response, result)
                result.batches += 1
                result.sent += len(rows)
                cursor = rows[-1][1]["local_seq"]
                if len(rows) < self.batch_size:
                    break
            else:
                result.more_pending = True

            counts = self.store.sync_counts()
            # Any closed record still unconfirmed is a backlog. Open records
            # (current session/period) are excluded from "pending" by design.
            backlog = result.more_pending or counts["pending"] > 0 or counts["failed"] > 0
            self.backoff.reset()
            self._set_health(
                state=SyncState.BACKLOG if backlog else SyncState.HEALTHY,
                last_success_at=iso_utc(self._wall()),
                consecutive_failures=0,
                last_error=None if not counts["failed"] else "some records were rejected; see local sync errors",
            )
            if result.sent:
                logger.info(
                    "sync cycle: sent=%d confirmed=%d stale=%d conflicts=%d failed=%d batches=%d",
                    result.sent, result.confirmed, result.stale, result.conflicts, result.failed, result.batches,
                )
            return result

    def _failed(self, result: CycleResult, exc: SyncTransportError) -> CycleResult:
        result.ok = False
        result.error_kind = exc.kind
        result.error = str(exc)[:300]
        if isinstance(exc, RateLimited):
            result.retry_after = exc.retry_after
        health = self._load_health()
        self._set_health(
            state=_STATE_FOR_ERROR.get(exc.kind, SyncState.SERVER_ERROR),
            last_error=f"{exc.kind}: {result.error}",
            consecutive_failures=health.consecutive_failures + 1,
        )
        return result

    def next_delay(self, result: CycleResult) -> float:
        if result.ok:
            return 1.0 if result.more_pending else float(self.interval_seconds)
        if result.error_kind == "auth":
            # Not a transient fault: retry rarely (credentials may be fixed
            # or the device re-enabled), never in a tight loop.
            return self.auth_retry_seconds * (0.8 + 0.4 * self._rng())
        return self.backoff.next_delay(at_least=result.retry_after or 0.0)

    # ─── background thread ─────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ZazaSyncWorker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def trigger(self) -> None:
        """Run a cycle as soon as possible (e.g. manual "sync now")."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.run_once()
            except Exception as exc:  # noqa: BLE001 — never let the thread die
                logger.error("sync cycle crashed: %s", type(exc).__name__)
                result = CycleResult(ok=False, error_kind="internal", error=type(exc).__name__)
                try:
                    self._set_health(state=SyncState.SERVER_ERROR, last_error=f"internal: {type(exc).__name__}")
                except Exception:  # noqa: BLE001
                    pass
            delay = self.next_delay(result)
            try:
                self._set_health(next_attempt_at=iso_utc(self._wall() + delay))
            except Exception:  # noqa: BLE001
                pass
            self._wake.wait(delay)
            self._wake.clear()


__all__ = [
    "AuthError", "CycleResult", "NetworkError", "SyncHealth", "SyncState", "SyncWorker",
]
