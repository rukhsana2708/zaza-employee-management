"""Phase 7 on PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL, see conftest.py).

Migration 0004 constraints, the PostgreSQL dashboard repository behind the
real web routes, schedule management through the single Phase 5 write path
(database-validated, audited), and the manager CLI.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from .test_attendance_postgres import TZ, Synced, at, iso

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("jinja2")
pytest.importorskip("argon2")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import errors  # noqa: E402

from deskmate.zaza_server.attendance import SummaryService  # noqa: E402
from deskmate.zaza_server.attendance.postgres_store import PostgresAttendanceStore  # noqa: E402
from deskmate.zaza_server.auth import register_device  # noqa: E402
from deskmate.zaza_server.dashboard.auth import ManagerAuth  # noqa: E402
from deskmate.zaza_server.dashboard.config import DashboardSettings  # noqa: E402
from deskmate.zaza_server.dashboard.models import CurrentStatus  # noqa: E402
from deskmate.zaza_server.dashboard.queries import (  # noqa: E402
    DuplicateUsername,
    PostgresDashboardRepository,
)
from deskmate.zaza_server.dashboard.routes import mount_dashboard  # noqa: E402
from deskmate.zaza_server.dashboard.security import COOKIE_NAME, token_hash  # noqa: E402
from deskmate.zaza_server.dashboard.service import DashboardService  # noqa: E402

UTC = timezone.utc
H = 3600
MON = date(2026, 10, 5)
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)  # Thursday 12:00 Dhaka
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def office(pg_repo):
    pg_repo.add_employee("emp-1", "Employee One", timezone=TZ)
    register_device(pg_repo, "device-1", "emp-1")
    with pg_repo.connection() as conn:
        conn.execute("UPDATE devices SET last_seen_at = %s", (NOW - timedelta(seconds=30),))
    store = PostgresAttendanceStore(pg_repo)
    for dow in range(1, 6):
        store.add_schedule("emp-1", is_working_day=True, day_of_week=dow, effective_from=date(2026, 1, 1),
                           start_time=time(9), end_time=time(17), expected_work_seconds=8 * H)
    sync = Synced(pg_repo)
    sid = sync.session(at(MON, "09:00"), at(MON, "17:00"))
    sync.period(sid, "ACTIVE", at(MON, "09:30"), at(MON, "17:00"))
    usage = {"usage_date": "2026-10-05", "day_start_utc": iso(at(MON, "00:00")), "day_end_utc": iso(at(MON + timedelta(days=1), "00:00")),
             "app_name": "Code.exe", "active_seconds": 7.5 * H, "idle_seconds": 0.0, "unknown_seconds": 0.0,
             "period_count": 1}
    sync._put("app_usage_daily", str(uuid.uuid4()), 1, usage, at(MON, "17:00"))
    SummaryService(store, clock=lambda: NOW).recalculate(date(2026, 10, 1), date(2026, 10, 8))
    return pg_repo


def client_for(repo, clock=lambda: NOW) -> tuple[TestClient, PostgresDashboardRepository]:  # noqa: ANN001
    dash = PostgresDashboardRepository(repo)
    app = FastAPI()
    mount_dashboard(app, dash, DashboardSettings(), clock)
    return TestClient(app, follow_redirects=False), dash


def login(client: TestClient, username: str = "manager", password: str = PASSWORD):  # noqa: ANN201
    token = re.search(r'name="login_token" value="([^"]+)"', client.get("/manager/login").text).group(1)
    return client.post("/manager/login", data={"username": username, "password": password, "login_token": token})


def csrf(client: TestClient) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/manager/schedules").text).group(1)


def rows(repo, sql: str, params=()) -> list[dict]:  # noqa: ANN001
    with repo.connection() as conn:
        return conn.execute(sql, params).fetchall()


def test_migration_0004_constraints(pg_repo, pg_settings):
    def run(sql: str, params=()):  # noqa: ANN001, ANN202
        with psycopg.connect(**pg_settings.connect_kwargs(), autocommit=True) as conn:
            conn.execute(sql, params)

    good = "$argon2id$v=19$m=65536,t=3,p=4$" + "a" * 22 + "$" + "b" * 43
    run("INSERT INTO manager_users (username, display_name, password_hash) VALUES ('manager', 'M', %s)", (good,))
    with pytest.raises(errors.CheckViolation, match="manager_users_password_hash_argon2id"):
        run("INSERT INTO manager_users (username, display_name, password_hash) VALUES ('other', 'O', %s)",
            ("plaintext-password-123",))
    with pytest.raises(errors.CheckViolation, match="manager_users_username_format"):
        run("INSERT INTO manager_users (username, display_name, password_hash) VALUES ('Other', 'O', %s)", (good,))
    with pytest.raises(errors.CheckViolation, match="manager_users_role_valid"):
        run("INSERT INTO manager_users (username, display_name, password_hash, role) VALUES ('x-1', 'O', %s, 'ROOT')",
            (good,))
    with pytest.raises(errors.UniqueViolation):
        run("INSERT INTO manager_users (username, display_name, password_hash) VALUES ('manager', 'M2', %s)", (good,))
    uid = rows(pg_repo, "SELECT manager_user_id FROM manager_users")[0]["manager_user_id"]
    with pytest.raises(errors.CheckViolation, match="manager_sessions_token_hash_format"):
        run("INSERT INTO manager_sessions (manager_user_id, session_token_hash, csrf_token_hash, expires_at) "
            "VALUES (%s, 'raw-token', %s, now() + interval '1 hour')", (uid, "c" * 64))
    run("INSERT INTO audit_logs (actor_type, actor_id, action, entity_type, entity_id) "
        "VALUES ('CLI', 'x', 'manager.create', 'manager_user', %s)", (str(uid),))


def test_accounts_and_sessions_on_postgres(office):
    client, dash = client_for(office)
    auth = ManagerAuth(dash, clock=lambda: NOW)
    user = auth.create_user("Manager", "Project Manager", PASSWORD)
    with pytest.raises(DuplicateUsername):
        auth.create_user("MANAGER", "Again", PASSWORD)
    stored = rows(office, "SELECT username, password_hash FROM manager_users")[0]
    assert stored["username"] == "manager" and stored["password_hash"].startswith("$argon2id$")
    assert login(client, "MANAGER").status_code == 303
    token = client.cookies.get(COOKIE_NAME)
    session = rows(office, "SELECT session_token_hash, expires_at FROM manager_sessions")[0]
    assert session["session_token_hash"] == token_hash(token) and session["expires_at"] == NOW + timedelta(hours=12)
    assert rows(office, "SELECT last_login_at FROM manager_users")[0]["last_login_at"] == NOW
    dump = str(rows(office, "SELECT * FROM manager_sessions")) + str(rows(office, "SELECT * FROM audit_logs"))
    assert token not in dump and PASSWORD not in dump
    assert client.get("/manager").status_code == 200
    auth.set_active("manager", False)
    assert client.get("/manager").status_code == 303  # disabled: refused immediately
    audit = [(r["action"], r["actor_type"]) for r in rows(
        office, "SELECT action, actor_type FROM audit_logs WHERE entity_type = 'manager_user' ORDER BY audit_id")]
    assert audit == [("manager.create", "CLI"), ("manager.login", "MANAGER"), ("manager.disable", "CLI")]
    assert user.manager_user_id


def test_pages_read_postgres(office):
    client, dash = client_for(office)
    ManagerAuth(dash, clock=lambda: NOW).create_user("manager", "Project Manager", PASSWORD)
    login(client)
    week = {"period": "this_week"}
    for path in ("/manager", "/manager/employees", "/manager/employees/emp-1", "/manager/attendance",
                 "/manager/applications", "/manager/schedules"):
        response = client.get(path, params=week)
        assert response.status_code == 200, (path, response.text[:400])
    overview = client.get("/manager/api/overview", params=week).json()
    stored = rows(office, "SELECT sum(active_seconds) AS a, sum(scheduled_seconds) AS s, "
                          "sum(attendance_credit_seconds) AS c, sum(attendance_basis_seconds) AS b "
                          "FROM daily_summaries WHERE local_date BETWEEN '2026-10-05' AND '2026-10-11'")[0]
    assert overview["totals"]["active_seconds"] == stored["a"] == 7.5 * H
    assert overview["totals"]["scheduled_seconds"] == stored["s"]
    assert overview["totals"]["attendance_percentage"] == round(stored["c"] / stored["b"] * 100, 2)
    assert overview["totals"]["late_employees"] == ["Employee One"]
    apps = client.get("/manager/api/applications", params=week).json()["rows"]
    assert apps == [{"application": "Code.exe", "employee_id": None, "active_seconds": 7.5 * H,
                     "idle_seconds": 0.0, "unknown_seconds": 0.0, "usage_days": 1}]
    detail = client.get("/manager/employees/emp-1", params=week).text
    assert "Code.exe" in detail and "Recent activity periods" in detail and "payload" not in detail


def test_current_status_from_postgres(office):
    service = DashboardService(PostgresDashboardRepository(office), clock=lambda: NOW)
    assert service.current_status()["emp-1"].status is CurrentStatus.ONLINE_NO_ACTIVITY
    sync = Synced(office)
    sid = sync.session(NOW - timedelta(minutes=10), None, status="OPEN")
    sync.period(sid, "IDLE", NOW - timedelta(minutes=10), NOW - timedelta(minutes=6))
    sync.period(sid, "ACTIVE", NOW - timedelta(minutes=6), NOW - timedelta(seconds=20))
    assert service.current_status()["emp-1"].status is CurrentStatus.ACTIVE
    with office.connection() as conn:
        conn.execute("UPDATE devices SET last_seen_at = %s", (NOW - timedelta(minutes=30),))
    assert service.current_status()["emp-1"].status is CurrentStatus.OFFLINE
    with office.connection() as conn:
        conn.execute("UPDATE devices SET status = 'DISABLED', disabled_at = now()")
    assert service.current_status()["emp-1"].status is CurrentStatus.NO_DEVICE


def test_schedule_management_is_validated_by_the_database_and_audited(office):
    client, dash = client_for(office)
    ManagerAuth(dash, clock=lambda: NOW).create_user("manager", "Project Manager", PASSWORD)
    login(client)
    token = csrf(client)
    # invalid: start == end -> the database constraint rejects it, nothing stored
    response = client.post("/manager/schedules/date", data={
        "csrf_token": token, "employee_id": "emp-1", "schedule_date": "2026-10-20", "start": "09:00",
        "end": "09:00", "expected_hours": "1"})
    assert response.status_code == 400 and "work_schedules_hours_valid" in response.text
    assert "a working day needs a start and an end that differ" in response.text
    assert not rows(office, "SELECT 1 FROM work_schedules WHERE schedule_date IS NOT NULL")
    # valid overnight date override
    response = client.post("/manager/schedules/date", data={
        "csrf_token": token, "employee_id": "emp-1", "schedule_date": "2026-10-20", "start": "20:00",
        "end": "04:00", "expected_hours": "8"})
    assert response.status_code == 303
    (rule,) = rows(office, "SELECT schedule_id::text AS id, start_time, end_time, timezone FROM work_schedules "
                           "WHERE schedule_date = '2026-10-20'")
    assert (rule["start_time"], rule["end_time"], rule["timezone"]) == (time(20), time(4), TZ)
    # a running weekly rule (Monday, since 2026-01-01) changed from next Monday: split, history kept
    monday = rows(office, "SELECT schedule_id::text AS id FROM work_schedules WHERE day_of_week = 1")[0]["id"]
    response = client.post(f"/manager/schedules/{monday}/update", data={
        "csrf_token": token, "start": "10:00", "end": "18:00", "expected_hours": "8", "from_date": "2026-10-12"})
    assert response.status_code == 303
    mondays = rows(office, "SELECT effective_from, effective_to, start_time FROM work_schedules WHERE day_of_week = 1 "
                           "ORDER BY effective_from")
    assert [(r["effective_from"], r["effective_to"], r["start_time"]) for r in mondays] == [
        (date(2026, 1, 1), date(2026, 10, 11), time(9)), (date(2026, 10, 12), None, time(10))]
    # history: a change starting in the past is refused
    response = client.post(f"/manager/schedules/{monday}/update", data={
        "csrf_token": token, "start": "10:00", "end": "18:00", "expected_hours": "8", "from_date": "2026-10-01"})
    assert response.status_code == 400
    # remove the future override (deleted) — audited with old values
    response = client.post(f"/manager/schedules/{rule['id']}/remove", data={"csrf_token": token})
    assert response.status_code == 303
    assert not rows(office, "SELECT 1 FROM work_schedules WHERE schedule_date = '2026-10-20'")
    audit = rows(office, "SELECT actor_type, actor_id, action, entity_id, old_values, new_values FROM audit_logs "
                         "WHERE entity_type = 'work_schedule' AND actor_type = 'MANAGER' ORDER BY audit_id")
    assert [(a["action"], a["actor_id"]) for a in audit] == [
        ("work_schedule.create", "manager"), ("work_schedule.update", "manager"),
        ("work_schedule.create", "manager"), ("work_schedule.delete", "manager")]
    assert audit[1]["old_values"]["effective_to"] is None and audit[1]["new_values"]["effective_to"] == "2026-10-11"
    assert audit[-1]["entity_id"] == rule["id"] and audit[-1]["old_values"]["start_time"] == "20:00:00"
    assert all(token not in str(a) for a in audit)
    # GET never mutates
    assert client.get(f"/manager/schedules/{monday}/remove").status_code == 405


def test_manager_cli(office, pg_settings, monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    s = pg_settings
    for k, v in {"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_HOST": s.host, "ZAZA_DB_PORT": str(s.port),
                 "ZAZA_DB_NAME": s.dbname, "ZAZA_DB_USER": s.user, "ZAZA_DB_PASSWORD": s.password,
                 "ZAZA_DB_SCHEMA": s.schema}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["manager-create", "--username", "Boss", "--name", "Project Manager", "--role", "ADMIN"]) == 0
    out = capsys.readouterr().out
    assert "Created admin account boss" in out and PASSWORD not in out
    answers = iter(["short", "short"])
    assert main(["manager-reset-password", "--username", "boss"]) == 1
    assert "at least 12" in capsys.readouterr().err
    answers = iter(["a-new-long-passphrase", "different-passphrase"])
    assert main(["manager-reset-password", "--username", "boss"]) == 1
    assert "don't match" in capsys.readouterr().err
    client, _ = client_for(office, clock=lambda: datetime.now(UTC))
    assert login(client, "boss").status_code == 303
    assert main(["manager-list"]) == 0
    assert "boss" in capsys.readouterr().out
    assert main(["manager-revoke-sessions", "--username", "boss"]) == 0
    assert "1 session(s)" in capsys.readouterr().out
    assert client.get("/manager").status_code == 303
    assert login(client, "boss").status_code == 303
    assert main(["manager-disable", "--username", "boss"]) == 0
    assert client.get("/manager").status_code == 303
    assert login(client, "boss").status_code == 401
    assert main(["manager-enable", "--username", "boss"]) == 0
    answers = iter(["a-new-long-passphrase", "a-new-long-passphrase"])
    assert main(["manager-reset-password", "--username", "boss"]) == 0
    assert login(client, "boss").status_code == 401
    assert login(client, "boss", "a-new-long-passphrase").status_code == 303
    assert s.password not in capsys.readouterr().out


def test_real_serve_command_mounts_dashboard_and_sync_api(office, pg_settings, monkeypatch, capsys):
    """``python -m deskmate.zaza_server serve`` on PostgreSQL + loopback: the
    one app has the manager dashboard AND the sync API. uvicorn.run is
    replaced, so nothing listens; requests run while the repository is open."""
    from deskmate.zaza_server.__main__ import main

    s = pg_settings
    for k, v in {"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_HOST": s.host, "ZAZA_DB_PORT": str(s.port),
                 "ZAZA_DB_NAME": s.dbname, "ZAZA_DB_USER": s.user, "ZAZA_DB_PASSWORD": s.password,
                 "ZAZA_DB_SCHEMA": s.schema}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    ManagerAuth(PostgresDashboardRepository(office)).create_user("manager", "Project Manager", PASSWORD)
    seen = {}

    def fake_run(app, **kwargs):  # noqa: ANN001, ANN003
        client = TestClient(app, follow_redirects=False)
        seen["login_page"] = client.get("/manager/login").status_code
        if seen["login_page"] == 200:
            seen["login"] = login(client).status_code
            seen["overview"] = client.get("/manager").status_code
        seen["health"] = client.get("/api/v1/sync/health").status_code
        seen["batch_unauthenticated"] = client.post("/api/v1/sync/batch", json={}).status_code
        seen["host"] = kwargs["host"]

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert main(["serve", "--host", "127.0.0.1", "--port", "8765"]) == 0
    assert seen == {"login_page": 200, "login": 303, "overview": 200, "health": 200,
                    "batch_unauthenticated": 401, "host": "127.0.0.1"}
    assert s.password not in capsys.readouterr().out
    seen.clear()
    assert main(["serve", "--host", "0.0.0.0", "--port", "8765"]) == 0
    assert seen["login_page"] == 404 and seen["health"] == 200
    assert "Manager dashboard not mounted" in capsys.readouterr().out
