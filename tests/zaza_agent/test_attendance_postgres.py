"""Phase 5 on PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL, see conftest.py).

Synced records go in through the Phase 4 repository exactly as the API
writes them; summaries come out of daily/weekly/monthly_summaries. The
results must equal the in-memory engine's for the same inputs.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from .conftest import pg_admin_execute, pg_test_settings

psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402

from deskmate.zaza.sync.protocol import SYNC_RECORD_ADAPTER  # noqa: E402
from deskmate.zaza_server.attendance import (  # noqa: E402
    AttendanceStatus,
    DataQuality,
    SummaryService,
)
from deskmate.zaza_server.attendance.models import (  # noqa: E402
    CALCULATION_VERSION,
    DeviceInput,
    PeriodInput,
    SessionInput,
)
from deskmate.zaza_server.attendance.postgres_store import PostgresAttendanceStore  # noqa: E402
from deskmate.zaza_server.attendance.store import InMemoryAttendanceStore  # noqa: E402
from deskmate.zaza_server.auth import register_device  # noqa: E402
from deskmate.zaza_server.repository import IncomingRecord  # noqa: E402
from deskmate.zaza_server.service import content_hash  # noqa: E402

S = AttendanceStatus
H = 3600
UTC = timezone.utc
TZ = "Asia/Dhaka"
MON = date(2026, 10, 5)
LATER = datetime(2026, 12, 1, tzinfo=UTC)


def at(day: date, hhmm: str, tz: str = TZ) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm)).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def iso(d: datetime) -> str:
    return d.isoformat()


class Synced:
    """Writes wire records through PostgresRepository.upsert_records."""

    def __init__(self, repo) -> None:  # noqa: ANN001
        self.repo = repo
        self.seq = 0

    def _put(self, record_type: str, record_id: str, version: int, data: dict, created: datetime) -> str:
        self.seq += 1
        raw = {"record_type": record_type, "record_id": record_id, "record_version": version,
               "device_id": "device-1", "employee_id": "emp-1", "local_seq": self.seq,
               "created_at": iso(created), "updated_at": iso(created), "data": data}
        model = SYNC_RECORD_ADAPTER.validate_python(raw)
        outcome = self.repo.upsert_records([IncomingRecord(
            record_type, record_id, version, "device-1", "emp-1", self.seq, content_hash(model),
            model.model_dump_json(), str(uuid.uuid4()))])[0]
        assert outcome.status in ("accepted", "updated"), outcome
        return record_id

    def session(self, start: datetime, end: datetime | None, status: str = "CLOSED", sid: str | None = None) -> str:
        data = {"started_at": iso(start), "ended_at": iso(end) if end else None,
                "last_heartbeat_at": iso(end or start), "status": status, "start_reason": "AGENT_START",
                "end_reason": None if status == "OPEN" else "LOGOFF", "previous_session_id": None,
                "tracked_seconds": 0.0, "active_seconds": 0.0, "idle_seconds": 0.0, "unknown_seconds": 0.0,
                "locked_seconds": 0.0}
        return self._put("work_session", sid or str(uuid.uuid4()), 1, data, start)

    def period(self, session_id: str, status: str, start: datetime, end: datetime, *, rid: str | None = None,
               version: int = 1) -> str:
        data = {"session_id": session_id, "started_at": iso(start), "ended_at": iso(end),
                "duration_seconds": (end - start).total_seconds(), "is_open": False, "status": status,
                "status_detail": "MONITORING_UNAVAILABLE" if status == "UNKNOWN" else None,
                "app_name": "Code.exe", "window_title": "x", "domain": None, "privacy_excluded": False,
                "start_reason": "SESSION_START", "end_reason": "APP_CHANGE"}
        return self._put("activity_period", rid or str(uuid.uuid4()), version, data, start)


@pytest.fixture
def world(pg_repo):
    pg_repo.add_employee("emp-1", "Employee One", timezone=TZ)
    register_device(pg_repo, "device-1", "emp-1")
    with pg_repo.connection() as conn:  # the device has synced since: absence can be concluded
        conn.execute("UPDATE devices SET last_seen_at = %s", (LATER,))
    store = PostgresAttendanceStore(pg_repo)
    for dow in range(1, 6):
        store.add_schedule("emp-1", timezone=TZ, is_working_day=True, day_of_week=dow,
                           effective_from=date(2026, 1, 1), start_time=time(9), end_time=time(17),
                           expected_work_seconds=8 * H)
    for dow in (6, 7):
        store.add_schedule("emp-1", timezone=TZ, is_working_day=False, day_of_week=dow,
                           effective_from=date(2026, 1, 1))
    service = SummaryService(store, clock=lambda: LATER)
    return pg_repo, store, service, Synced(pg_repo)


def mirror(store: PostgresAttendanceStore) -> InMemoryAttendanceStore:
    """The same inputs, loaded into the in-memory store."""
    mem = InMemoryAttendanceStore()
    for e in store.list_employees(active_only=False):
        mem.add_employee(e.employee_id, e.timezone, is_active=e.is_active)
        for r in store.schedules(e.employee_id):
            mem.add_rule(r)
        for d in store.devices(e.employee_id):
            mem.add_device(e.employee_id, d)
        lo, hi = datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
        for p in store.periods(e.employee_id, lo, hi):
            mem.add_period(e.employee_id, p)
        for s in store.sessions(e.employee_id, lo, hi):
            mem.add_session(e.employee_id, s)
    return mem


def test_migration_0002_is_forward_and_preserves_phase4_data():
    from deskmate.zaza_server.postgres import PostgresRepository, migrate

    settings = pg_test_settings("zaza_pytest_mig5")
    pg_admin_execute(settings, "DROP SCHEMA IF EXISTS zaza_pytest_mig5 CASCADE", "CREATE SCHEMA zaza_pytest_mig5")
    try:
        migrate.upgrade(settings, "0001_initial")  # a Phase 4 database
        with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
            conn.execute("INSERT INTO employees (employee_id, display_name) VALUES ('kept', 'Kept')")
        with pytest.raises(Exception, match="migrate"):
            PostgresRepository(settings)  # Phase 5 server refuses a Phase 4 schema
        migrate.upgrade(settings)
        migrate.upgrade(settings)  # repeat: no-op
        repo = PostgresRepository(settings)
        assert repo.get_employee("kept") is not None
        repo.close()
    finally:
        pg_admin_execute(settings, "DROP SCHEMA IF EXISTS zaza_pytest_mig5 CASCADE")


def test_postgres_results_equal_in_memory_results(world):
    repo, store, service, sync = world
    s1 = sync.session(at(MON, "08:50"), at(MON, "12:00"), status="INTERRUPTED")
    sync.period(s1, "UNKNOWN", at(MON, "08:50"), at(MON, "09:20"))
    sync.period(s1, "ACTIVE", at(MON, "09:20"), at(MON, "11:00"))
    sync.period(s1, "IDLE", at(MON, "11:00"), at(MON, "11:30"))
    sync.period(s1, "ACTIVE", at(MON, "11:30"), at(MON, "12:00"))
    s2 = sync.session(at(MON, "12:10"), at(MON, "18:00"))
    sync.period(s2, "LOCKED", at(MON, "12:10"), at(MON, "13:00"))
    sync.period(s2, "ACTIVE", at(MON, "13:00"), at(MON, "18:00"))
    s3 = sync.session(at(MON + timedelta(days=1), "09:45"), at(MON + timedelta(days=1), "16:00"))
    sync.period(s3, "ACTIVE", at(MON + timedelta(days=1), "09:45"), at(MON + timedelta(days=1), "16:00"))
    reference = SummaryService(mirror(store), clock=lambda: LATER)
    for d in (MON, MON + timedelta(days=1), MON + timedelta(days=2), MON + timedelta(days=5)):
        stored = service.calculate_daily("emp-1", d)
        assert store.get_daily("emp-1", d) == stored == reference.calculate_daily("emp-1", d)
    week = service.calculate_week("emp-1", MON)
    assert store.get_period("emp-1", "WEEK", MON) == week == reference.calculate_week("emp-1", MON)
    month = service.calculate_month("emp-1", MON)
    assert store.get_period("emp-1", "MONTH", date(2026, 10, 1)) == month
    mon = store.get_daily("emp-1", MON)
    assert mon.attendance_status is S.PRESENT and "START_UNCERTAIN" in mon.quality_flags
    assert (mon.session_count, mon.locked_seconds, mon.overtime_seconds) == (2, 50 * 60, H)
    assert store.get_daily("emp-1", MON + timedelta(days=1)).attendance_status is S.LATE_AND_EARLY
    assert store.get_daily("emp-1", MON + timedelta(days=2)).attendance_status is S.ABSENT


def test_recalculation_is_idempotent_in_postgres(world, pg_settings):
    repo, store, service, sync = world
    sid = sync.session(at(MON, "09:00"), at(MON, "17:00"))
    sync.period(sid, "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    service.calculate_daily("emp-1", MON)
    service.calculate_week("emp-1", MON)
    with repo.connection() as conn:
        before = conn.execute("SELECT updated_at FROM daily_summaries WHERE local_date = %s", (MON,)).fetchone()
    assert store.save_daily(service.compute_daily("emp-1", MON)) is False
    first = service.recalculate(MON, MON, ["emp-1"])  # fills the rest of the month once
    assert first.days == 31 and first.days_changed == 31 - 7
    assert service.recalculate(MON, MON, ["emp-1"]).days_changed == 0
    with repo.connection() as conn:
        rows = conn.execute("SELECT updated_at FROM daily_summaries WHERE local_date = %s", (MON,)).fetchall()
        weeks = conn.execute("SELECT count(*) AS n FROM weekly_summaries").fetchone()["n"]
    assert len(rows) == 1 and rows[0]["updated_at"] == before["updated_at"] and weeks == 1


def test_higher_synced_version_changes_the_summary(world):
    repo, store, service, sync = world
    sid = sync.session(at(MON, "09:00"), at(MON, "17:00"))
    rid = sync.period(sid, "ACTIVE", at(MON, "09:00"), at(MON, "12:00"))
    assert service.calculate_daily("emp-1", MON).attendance_status is S.EARLY_LEAVE
    sync.period(sid, "ACTIVE", at(MON, "09:00"), at(MON, "17:00"), rid=rid, version=2)
    assert store.save_daily(service.compute_daily("emp-1", MON)) is True
    stored = store.get_daily("emp-1", MON)
    assert stored.attendance_status is S.PRESENT and stored.active_seconds == 8 * H


def test_overnight_and_dst_schedules_in_postgres(pg_repo):
    ny = "America/New_York"
    pg_repo.add_employee("emp-1", "Employee One", timezone=ny)
    register_device(pg_repo, "device-1", "emp-1")
    store = PostgresAttendanceStore(pg_repo)
    sat = date(2026, 3, 7)  # DST begins Sunday 2026-03-08
    store.add_schedule("emp-1", timezone=ny, is_working_day=True, schedule_date=sat, start_time=time(22),
                       end_time=time(6), expected_work_seconds=8 * H)
    sync = Synced(pg_repo)
    sid = sync.session(at(sat, "21:30", ny), at(sat + timedelta(days=1), "06:30", ny))
    sync.period(sid, "ACTIVE", at(sat, "21:30", ny), at(sat + timedelta(days=1), "06:30", ny))
    s = SummaryService(store, clock=lambda: LATER).calculate_daily("emp-1", sat)
    assert s.scheduled_start == datetime(2026, 3, 8, 3, 0, tzinfo=UTC)
    assert s.scheduled_end == datetime(2026, 3, 8, 10, 0, tzinfo=UTC)
    assert (s.shift_span_seconds, s.scheduled_seconds, s.overtime_seconds) == (7 * H, 7 * H, H)
    assert store.get_daily("emp-1", sat).timezone == ny


def test_summary_constraints(world, pg_settings):
    repo, store, service, _ = world
    service.calculate_daily("emp-1", MON)
    service.calculate_week("emp-1", MON)
    bad = [
        "UPDATE daily_summaries SET idle_seconds = idle_seconds + 1",               # tracked != sum
        "UPDATE daily_summaries SET attendance_status = 'SICK'",
        "UPDATE daily_summaries SET late_seconds = 60",                            # ABSENT can't be late
        "UPDATE daily_summaries SET attendance_percentage = 101",
        "UPDATE daily_summaries SET worked_day = true",                             # no active time
        "UPDATE daily_summaries SET attendance_status = 'DAY_OFF'",                # it is a working day
        "UPDATE daily_summaries SET summary_hash = 'x'",
        "UPDATE weekly_summaries SET week_start = week_start + 1, week_end = week_end + 1",  # not a Monday
        "UPDATE weekly_summaries SET absent_days = 9",
    ]
    for statement in bad:
        with pytest.raises(errors.CheckViolation):
            with psycopg.connect(**pg_settings.connect_kwargs(), autocommit=True) as conn:
                conn.execute(statement)


def test_schedule_admin_is_validated_and_audited(world):
    repo, store, _, _ = world
    with pytest.raises(ValueError, match="work_schedules_hours_valid"):
        store.add_schedule("emp-1", timezone=TZ, is_working_day=True, schedule_date=MON, start_time=time(9),
                           end_time=time(9), expected_work_seconds=H)
    store.add_schedule("emp-1", timezone=TZ, is_working_day=True, schedule_date=MON, start_time=time(20),
                       end_time=time(4), expected_work_seconds=8 * H)
    created = repo.list_audit(entity_type="work_schedule")
    assert len(created) == 8 and created[-1]["action"] == "work_schedule.create"
    assert created[-1]["new_values"]["start_time"] == "20:00:00"


def test_cli_schedule_and_summary_commands(world, pg_settings, monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    repo, store, _, sync = world
    s = pg_settings
    for k, v in {"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_HOST": s.host, "ZAZA_DB_PORT": str(s.port),
                 "ZAZA_DB_NAME": s.dbname, "ZAZA_DB_USER": s.user, "ZAZA_DB_PASSWORD": s.password,
                 "ZAZA_DB_SCHEMA": s.schema}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    repo.add_employee("emp-2", "Night Worker", timezone=TZ)
    assert main(["add-schedule", "--employee-id", "emp-2", "--weekdays", "1-5", "--start", "20:00", "--end",
                 "04:00", "--expected-hours", "8", "--timezone", TZ, "--effective-from", "2026-01-01"]) == 0
    assert main(["list-schedules", "--employee-id", "emp-2"]) == 0
    assert "20:00-04:00 expected 8h" in capsys.readouterr().out
    sid = sync.session(at(MON, "09:00"), at(MON, "17:00"))
    sync.period(sid, "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    assert main(["summarize-day", "--date", str(MON), "--employee-id", "emp-1"]) == 0
    out = capsys.readouterr().out
    assert "PRESENT" in out and s.password not in out
    assert main(["summarize-week", "--date", str(MON)]) == 0
    assert "emp-2" in capsys.readouterr().out  # every active employee by default
    assert main(["summarize-month", "--month", "2026-10", "--employee-id", "emp-1"]) == 0
    assert "MONTH 2026-10-01..2026-10-31" in capsys.readouterr().out
    assert main(["recalculate", "--from", str(MON), "--to", str(MON)]) == 0
    assert "Recalculated" in capsys.readouterr().out
    with repo.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM daily_summaries WHERE local_date = %s", (MON,)).fetchone()["n"]
    assert n == 2


def test_policy_is_recorded_with_each_summary(world):
    repo, store, service, _ = world
    service.calculate_daily("emp-1", MON)
    with repo.connection() as conn:
        row = conn.execute("SELECT policy, calculation_version FROM daily_summaries").fetchone()
    assert row["policy"]["late_grace_seconds"] == 0 and row["calculation_version"] == CALCULATION_VERSION == 2
    assert json.dumps(row["policy"])  # plain JSON object


def test_unused_input_types_load(world):
    _, store, _, sync = world
    sid = sync.session(at(MON, "09:00"), None, status="OPEN")
    sessions = store.sessions("emp-1", at(MON, "00:00"), at(MON, "23:59"))
    assert sessions == [SessionInput(sid, "device-1", "OPEN", at(MON, "09:00"), None, at(MON, "09:00"))]
    assert store.devices("emp-1")[0] == DeviceInput("device-1", "ACTIVE", LATER)
    assert store.periods("emp-1", at(MON, "00:00"), at(MON, "23:59")) == []
    assert isinstance(PeriodInput, type) and DataQuality.COMPLETE.value == "COMPLETE"


# ─── review fixes ──────────────────────────────────────────────────────────


def test_schedule_timezone_must_equal_employee_timezone(world, pg_settings):
    repo, store, _, _ = world
    sid = store.add_schedule("emp-1", is_working_day=True, schedule_date=MON, start_time=time(20),
                             end_time=time(4), expected_work_seconds=8 * H)  # overnight, default timezone
    assert {r.timezone for r in store.schedules("emp-1") if r.schedule_id == sid} == {TZ}
    store.add_schedule("emp-1", timezone=TZ, is_working_day=False, schedule_date=MON + timedelta(days=1))
    with pytest.raises(ValueError, match="differs from employee 'emp-1' timezone 'Asia/Dhaka'"):
        store.add_schedule("emp-1", timezone="Europe/London", is_working_day=False,
                           schedule_date=MON + timedelta(days=2))
    with pytest.raises(ValueError, match="unknown employee"):
        store.add_schedule("nobody", is_working_day=False, schedule_date=MON)
    # Direct SQL can't bypass it, in either direction.
    with pytest.raises(errors.ForeignKeyViolation, match="work_schedules_timezone_matches_employee"):
        with psycopg.connect(**pg_settings.connect_kwargs(), autocommit=True) as conn:
            conn.execute("INSERT INTO work_schedules (employee_id, schedule_date, is_working_day, timezone) "
                         "VALUES ('emp-1', '2026-12-25', false, 'Europe/London')")
    with pytest.raises(errors.ForeignKeyViolation):
        with psycopg.connect(**pg_settings.connect_kwargs(), autocommit=True) as conn:
            conn.execute("UPDATE employees SET timezone = 'Europe/London' WHERE employee_id = 'emp-1'")


def test_migration_0003_refuses_existing_mismatched_schedules():
    from deskmate.zaza_server.postgres import migrate

    settings = pg_test_settings("zaza_pytest_mig5tz")
    pg_admin_execute(settings, "DROP SCHEMA IF EXISTS zaza_pytest_mig5tz CASCADE", "CREATE SCHEMA zaza_pytest_mig5tz")
    try:
        migrate.upgrade(settings, "0002_attendance_summaries")
        with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
            conn.execute("INSERT INTO employees (employee_id, display_name, timezone) VALUES ('e', 'E', 'Asia/Dhaka')")
            conn.execute("INSERT INTO work_schedules (employee_id, schedule_date, is_working_day, timezone) "
                         "VALUES ('e', '2026-12-25', false, 'Europe/London')")
        with pytest.raises(Exception, match="different from their employee"):
            migrate.upgrade(settings)
        with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
            conn.execute("UPDATE work_schedules SET timezone = 'Asia/Dhaka'")
        migrate.upgrade(settings)
        with psycopg.connect(**settings.connect_kwargs(), autocommit=True) as conn:
            assert migrate.current_revision(conn) == migrate.head_revision()  # 0003 applied, and later ones
    finally:
        pg_admin_execute(settings, "DROP SCHEMA IF EXISTS zaza_pytest_mig5tz CASCADE")


def test_cli_schedule_timezone_defaults_and_recent_days(world, pg_settings, monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    repo, store, _, _ = world
    s = pg_settings
    for k, v in {"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_HOST": s.host, "ZAZA_DB_PORT": str(s.port),
                 "ZAZA_DB_NAME": s.dbname, "ZAZA_DB_USER": s.user, "ZAZA_DB_PASSWORD": s.password,
                 "ZAZA_DB_SCHEMA": s.schema}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    repo.add_employee("emp-ny", "New York", timezone="America/New_York")
    assert main(["add-schedule", "--employee-id", "emp-ny", "--weekdays", "1-5", "--start", "09:00", "--end",
                 "17:00", "--expected-hours", "8"]) == 0  # no --timezone: the employee's
    out = capsys.readouterr().out
    ny_today = datetime.now(ZoneInfo("America/New_York")).date()
    assert f"from {ny_today} (America/New_York)" in out
    assert {r.timezone for r in store.schedules("emp-ny")} == {"America/New_York"}
    assert {r.effective_from for r in store.schedules("emp-ny")} == {ny_today}
    assert main(["add-schedule", "--employee-id", "emp-ny", "--date", "2026-12-25", "--day-off",
                 "--timezone", "Asia/Dhaka"]) == 1
    assert "schedules must use the employee's timezone" in capsys.readouterr().err
    assert main(["recalculate", "--recent-days", "0"]) == 1
    assert ">= 1" in capsys.readouterr().err
    assert main(["recalculate", "--recent-days", "1", "--employee-id", "emp-ny"]) == 0
    with repo.connection() as conn:
        latest = conn.execute("SELECT max(local_date) AS d FROM daily_summaries WHERE employee_id = 'emp-ny'"
                              ).fetchone()["d"]
    assert latest == datetime.now(ZoneInfo("America/New_York")).date()  # nothing in the future
