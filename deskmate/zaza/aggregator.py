"""Turns per-tick observations into summarized activity periods and idle
periods.

One *activity period* is a contiguous stretch with the same period key:
status (ACTIVE / IDLE / UNKNOWN / LOCKED), application, domain, and for
UNKNOWN the reason it is unknown. Each tick either extends the open period or
closes it and opens the next one at the same instant, so a session's periods
tile it with no gaps and no overlaps.

Window-title changes inside the same app are debounced: a new (normalized)
title must stay put for ``title_debounce_seconds`` before it splits the
period, and the split is placed where that title was first seen. Brief title
flicker (tab hopping, unread counters, "saving..." markers) is absorbed.

Status changes can be placed at their real time via ``effective_at``: idle
begins where the grace period ran out (last input + threshold, computed by
the agent), which may fall between ticks, and activity resumes at the input
that ended the idle stretch. Placement is always clamped to the open period,
so a closed period is never rewritten.

*Idle periods* are the merged stretches of consecutive IDLE activity periods
(an app switch while idle doesn't split them).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .privacy import title_key
from .storage import ActivityStore
from .timeutil import parse_iso

ACTIVE = "ACTIVE"
IDLE = "IDLE"
UNKNOWN = "UNKNOWN"
LOCKED = "LOCKED"


@dataclass(frozen=True)
class Observation:
    status: str
    app_name: str | None = None
    window_title: str | None = None
    domain: str | None = None
    privacy_excluded: bool = False
    detail: str | None = None  # why UNKNOWN: MONITORING_UNAVAILABLE, AWAITING_INPUT, TELEMETRY_GAP

    @property
    def key(self) -> tuple:
        return (self.status, self.detail if self.status == UNKNOWN else None, self.app_name, self.domain)


@dataclass
class _OpenPeriod:
    period_id: str
    started: float
    status: str
    key: tuple
    tkey: str


@dataclass
class _OpenIdle:
    idle_id: str
    started: float


@dataclass
class _PendingTitle:
    tkey: str
    since: float


def _idle_end_reason(next_status: str) -> str:
    return {ACTIVE: "ACTIVITY_RESUMED", LOCKED: "LOCK", UNKNOWN: "MONITORING_UNKNOWN"}.get(next_status, "STATUS_CHANGE")


class PeriodAggregator:
    def __init__(
        self,
        store: ActivityStore,
        *,
        device_id: str,
        title_debounce_seconds: float,
        on_period_closed: Callable[[float, float], None] | None = None,
    ) -> None:
        self._store = store
        self._device_id = device_id
        self._debounce = title_debounce_seconds
        self._on_period_closed = on_period_closed
        self._session_id: str | None = None
        self._cursor = 0.0
        self._next_start_reason = "SESSION_START"
        self._open: _OpenPeriod | None = None
        self._idle: _OpenIdle | None = None
        self._pending: _PendingTitle | None = None

    @property
    def open_period_id(self) -> str | None:
        return self._open.period_id if self._open else None

    def begin(self, session_id: str, started_at: float) -> None:
        self._session_id = session_id
        self._cursor = started_at
        self._next_start_reason = "SESSION_START"
        self._open = None
        self._idle = None
        self._pending = None

    # ─── main entry point ──────────────────────────────────────────────────
    def observe(
        self, obs: Observation, *, now: float, effective_at: float | None = None, reason: str | None = None
    ) -> None:
        if self._session_id is None:
            raise RuntimeError("aggregator has no session; call begin() first")
        with self._store.transaction():
            op = self._open
            if op is None:
                start = min(self._cursor, now)
                self._start_period(obs, at=start, now=now, reason=reason or self._next_start_reason)
                return

            if obs.key != op.key:
                status_changed = obs.status != op.status
                split = effective_at if (status_changed and effective_at is not None) else now
                split = min(max(split, op.started), now)
                why = reason or ("STATUS_CHANGE" if status_changed else "APP_CHANGE")
                self._close_open(split, why)
                self._start_period(obs, at=split, now=now, reason=why)
                return

            tkey = title_key(obs.window_title)
            if tkey == op.tkey:
                self._pending = None
                self._extend(now)
                return

            split_at: float | None = None
            if self._debounce <= 0:
                split_at = now
            elif self._pending is not None and self._pending.tkey == tkey:
                if now - self._pending.since >= self._debounce:
                    split_at = max(self._pending.since, op.started)
            else:
                self._pending = _PendingTitle(tkey=tkey, since=now)

            if split_at is None:
                self._extend(now)
                return
            self._close_open(split_at, reason or "WINDOW_CHANGE")
            self._start_period(obs, at=split_at, now=now, reason=reason or "WINDOW_CHANGE")

    def record_gap(self, start: float, end: float) -> None:
        """The agent wasn't sampling between ``start`` and ``end`` (sleep,
        suspended process). Close everything at ``start`` and record the gap
        itself as a closed UNKNOWN period — never as activity or idle."""
        if self._session_id is None or end <= start:
            return
        with self._store.transaction():
            if self._open is not None:
                self._close_open(max(start, self._open.started), "TELEMETRY_GAP")
            if self._idle is not None:
                self._close_idle(start, "TELEMETRY_GAP")
            self._store.insert_period(
                period_id=str(uuid.uuid4()),
                session_id=self._session_id,
                device_id=self._device_id,
                started_at=start,
                ended_at=end,
                status=UNKNOWN,
                status_detail="TELEMETRY_GAP",
                start_reason="TELEMETRY_GAP",
                end_reason="TELEMETRY_GAP",
                is_open=False,
            )
            self._cursor = end
            self._next_start_reason = "TELEMETRY_GAP"
            self._pending = None

    def finish(self, now: float, reason: str) -> None:
        with self._store.transaction():
            if self._open is not None:
                self._close_open(max(now, self._open.started), reason)
            if self._idle is not None:
                self._close_idle(max(now, self._idle.started), reason)
            self._pending = None

    def reload(self) -> None:
        """Rebuild in-memory state from the database — used after a failed
        (rolled-back) tick so memory and disk agree again."""
        if self._session_id is None:
            return
        self._open = None
        self._idle = None
        self._pending = None
        periods = self._store.periods(self._session_id)
        for row in periods:
            if row["is_open"]:
                obs = Observation(
                    status=row["status"], app_name=row["app_name"], domain=row["domain"], detail=row["status_detail"]
                )
                self._open = _OpenPeriod(
                    period_id=row["period_id"],
                    started=parse_iso(row["started_at"]),
                    status=row["status"],
                    key=obs.key,
                    tkey=title_key(row["window_title"]),
                )
        if periods:
            self._cursor = max(parse_iso(r["ended_at"]) for r in periods)
        for row in self._store.idle_periods(self._session_id):
            if row["is_open"]:
                self._idle = _OpenIdle(idle_id=row["idle_id"], started=parse_iso(row["started_at"]))

    # ─── internals ─────────────────────────────────────────────────────────
    def _start_period(self, obs: Observation, *, at: float, now: float, reason: str) -> None:
        period_id = str(uuid.uuid4())
        self._store.insert_period(
            period_id=period_id,
            session_id=self._session_id,
            device_id=self._device_id,
            started_at=at,
            ended_at=now,
            status=obs.status,
            status_detail=obs.detail if obs.status == UNKNOWN else None,
            app_name=obs.app_name,
            window_title=obs.window_title,
            domain=obs.domain,
            privacy_excluded=obs.privacy_excluded,
            start_reason=reason,
        )
        self._open = _OpenPeriod(period_id, at, obs.status, obs.key, title_key(obs.window_title))
        self._pending = None
        if obs.status == IDLE:
            if self._idle is None:
                idle_id = str(uuid.uuid4())
                self._store.insert_idle(
                    idle_id=idle_id, session_id=self._session_id, device_id=self._device_id,
                    started_at=at, ended_at=now,
                )
                self._idle = _OpenIdle(idle_id, at)
            else:
                self._store.update_idle_end(self._idle.idle_id, started_at=self._idle.started, ended_at=now)
        elif self._idle is not None:
            self._close_idle(at, _idle_end_reason(obs.status))

    def _extend(self, now: float) -> None:
        op = self._open
        self._store.update_period_end(op.period_id, started_at=op.started, ended_at=now)
        if self._idle is not None:
            self._store.update_idle_end(self._idle.idle_id, started_at=self._idle.started, ended_at=now)

    def _close_open(self, at: float, reason: str) -> None:
        op = self._open
        self._store.update_period_end(op.period_id, started_at=op.started, ended_at=at, close_reason=reason)
        self._open = None
        self._cursor = at
        if self._on_period_closed is not None:
            self._on_period_closed(op.started, at)

    def _close_idle(self, at: float, reason: str) -> None:
        idle = self._idle
        self._store.update_idle_end(idle.idle_id, started_at=idle.started, ended_at=max(at, idle.started),
                                    close_reason=reason)
        self._idle = None
