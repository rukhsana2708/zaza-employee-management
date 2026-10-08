"""PostgreSQL integration tests (opt-in).

Skipped unless ZAZA_TEST_POSTGRES_URL names a disposable database whose name
contains "test". They create and drop their own schemas (``zaza_pytest*``)
and never touch anything else. See conftest.py.

The shared API conformance suite (test_sync_server.py) also runs against
PostgreSQL when the variable is set.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import threading
import uuid
from datetime import datetime, timezone

import pytest

from .conftest import PG_TEST_SCHEMA, SyncServer, pg_admin_execute, pg_test_settings, work_session
from .test_sync_server import SESSION_ID, TS, TS2, period, session

psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402

from deskmate.zaza.sync.protocol import SYNC_RECORD_ADAPTER  # noqa: E402
from deskmate.zaza_server.auth import (  # noqa: E402
    AuthError,
    authenticate_request,
    register_device,
    rotate_token,
)
from deskmate.zaza_server.repository import (  # noqa: E402
    ORPHAN_PERIOD_ERROR,
    IncomingRecord,
    RepositoryUnavailable,
)
from deskmate.zaza_server.service import content_hash  # noqa: E402

SYNCED_TABLES = ("work_sessions", "activity_periods", "idle_periods", "application_usage_daily")


# ─── helpers ───────────────────────────────────────────────────────────────


def incoming(record: dict, batch_id: str | None = None) -> IncomingRecord:
    model = SYNC_RECORD_ADAPTER.validate_python(record)
    return IncomingRecord(
        record_type=model.record_type, record_id=str(model.record_id), record_version=model.record_version,
        device_id=model.device_id, employee_id=model.employee_id, local_seq=model.local_seq,
        content_hash=content_hash(model), payload_json=model.model_dump_json(),
        batch_id=batch_id or str(uuid.uuid4()),
    )


def idle(record_id: str | None = None, version: int = 1, **data) -> dict:
    return {
        "record_type": "idle_period", "record_id": record_id or str(uuid.uuid4()), "record_version": version,
        "device_id": "device-1", "employee_id": "emp-1", "local_seq": 5, "created_at": TS, "updated_at": TS2,
        "data": {"session_id": SESSION_ID, "started_at": TS, "ended_at": TS2, "duration_seconds": 300.0,
                 "is_open": False, "end_reason": "INPUT", **data},
    }


def usage(record_id: str | None = None, version: int = 1, **data) -> dict:
    return {
        "record_type": "app_usage_daily", "record_id": record_id or str(uuid.uuid4()), "record_version": version,
        "device_id": "device-1", "employee_id": "emp-1", "local_seq": 6, "created_at": TS, "updated_at": TS2,
        "data": {"usage_date": "2026-10-07", "day_start_utc": "2026-10-06T18:00:00+00:00",
                 "day_end_utc": "2026-10-07T18:00:00+00:00", "app_name": "Code.exe", "active_seconds": 120.0,
                 "idle_seconds": 30.0, "unknown_seconds": 0.0, "period_count": 3, **data},
    }


def query(settings, sql: str, params: tuple = (), *, tz: str | None = None) -> list[dict]:
    from psycopg.rows import dict_row

    with psycopg.connect(**settings.connect_kwargs(), autocommit=True, row_factory=dict_row) as conn:
        if tz:
            conn.execute(f"SET TIME ZONE '{tz}'")
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def execute_expect(settings, error: type[Exception], sql: str, params: tuple = ()) -> None:
    with pytest.raises(error):
        query(settings, sql, params)


@pytest.fixture
def seeded(pg_repo):
    """emp-1 / device-1 registered and SESSION_ID synced; returns (repo, token)."""
    pg_repo.add_employee("emp-1", "Employee One", timezone="Asia/Dhaka")
    token = register_device(pg_repo, "device-1", "emp-1").token
    assert [o.status for o in pg_repo.upsert_records([incoming(session())])] == ["accepted"]
    return pg_repo, token


# ─── employees, devices, tokens ────────────────────────────────────────────


def test_employee_device_foreign_keys(pg_repo, pg_settings):
    with pytest.raises(ValueError, match="unknown employee"):
        pg_repo.add_device("device-1", "ghost")
    execute_expect(pg_settings, errors.ForeignKeyViolation,
                   "INSERT INTO devices (device_id, employee_id) VALUES ('d-x', 'ghost')")
    pg_repo.add_employee("emp-1", "Employee One")
    pg_repo.add_device("device-1", "emp-1", display_name="Front desk PC")
    assert pg_repo.get_device("device-1").display_name == "Front desk PC"
    # an employee with devices can't be deleted out from under them
    execute_expect(pg_settings, errors.ForeignKeyViolation, "DELETE FROM employees WHERE employee_id = 'emp-1'")


def test_plaintext_device_token_is_never_persisted(pg_repo, pg_settings):
    pg_repo.add_employee("emp-1", "Employee One")
    first = register_device(pg_repo, "device-1", "emp-1").token
    second = rotate_token(pg_repo, "device-1", revoke_old=True).token
    for table in ("employees", "devices", "device_tokens", "audit_logs"):
        dump = json.dumps([r["j"] for r in query(pg_settings, f"SELECT row_to_json(t)::text AS j FROM {table} t")])
        assert first not in dump and second not in dump, table
    # the column itself refuses anything that isn't a SHA-256 hex digest
    execute_expect(
        pg_settings, errors.CheckViolation,
        "INSERT INTO device_tokens (token_id, device_id, token_hash) VALUES (%s, 'device-1', %s)",
        (uuid.uuid4(), first),
    )


def test_valid_authenticated_device_lookup_updates_last_seen(seeded, pg_settings):
    repo, token = seeded
    before = query(pg_settings, "SELECT updated_at FROM devices WHERE device_id = 'device-1'")[0]["updated_at"]
    ctx = authenticate_request(repo, f"Bearer {token}", "device-1")
    assert ctx.device.device_id == "device-1" and ctx.device.employee_id == "emp-1"
    repo.touch_device("device-1", token_id=ctx.token_id)
    row = query(pg_settings, "SELECT last_seen_at, updated_at FROM devices WHERE device_id = 'device-1'")[0]
    assert row["last_seen_at"] is not None
    assert row["updated_at"] == before  # connectivity is not an admin edit
    used = query(pg_settings, "SELECT last_used_at FROM device_tokens WHERE token_id = %s", (ctx.token_id,))
    assert used[0]["last_used_at"] is not None


def test_disabled_device_and_revoked_token_are_refused(seeded, pg_settings):
    repo, token = seeded
    repo.set_device_status("device-1", "DISABLED")
    with pytest.raises(AuthError) as info:
        authenticate_request(repo, f"Bearer {token}", "device-1")
    assert info.value.status_code == 403 and info.value.error == "device disabled"
    assert query(pg_settings, "SELECT disabled_at FROM devices")[0]["disabled_at"] is not None
    repo.set_device_status("device-1", "ACTIVE")
    assert query(pg_settings, "SELECT disabled_at FROM devices")[0]["disabled_at"] is None
    new = rotate_token(repo, "device-1", revoke_old=True).token
    with pytest.raises(AuthError) as info:
        authenticate_request(repo, f"Bearer {token}", "device-1")
    assert info.value.status_code == 403 and info.value.error == "token revoked"
    assert authenticate_request(repo, f"Bearer {new}", "device-1").device.device_id == "device-1"
    execute_expect(pg_settings, errors.CheckViolation,
                   "UPDATE device_tokens SET status = 'REVOKED', revoked_at = NULL")


# ─── synchronized records: typed persistence ───────────────────────────────


def test_work_session_persistence(seeded, pg_settings):
    repo, _ = seeded
    closed = session(version=2, status="CLOSED", ended_at=TS2, end_reason="LOGOFF", tracked_seconds=300.0,
                     active_seconds=200.0, idle_seconds=60.0, unknown_seconds=25.0, locked_seconds=15.0)
    assert [o.status for o in repo.upsert_records([incoming(closed)])] == ["updated"]
    row = query(pg_settings, "SELECT * FROM work_sessions")[0]
    assert str(row["session_id"]) == SESSION_ID and row["record_version"] == 2
    assert row["status"] == "CLOSED" and row["end_reason"] == "LOGOFF"
    assert row["started_at"] == datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    assert row["ended_at"] == datetime(2026, 10, 7, 9, 5, tzinfo=timezone.utc)
    assert (row["active_seconds"], row["idle_seconds"], row["locked_seconds"]) == (200.0, 60.0, 15.0)
    assert row["employee_id"] == "emp-1" and row["device_id"] == "device-1"
    assert row["payload"]["data"]["status"] == "CLOSED"


def test_activity_period_persistence(seeded, pg_settings):
    repo, _ = seeded
    p = period(app="chrome.exe", window_title="Docs", domain="docs.example.com", status="ACTIVE")
    excluded = period(app="bank.exe", window_title="Excluded / Private", domain=None, privacy_excluded=True)
    assert [o.status for o in repo.upsert_records([incoming(p), incoming(excluded)])] == ["accepted", "accepted"]
    rows = {str(r["period_id"]): r for r in query(pg_settings, "SELECT * FROM activity_periods")}
    row = rows[p["record_id"]]
    assert (row["app_name"], row["window_title"], row["domain"], row["status"]) == (
        "chrome.exe", "Docs", "docs.example.com", "ACTIVE",
    )
    assert row["duration_seconds"] == 300.0 and row["is_open"] is False
    assert str(row["session_id"]) == SESSION_ID and row["device_id"] == "device-1"
    assert rows[excluded["record_id"]]["privacy_excluded"] is True


def test_idle_period_persistence(seeded, pg_settings):
    repo, _ = seeded
    i = idle(duration_seconds=180.0, end_reason="LOCKED")
    assert [o.status for o in repo.upsert_records([incoming(i)])] == ["accepted"]
    row = query(pg_settings, "SELECT * FROM idle_periods")[0]
    assert str(row["idle_id"]) == i["record_id"] and row["duration_seconds"] == 180.0
    assert row["end_reason"] == "LOCKED" and str(row["session_id"]) == SESSION_ID


def test_application_usage_persistence(seeded, pg_settings):
    repo, _ = seeded
    u = usage()
    assert [o.status for o in repo.upsert_records([incoming(u)])] == ["accepted"]
    row = query(pg_settings, "SELECT * FROM application_usage_daily")[0]
    assert str(row["usage_date"]) == "2026-10-07" and row["app_name"] == "Code.exe"
    assert row["day_start_utc"] == datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
    assert (row["active_seconds"], row["idle_seconds"], row["period_count"]) == (120.0, 30.0, 3)
    # a second row for the same device/day/app under another id is refused
    dup = usage(active_seconds=1.0)
    outcome = repo.upsert_records([incoming(dup)])[0]
    assert outcome.status == "rejected" and "device_day_app_unique" in outcome.error


# ─── idempotency semantics at the row level ────────────────────────────────


def test_idempotency_rows(seeded, pg_settings):
    repo, _ = seeded
    rid = str(uuid.uuid4())
    v1 = incoming(period(rid, 1, app="one.exe"))
    assert repo.upsert_records([v1])[0].status == "accepted"
    first = query(pg_settings, "SELECT * FROM activity_periods WHERE period_id = %s", (rid,))[0]
    assert repo.upsert_records([v1])[0].status == "already_current"
    assert repo.upsert_records([incoming(period(rid, 3, app="three.exe"))])[0].status == "updated"
    stale = repo.upsert_records([incoming(period(rid, 2, app="two.exe"))])[0]
    assert (stale.status, stale.server_version) == ("stale", 3)
    conflict = repo.upsert_records([incoming(period(rid, 3, app="other.exe"))])[0]
    assert (conflict.status, conflict.server_version) == ("conflict", 3)
    row = query(pg_settings, "SELECT * FROM activity_periods WHERE period_id = %s", (rid,))[0]
    assert row["record_version"] == 3 and row["app_name"] == "three.exe"
    assert row["first_received_at"] == first["first_received_at"]
    assert row["last_received_at"] >= first["last_received_at"]
    assert query(pg_settings, "SELECT count(*) AS n FROM activity_periods")[0]["n"] == 1


# ─── concurrency ───────────────────────────────────────────────────────────


def _race(repo, records: list[IncomingRecord]) -> list[str]:
    barrier = threading.Barrier(len(records))
    results: list[str | None] = [None] * len(records)

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = repo.upsert_records([records[i]])[0].status

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(records))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_concurrent_version_submissions_never_lose_the_highest(seeded, pg_settings):
    repo, _ = seeded
    for round_ in range(20):  # 160 racing submissions; caught a real now()-vs-lock-wait bug
        rid = str(uuid.uuid4())
        versions = list(range(1, 9))
        random.Random(round_).shuffle(versions)
        statuses = _race(repo, [incoming(period(rid, v, app=f"v{v}.exe")) for v in versions])
        row = query(pg_settings, "SELECT record_version, app_name FROM activity_periods WHERE period_id = %s",
                    (rid,))[0]
        assert (row["record_version"], row["app_name"]) == (8, "v8.exe"), statuses
        assert statuses.count("accepted") == 1
        assert set(statuses) <= {"accepted", "updated", "stale"}


def test_concurrent_identical_submissions_insert_once(seeded, pg_settings):
    repo, _ = seeded
    rid = str(uuid.uuid4())
    statuses = _race(repo, [incoming(period(rid, 1)) for _ in range(8)])
    assert statuses.count("accepted") == 1 and statuses.count("already_current") == 7
    assert query(pg_settings, "SELECT count(*) AS n FROM activity_periods")[0]["n"] == 1


def test_concurrent_same_version_different_content_keeps_the_winner(seeded, pg_settings):
    repo, _ = seeded
    rid = str(uuid.uuid4())
    records = [incoming(period(rid, 1, app=f"app{i}.exe")) for i in range(6)]
    statuses = _race(repo, records)
    assert statuses.count("accepted") == 1 and statuses.count("conflict") == 5
    winner = records[statuses.index("accepted")]
    stored = query(pg_settings, "SELECT content_hash FROM activity_periods WHERE period_id = %s", (rid,))[0]
    assert stored["content_hash"] == winner.content_hash


def test_deadlock_rolls_back_whole_batch_and_retries(seeded, pg_settings, monkeypatch):
    repo, _ = seeded
    from deskmate.zaza_server.postgres import repository as pg_module

    original = pg_module.PostgresRepository._upsert_one
    calls = {"n": 0}

    def flaky(self, conn, rec):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 2:  # second record of the first attempt
            raise errors.DeadlockDetected("simulated deadlock")
        return original(self, conn, rec)

    monkeypatch.setattr(pg_module.PostgresRepository, "_upsert_one", flaky)
    a, b = period(), period()
    outcomes = repo.upsert_records([incoming(a), incoming(b)])
    assert [o.status for o in outcomes] == ["accepted", "accepted"]  # 'a' was rolled back, then re-inserted
    assert calls["n"] == 4
    assert query(pg_settings, "SELECT count(*) AS n FROM activity_periods")[0]["n"] == 2


# ─── partial batches & per-record rollback ─────────────────────────────────


def test_partial_batch_rolls_back_only_the_failing_record(seeded, pg_settings):
    repo, _ = seeded
    good1 = period(app="good1.exe")
    bad = period(window_title="VERY-PRIVATE-TITLE", started_at=TS2, ended_at=TS)  # end before start: DB CHECK
    good2 = period(app="good2.exe")
    orphan = period(session_id=str(uuid.uuid4()))
    outcomes = repo.upsert_records([incoming(r) for r in (good1, bad, good2, orphan)])
    assert [o.status for o in outcomes] == ["accepted", "rejected", "accepted", "rejected"]
    assert "activity_periods_end_after_start" in outcomes[1].error
    assert "VERY-PRIVATE-TITLE" not in outcomes[1].error  # row data is never echoed
    assert outcomes[3].error == ORPHAN_PERIOD_ERROR
    stored = {str(r["period_id"]) for r in query(pg_settings, "SELECT period_id FROM activity_periods")}
    assert stored == {good1["record_id"], good2["record_id"]}


def test_no_cross_device_record_ownership_corruption(seeded, pg_settings):
    repo, _ = seeded
    repo.add_employee("emp-2", "Employee Two")
    register_device(repo, "device-2", "emp-2")
    rid = str(uuid.uuid4())
    repo.upsert_records([incoming(period(rid, 1))])
    takeover = dict(period(rid, 9, device_id="device-2", employee_id="emp-2"))
    outcome = repo.upsert_records([incoming(takeover)])[0]
    assert outcome.status == "rejected"
    row = query(pg_settings, "SELECT device_id, employee_id, record_version FROM activity_periods "
                             "WHERE period_id = %s", (rid,))[0]
    assert (row["device_id"], row["employee_id"], row["record_version"]) == ("device-1", "emp-1", 1)
    # and the composite FK stops a device-2 row pointing at device-1's session at the DB level
    execute_expect(
        pg_settings, errors.ForeignKeyViolation,
        "UPDATE activity_periods SET device_id = 'device-2', employee_id = 'emp-2' WHERE period_id = %s", (rid,),
    )


# ─── time handling ─────────────────────────────────────────────────────────


def test_all_timestamp_columns_are_timestamptz_and_utc_roundtrips(seeded, pg_settings):
    repo, _ = seeded
    cols = query(
        pg_settings,
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND (column_name LIKE '%%\\_at' OR column_name LIKE '%%\\_utc')",
        (PG_TEST_SCHEMA,),
    )
    assert len(cols) > 20
    assert {(c["table_name"], c["column_name"]) for c in cols if c["data_type"] != "timestamp with time zone"} == set()
    p = period(started_at="2026-10-07T23:30:00+00:00", ended_at="2026-10-07T23:59:00+00:00")
    repo.upsert_records([incoming(p)])
    for tz in ("UTC", "Asia/Dhaka", "America/New_York"):
        row = query(pg_settings, "SELECT started_at FROM activity_periods WHERE period_id = %s",
                    (p["record_id"],), tz=tz)[0]
        assert row["started_at"] == datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)  # same instant


# ─── constraints (not just Python validation) ──────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE activity_periods SET duration_seconds = -1",
        "UPDATE activity_periods SET duration_seconds = 'NaN'",
        "UPDATE activity_periods SET record_version = 0",
        "UPDATE activity_periods SET ended_at = started_at - interval '1 second'",
        "UPDATE activity_periods SET status = 'BUSY'",
        "UPDATE activity_periods SET privacy_excluded = true, window_title = 'Bank statement - March'",
        "UPDATE activity_periods SET content_hash = 'not-a-hash'",
        "UPDATE activity_periods SET local_seq = -1",
        "UPDATE work_sessions SET tracked_seconds = -5",
        "UPDATE work_sessions SET ended_at = NULL, status = 'CLOSED'",
        "UPDATE work_sessions SET status = 'OPEN', ended_at = started_at",
        "UPDATE work_sessions SET record_version = -3",
        "UPDATE idle_periods SET duration_seconds = -0.5",
        "UPDATE application_usage_daily SET period_count = -1",
        "UPDATE application_usage_daily SET active_seconds = 100000",
        "UPDATE application_usage_daily SET day_end_utc = day_start_utc",
        "UPDATE devices SET status = 'LOST'",
        "UPDATE employees SET role = 'OWNER'",
    ],
)
def test_constraints_reject_invalid_values(seeded, pg_settings, sql):
    repo, _ = seeded
    repo.upsert_records([incoming(period()), incoming(idle()), incoming(usage())])
    execute_expect(pg_settings, errors.CheckViolation, sql)


def test_employee_timezone_validated_by_database(pg_repo, pg_settings):
    execute_expect(pg_settings, errors.InvalidParameterValue,
                   "INSERT INTO employees (employee_id, display_name, timezone) VALUES ('e', 'E', 'Mars/Base')")
    query(pg_settings, "INSERT INTO employees (employee_id, display_name, timezone) VALUES ('e', 'E', 'Asia/Dhaka')")


@pytest.mark.parametrize(
    "values, ok",
    [
        ("1, '2026-01-01', NULL, NULL, true, '09:00', '17:00', 28800", True),
        ("NULL, NULL, NULL, '2026-12-25', false, NULL, NULL, NULL", True),
        ("1, '2026-01-01', NULL, NULL, true, NULL, NULL, 28800", False),  # working day without hours
        ("1, '2026-01-01', NULL, NULL, true, '17:00', '09:00', 0", False),  # overnight, but expected 0
        ("1, '2026-01-01', NULL, NULL, true, '09:00', '10:00', 7200", False),  # expected > span
        ("1, '2026-01-01', NULL, NULL, false, '09:00', '17:00', 0", False),  # day off with hours
        ("8, '2026-01-01', NULL, NULL, false, NULL, NULL, NULL", False),  # day_of_week out of range
        ("1, '2026-01-01', NULL, '2026-01-05', false, NULL, NULL, NULL", False),  # both kinds
        ("1, '2026-02-01', '2026-01-01', NULL, false, NULL, NULL, NULL", False),  # effective_to < from
    ],
)
def test_work_schedule_constraints(seeded, pg_settings, values, ok):
    sql = ("INSERT INTO work_schedules (employee_id, day_of_week, effective_from, effective_to, schedule_date, "
           f"is_working_day, start_time, end_time, expected_work_seconds, timezone) VALUES ('emp-1', {values}, "
           "'Asia/Dhaka')")
    if ok:
        query(pg_settings, sql)
    else:
        execute_expect(pg_settings, errors.CheckViolation, sql)


# ─── audit log ─────────────────────────────────────────────────────────────


def test_audit_log_structure_and_append_only(pg_repo, pg_settings):
    pg_repo.add_employee("emp-1", "Employee One")
    first = register_device(pg_repo, "device-1", "emp-1")
    rotate_token(pg_repo, "device-1", revoke_old=True)
    pg_repo.set_device_status("device-1", "DISABLED")
    pg_repo.set_device_status("device-1", "ACTIVE")
    actions = [a["action"] for a in pg_repo.list_audit()]
    assert actions == [
        "employee.create", "device.register", "device_token.issue", "device_token.issue",
        "device_token.revoke", "device.disable", "device.enable",
    ]
    revoke = pg_repo.list_audit(entity_type="device_token", entity_id=first.token_id)[-1]
    assert revoke["actor_type"] == "CLI" and revoke["actor_id"] == "pytest"
    assert revoke["old_values"] == {"status": "ACTIVE"} and revoke["new_values"]["status"] == "REVOKED"
    assert revoke["occurred_at"].tzinfo is not None
    columns = {c["column_name"] for c in query(
        pg_settings, "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                     "AND table_name = 'audit_logs'", (PG_TEST_SCHEMA,))}
    assert columns == {"audit_id", "occurred_at", "actor_type", "actor_id", "action", "entity_type",
                       "entity_id", "old_values", "new_values", "request_id"}
    for statement in ("UPDATE audit_logs SET action = 'device.forged'", "DELETE FROM audit_logs",
                      "TRUNCATE audit_logs"):
        execute_expect(pg_settings, errors.RestrictViolation, statement)
    assert len(pg_repo.list_audit()) == len(actions)


# ─── migrations ────────────────────────────────────────────────────────────


@pytest.fixture
def empty_schema():
    settings = pg_test_settings("zaza_pytest_mig")
    pg_admin_execute(settings, "DROP SCHEMA IF EXISTS zaza_pytest_mig CASCADE", "CREATE SCHEMA zaza_pytest_mig")
    yield settings
    pg_admin_execute(settings, "DROP SCHEMA IF EXISTS zaza_pytest_mig CASCADE")


def test_migration_upgrade_from_empty_database_and_repeat(empty_schema):
    from deskmate.zaza_server.postgres import PostgresRepository, migrate

    with pytest.raises(RepositoryUnavailable, match="migrate"):
        PostgresRepository(empty_schema)  # server refuses to run on an unmigrated database
    migrate.upgrade(empty_schema)
    repo = PostgresRepository(empty_schema)
    repo.add_employee("emp-keep", "Kept Across Upgrades")
    migrate.upgrade(empty_schema)  # repeated: no-op, data preserved
    migrate.upgrade(empty_schema)
    assert repo.get_employee("emp-keep") is not None
    repo.close()
    tables = {r["table_name"] for r in query(
        empty_schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'zaza_pytest_mig'")}
    assert tables == {"alembic_version", "employees", "devices", "device_tokens", "work_schedules",
                      "work_sessions", "activity_periods", "idle_periods", "application_usage_daily", "audit_logs",
                      "daily_summaries", "weekly_summaries", "monthly_summaries", "manager_users", "manager_sessions"}
    assert [r["version_num"] for r in query(empty_schema, "SELECT version_num FROM alembic_version")] == [
        migrate.head_revision()
    ]


def test_cli_migrate_and_status(empty_schema, monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    s = empty_schema
    monkeypatch.setenv("ZAZA_DB_HOST", s.host)
    monkeypatch.setenv("ZAZA_DB_PORT", str(s.port))
    monkeypatch.setenv("ZAZA_DB_NAME", s.dbname)
    monkeypatch.setenv("ZAZA_DB_USER", s.user)
    monkeypatch.setenv("ZAZA_DB_PASSWORD", s.password)
    monkeypatch.setenv("ZAZA_DB_SCHEMA", "zaza_pytest_mig")
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    assert main(["db-status"]) == 0
    assert "(empty database)" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "Upgraded" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "already up to date" in out and s.password not in out


# ─── credentials hygiene ───────────────────────────────────────────────────


def test_database_credentials_are_never_logged(pg_settings, caplog):
    from deskmate.zaza_server.postgres import PostgresRepository, migrate

    caplog.set_level(logging.DEBUG)
    migrate.upgrade(pg_settings)
    repo = PostgresRepository(pg_settings)
    repo.list_devices()
    repo.close()
    wrong = dataclasses.replace(pg_settings, password="Wr0ng-" + pg_settings.password, pool_timeout=2)
    with pytest.raises(RepositoryUnavailable) as info:
        PostgresRepository(wrong)
    for secret in (pg_settings.password, wrong.password):
        assert secret not in caplog.text
        assert secret not in str(info.value)


# ─── the real agent, end to end, into PostgreSQL ───────────────────────────


def test_agent_sync_end_to_end_into_postgres(agent, clock, pg_repo, pg_settings, make_worker):
    from deskmate.zaza.sync.worker import SyncState

    server = SyncServer(pg_repo)
    session_id = work_session(agent, clock)
    worker = make_worker(agent, server.transport())
    result = worker.run_once()
    assert result.ok and result.failed == 0 and result.conflicts == 0
    assert worker.health().state == SyncState.HEALTHY
    for table in SYNCED_TABLES:
        assert query(pg_settings, f"SELECT count(*) AS n FROM {table}")[0]["n"] >= 1, table
    row = query(pg_settings, "SELECT status, employee_id FROM work_sessions WHERE session_id = %s",
                (session_id,))[0]
    assert (row["status"], row["employee_id"]) == ("CLOSED", "emp-1")
    for local_table in ("work_sessions", "activity_periods", "idle_periods", "app_usage_daily"):
        assert all(r["sync_status"] == "SYNCED" for r in agent.store._query(f"SELECT * FROM {local_table}"))
    again = worker.run_once()
    assert again.sent == 0
    assert query(pg_settings, "SELECT last_seen_at FROM devices")[0]["last_seen_at"] is not None


# ─── work schedules: shifts may cross midnight (Phase 4 review fix) ─────────
#
# A shift belongs to the local day it STARTS on; end_time < start_time means
# it ends on the following local day. start_time = end_time is not a 24-hour
# shift — it is rejected.

H = 3600


@pytest.mark.parametrize(
    "start, end, expected, ok",
    [
        ("09:00", "17:00", 8 * H, True),   # 1. same-day shift, full span
        ("09:00", "17:00", 6 * H, True),   # 2. same-day shift, shorter expected work
        ("20:00", "04:00", 8 * H, True),   # 3. overnight shift, full span
        ("20:00", "04:00", 6 * H, True),   # 4. overnight shift, shorter expected work
        ("20:00", "04:00", 9 * H, False),  # 5. expected exceeds the 8 h overnight span
        ("09:00", "17:00", 0, False),      # 6. a working day must expect some work
        ("09:00", "09:00", 8 * H, False),  # 7. start == end is not a 24-hour shift
        ("00:00", "00:00", 24 * H, False),  #    ... including at midnight
        ("00:00", "24:00", 24 * H, False),  #    and 24:00 can't sneak a 24-hour shift in
        ("23:00", "00:30", 90 * 60, True),  # just past midnight
        ("23:00", "00:30", 91 * 60, False),
        ("09:00", "17:00", -60, False),
    ],
)
def test_work_schedule_overnight_shifts(seeded, pg_settings, start, end, expected, ok):
    sql = ("INSERT INTO work_schedules (employee_id, day_of_week, effective_from, is_working_day, start_time, "
           "end_time, expected_work_seconds, timezone) VALUES ('emp-1', 5, '2026-01-01', true, %s, %s, %s, "
           "'Asia/Dhaka')")
    if ok:
        query(pg_settings, sql, (start, end, expected))
    else:
        execute_expect(pg_settings, errors.CheckViolation, sql, (start, end, expected))


@pytest.mark.parametrize("expected", [None, 0])
def test_work_schedule_day_off_with_null_times_is_valid(seeded, pg_settings, expected):  # 8.
    query(pg_settings, "INSERT INTO work_schedules (employee_id, schedule_date, is_working_day, start_time, "
                       "end_time, expected_work_seconds, timezone) VALUES ('emp-1', '2026-12-25', false, NULL, "
                       "NULL, %s, 'Asia/Dhaka')", (expected,))


def test_overnight_schedule_on_a_specific_date(seeded, pg_settings):
    query(pg_settings, "INSERT INTO work_schedules (employee_id, schedule_date, is_working_day, start_time, "
                       "end_time, expected_work_seconds, timezone) VALUES ('emp-1', '2026-12-31', true, '22:00', "
                       "'06:00', 28800, 'Asia/Dhaka')")
    row = query(pg_settings, "SELECT schedule_date, start_time, end_time FROM work_schedules")[0]
    assert (str(row["schedule_date"]), str(row["start_time"]), str(row["end_time"])) == (
        "2026-12-31", "22:00:00", "06:00:00",
    )
