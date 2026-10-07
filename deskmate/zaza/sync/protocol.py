"""Sync API wire contract (``/api/v1``), shared by the agent and the server.

Requests are strict: unknown fields are rejected and every timestamp must be
UTC with an explicit offset (``Z`` or ``+00:00``). Responses are parsed
leniently (unknown fields ignored) so a newer server can add fields without
breaking older agents.

Each record in a batch is validated individually on the server, so one bad
record is rejected on its own instead of failing the whole batch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

API_PREFIX = "/api/v1"
BATCH_PATH = f"{API_PREFIX}/sync/batch"
HEALTH_PATH = f"{API_PREFIX}/sync/health"
DEVICE_PATH = f"{API_PREFIX}/devices/me"

DEVICE_ID_HEADER = "X-ZaZa-Device-Id"

MAX_RECORDS_PER_BATCH = 500
MAX_BATCH_BYTES = 4 * 1024 * 1024

RecordType = Literal["work_session", "activity_period", "idle_period", "app_usage_daily"]

# record_type -> local SQLite table / id column
RECORD_TABLES: dict[str, tuple[str, str]] = {
    "work_session": ("work_sessions", "session_id"),
    "activity_period": ("activity_periods", "period_id"),
    "idle_period": ("idle_periods", "idle_id"),
    "app_usage_daily": ("app_usage_daily", "usage_id"),
}
TABLE_RECORD_TYPES = {table: rtype for rtype, (table, _) in RECORD_TABLES.items()}

ResultStatus = Literal["accepted", "updated", "already_current", "stale", "conflict", "rejected"]
# Statuses after which the sent version is safely held by the server.
CONFIRMED_STATUSES = frozenset({"accepted", "updated", "already_current"})


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC (offset Z or +00:00)")
    return value


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_require_utc)]
Seconds = Annotated[float, Field(ge=0, le=10 * 366 * 86400)]
ShortStr = Annotated[str, Field(min_length=1, max_length=64)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# ─── record payloads ───────────────────────────────────────────────────────


class WorkSessionData(_Strict):
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    last_heartbeat_at: UtcDatetime
    status: Literal["OPEN", "CLOSED", "INTERRUPTED"]
    start_reason: ShortStr
    end_reason: ShortStr | None = None
    previous_session_id: UUID | None = None
    tracked_seconds: Seconds
    active_seconds: Seconds
    idle_seconds: Seconds
    unknown_seconds: Seconds
    locked_seconds: Seconds


class ActivityPeriodData(_Strict):
    session_id: UUID
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_seconds: Seconds
    is_open: bool
    status: Literal["ACTIVE", "IDLE", "UNKNOWN", "LOCKED"]
    status_detail: ShortStr | None = None
    app_name: Annotated[str, Field(max_length=260)] | None = None
    window_title: Annotated[str, Field(max_length=256)] | None = None
    domain: Annotated[str, Field(max_length=253)] | None = None
    privacy_excluded: bool
    start_reason: ShortStr
    end_reason: ShortStr | None = None


class IdlePeriodData(_Strict):
    session_id: UUID
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_seconds: Seconds
    is_open: bool
    end_reason: ShortStr | None = None


class AppUsageDailyData(_Strict):
    # usage_date is the device's local calendar day; day_start_utc /
    # day_end_utc pin down exactly which UTC interval that day covered.
    usage_date: date
    day_start_utc: UtcDatetime
    day_end_utc: UtcDatetime
    app_name: Annotated[str, Field(min_length=1, max_length=260)]
    active_seconds: Seconds
    idle_seconds: Seconds
    unknown_seconds: Seconds
    period_count: Annotated[int, Field(ge=0)]


class _RecordBase(_Strict):
    record_id: UUID
    record_version: Annotated[int, Field(ge=1)]
    device_id: Identifier
    employee_id: Identifier | None = None
    local_seq: Annotated[int, Field(ge=0)]
    created_at: UtcDatetime
    updated_at: UtcDatetime


class WorkSessionRecord(_RecordBase):
    record_type: Literal["work_session"]
    data: WorkSessionData


class ActivityPeriodRecord(_RecordBase):
    record_type: Literal["activity_period"]
    data: ActivityPeriodData


class IdlePeriodRecord(_RecordBase):
    record_type: Literal["idle_period"]
    data: IdlePeriodData


class AppUsageDailyRecord(_RecordBase):
    record_type: Literal["app_usage_daily"]
    data: AppUsageDailyData


SyncRecord = Annotated[
    Union[WorkSessionRecord, ActivityPeriodRecord, IdlePeriodRecord, AppUsageDailyRecord],
    Field(discriminator="record_type"),
]
SYNC_RECORD_ADAPTER: TypeAdapter[Any] = TypeAdapter(SyncRecord)


# ─── batch envelope ────────────────────────────────────────────────────────


class SyncBatchRequest(_Strict):
    batch_id: UUID
    device_id: Identifier
    agent_version: Annotated[str, Field(min_length=1, max_length=32)]
    sent_at: UtcDatetime
    # Validated one by one (SYNC_RECORD_ADAPTER) so a bad record is rejected
    # individually rather than failing the whole batch.
    records: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=MAX_RECORDS_PER_BATCH)]


class RecordResult(_Lenient):
    record_type: str
    record_id: str
    record_version: int | None = None
    status: ResultStatus
    server_version: int | None = None
    error: str | None = None


class BatchSummary(_Lenient):
    accepted: int = 0
    updated: int = 0
    already_current: int = 0
    stale: int = 0
    conflict: int = 0
    rejected: int = 0


class SyncBatchResponse(_Lenient):
    batch_id: UUID
    server_time: AwareDatetime
    results: list[RecordResult]
    summary: BatchSummary


class HealthResponse(_Lenient):
    status: Literal["ok"]
    api_version: str
    server_time: AwareDatetime


class DeviceInfoResponse(_Lenient):
    device_id: str
    employee_id: str
    status: str


class ErrorResponse(_Lenient):
    error: str
    detail: str | None = None
