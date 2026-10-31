"""Phase B browser security and SQL-scoped reporting on real isolated tenant databases."""

import re
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from deskmate.zaza.sync.protocol import SYNC_RECORD_ADAPTER
from deskmate.zaza_server.app import create_saas_app
from deskmate.zaza_server.attendance import SummaryService
from deskmate.zaza_server.attendance.postgres_store import PostgresAttendanceStore
from deskmate.zaza_server.auth import register_device
from deskmate.zaza_server.repository import IncomingRecord
from deskmate.zaza_server.service import content_hash
from deskmate.zaza_server.workforce.service import WorkforceService
from deskmate.zaza_server.workforce_dashboard.queries import ScopedDashboardRepository

from .test_attendance_postgres import Synced, iso
from .test_saas_postgres import saas  # noqa: F401
from .test_workforce_foundation import workforce  # noqa: F401

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
START = datetime(2026, 10, 31, 14, tzinfo=UTC)
PW = "local browser test passphrase 81"
BASE = "/app/orgs/tenant-a"
PAGES = ["workspace", "workforce", "attendance", "applications", "analytics", "workforce/report"]


class EmployeeSynced(Synced):
    def __init__(self, repo, employee, device):
        super().__init__(repo)
        self.employee, self.device = employee, device

    def _put(self, record_type, record_id, version, data, created):
        self.seq += 1
        raw = {
            "record_type": record_type,
            "record_id": record_id,
            "record_version": version,
            "device_id": self.device,
            "employee_id": self.employee,
            "local_seq": self.seq,
            "created_at": iso(created),
            "updated_at": iso(created),
            "data": data,
        }
        model = SYNC_RECORD_ADAPTER.validate_python(raw)
        result = self.repo.upsert_records(
            [
                IncomingRecord(
                    record_type,
                    record_id,
                    version,
                    self.device,
                    self.employee,
                    self.seq,
                    content_hash(model),
                    model.model_dump_json(),
                    str(uuid.uuid4()),
                )
            ]
        )[0]
        assert result.status in {"accepted", "updated"}
        return record_id


@pytest.fixture
def browser(workforce):  # noqa: F811
    w = workforce
    w.saas.runtime.browser_clock = lambda: NOW
    w.service.add_reporting(w.ctx["a"]["owner"], "report", "boss", START)
    w.service.add_reporting(w.ctx["a"]["owner"], "nested", "report", START)
    for name in ["a", "b"]:
        with w.saas.runtime.manager.repository(w.ctx[name]["owner"]) as repo:
            with repo.connection() as conn:
                conn.execute(
                    "UPDATE employees SET display_name=CASE employee_id WHEN 'report' THEN 'Shared Report' "
                    "WHEN 'boss' THEN 'Shared Manager' WHEN 'unrelated' THEN 'PRIVATE UNRELATED' "
                    "WHEN 'nested' THEN 'PRIVATE NESTED' ELSE display_name END"
                )
            register_device(repo, "report-device", "report")
            sync = EmployeeSynced(repo, "report", "report-device")
            store = PostgresAttendanceStore(repo)
            for dow in range(1, 8):
                store.add_schedule(
                    "report",
                    is_working_day=True,
                    day_of_week=dow,
                    effective_from=date(2026, 1, 1),
                    start_time=time(9),
                    end_time=time(18),
                    expected_work_seconds=8 * 3600,
                )
            for day in [date(2026, 10, 31), date(2026, 11, 1)]:
                begin = datetime.combine(day, time(9), tzinfo=UTC)
                sid = sync.session(begin, begin + timedelta(hours=9))
                for hour, end, label in [
                    (9, 10, "PRIVATE BEFORE"),
                    (13, 15, "CROSS BOUNDARY"),
                    (16, 17, "VISIBLE AFTER"),
                ]:
                    rid = sync.period(
                        sid,
                        "ACTIVE",
                        datetime.combine(day, time(hour), tzinfo=UTC),
                        datetime.combine(day, time(end), tzinfo=UTC),
                    )
                    with repo.connection() as conn:
                        conn.execute(
                            "UPDATE activity_periods SET app_name=%s,window_title=%s WHERE period_id=%s",
                            (label, label + " " + name, rid),
                        )
                sync._put(
                    "app_usage_daily",
                    str(uuid.uuid4()),
                    1,
                    {
                        "usage_date": day.isoformat(),
                        "day_start_utc": iso(datetime.combine(day, time(0), tzinfo=UTC)),
                        "day_end_utc": iso(
                            datetime.combine(day + timedelta(days=1), time(0), tzinfo=UTC)
                        ),
                        "app_name": "Boundary Rollup" if day.day == 31 else "Eligible Rollup",
                        "active_seconds": 3600,
                        "idle_seconds": 0,
                        "unknown_seconds": 0,
                        "period_count": 1,
                    },
                    begin,
                )
            with repo.connection() as conn:
                conn.execute("UPDATE devices SET last_seen_at=%s", (NOW - timedelta(seconds=30),))
            SummaryService(store, clock=lambda: NOW).recalculate(
                date(2026, 10, 31), date(2026, 11, 2)
            )
    app = create_saas_app(w.saas.runtime)
    client = TestClient(app, follow_redirects=False)
    yield SimpleNamespace(w=w, app=app, client=client)
    client.close()


def sign_in(b, role="manager", *, client=None):
    client = client or b.client
    user = getattr(b.w, role)
    b.w.saas.runtime.auth.set_password(user.user_id, PW)
    token = re.search(r'name="login_token" value="([^"]+)"', client.get("/app/sign-in").text).group(
        1
    )
    response = client.post(
        "/app/sign-in",
        data={"email": user.email, "password": PW, "login_token": token},
        headers={"Origin": str(client.base_url).rstrip("/")},
    )
    assert response.status_code == 303, response.text
    return response


@pytest.mark.parametrize("page", PAGES)
def test_browser_pages_and_legacy_json_contract(browser, page):
    b = browser
    assert b.client.get(BASE + "/" + page).status_code == 303
    sign_in(b)
    response = b.client.get(BASE + "/" + page + "?period=custom&from=2026-10-31&to=2026-11-02")
    assert response.status_code == 200, response.text
    assert "<!doctype html>" in response.text
    assert "PRIVATE UNRELATED" not in response.text and "PRIVATE NESTED" not in response.text
    assert "/manager/" not in response.text and "zaza_t_" not in response.text
    for header in [
        "content-security-policy",
        "x-content-type-options",
        "x-frame-options",
        "referrer-policy",
    ]:
        assert header in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert b.client.get("/app/session").status_code == 200
    assert b.client.get(BASE).headers["content-type"].startswith("application/json")
    assert b.client.get(BASE + "/employees").status_code == 403


@pytest.mark.parametrize("role", ["employee", "manager", "owner"])
def test_role_subjects_and_same_ids_in_other_tenant(browser, role):
    b = browser
    sign_in(b, role)
    page = b.client.get(BASE + "/workforce")
    assert page.status_code == 200
    assert "Shared Report" in page.text
    if role == "employee":
        assert "Shared Manager" not in page.text
    if role == "manager":
        assert "Shared Manager" in page.text
    if role == "owner":
        assert "PRIVATE UNRELATED" in page.text
    if role != "owner":
        for identity in ["unrelated", "nested", "employee-1"]:
            assert b.client.get(BASE + "/workforce/" + identity).status_code == 404
    b.w.saas.control.set_membership_status(
        b.w.ctx["b"][role].organization_id, b.w.ctx["b"][role].user_id, "DISABLED"
    )
    assert b.client.get("/app/orgs/tenant-b/workforce/report").status_code == 404
    organizations = b.client.get("/app/organizations")
    assert "tenant-b/workspace" not in organizations.text
    assert str(b.w.ctx["a"][role].organization_id) not in organizations.text


def test_viewer_has_only_landing_context(browser):
    sign_in(browser, "viewer")
    assert browser.client.get(BASE + "/workspace").status_code == 200
    for page in PAGES[1:]:
        response = browser.client.get(BASE + "/" + page)
        assert response.status_code in {403, 404}
        assert "Shared Report" not in response.text


def test_midday_start_omits_aggregates_and_clips_intervals(browser):
    b = browser
    sign_in(b)
    detail = b.client.get(BASE + "/workforce/report?period=custom&from=2026-10-31&to=2026-11-02")
    assert detail.status_code == 200, detail.text
    assert "Boundary Rollup" not in detail.text
    assert "Eligible Rollup" in detail.text
    # Pre-authorization period is absent; overlapping period begins at 14:00, never 13:00.
    assert "2026-10-31 13:00" not in detail.text
    assert "2026-10-31 14:00" in detail.text
    service = WorkforceService(
        b.w.saas.control, b.w.saas.runtime.resolver, b.w.saas.runtime.manager, clock=lambda: NOW
    )
    with service.dashboard(b.w.ctx["a"]["manager"], "report") as (conn, scope):
        repo = ScopedDashboardRepository(conn, scope, now=NOW)
        assert (
            conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        )
        days = repo.daily(["report"], date(2026, 10, 31), date(2026, 11, 2))
        assert date(2026, 10, 31) not in {d.local_date for d in days}
        assert all(d.window_start >= START and d.window_end <= NOW for d in days)
        periods = repo.activity("report", START - timedelta(days=30), NOW, 200)
        assert periods and min(p.started_at for p in periods) >= START
        assert all(p.ended_at <= NOW for p in periods)
        assert all(
            p.duration_seconds == (p.ended_at - p.started_at).total_seconds() for p in periods
        )
    sign_in(b, "owner")
    owner = b.client.get(BASE + "/workforce/report?period=custom&from=2026-10-31&to=2026-11-02")
    assert "Boundary Rollup" in owner.text and "2026-10-31 13:00" in owner.text


def test_every_dashboard_query_is_sql_scoped(browser, monkeypatch):
    seen = []
    original = ScopedDashboardRepository._rows

    def capture(self, query, params=()):
        assert {s.employee_id for s in self.scope.subjects} <= {"boss", "report"}
        assert "JOIN authorized" in query
        seen.append(query)
        return original(self, query, params)

    monkeypatch.setattr(ScopedDashboardRepository, "_rows", capture)
    sign_in(browser)
    assert browser.client.get(BASE + "/analytics?period=this_month").status_code == 200
    assert seen and any("daily_summaries" in q for q in seen)
    assert any("application_usage_daily" in q for q in seen)
    assert all("weekly_summaries" not in q and "monthly_summaries" not in q for q in seen)


@pytest.mark.parametrize(
    "change", ["user", "membership", "organization", "tenant", "link", "reporting", "leave"]
)
def test_authorization_revocation_after_login(browser, change):
    b = browser
    sign_in(b)
    c = b.w.saas.control
    member = b.w.members["a"]["manager"]
    if change == "user":
        c.set_user_status(member.user_id, "DISABLED")
    if change == "membership":
        c.set_membership_status(member.organization_id, member.user_id, "DISABLED")
    if change == "organization":
        c.set_organization_status(member.organization_id, "SUSPENDED")
    if change == "tenant":
        c.mark_tenant_database(member.organization_id, "ERROR")
    if change == "link":
        b.w.service.revoke_link(b.w.ctx["a"]["owner"], "boss")
    if change == "leave":
        b.w.service.update_profile(
            b.w.ctx["a"]["owner"], "boss", version=1, employment_status="LEAVE"
        )
    if change == "reporting":
        with b.w.saas.runtime.manager.repository(b.w.ctx["a"]["owner"]) as repo:
            with repo.connection() as conn:
                identity = conn.execute(
                    "SELECT id FROM employee_reporting_relationships WHERE employee_id='report'"
                ).fetchone()["id"]
        b.w.service.end_reporting(b.w.ctx["a"]["owner"], identity, NOW - timedelta(seconds=1))
    response = b.client.get(BASE + "/workforce/report")
    assert response.status_code in {303, 403, 404}
    assert "Shared Report" not in response.text
    if change in {"reporting", "leave"}:
        assert b.client.get(BASE + "/workforce/boss").status_code == 200


def test_xss_and_no_business_write_routes(browser):
    b = browser
    marker = '<script>alert("workforce")</script>'
    with b.w.saas.runtime.manager.repository(b.w.ctx["a"]["owner"]) as repo:
        with repo.connection() as conn:
            conn.execute(
                "UPDATE employees SET display_name=%s WHERE employee_id='report'", (marker,)
            )
    sign_in(b)
    text = b.client.get(BASE + "/workforce").text
    assert marker not in text and "&lt;script&gt;" in text
    for path in ["/workforce/report", "/attendance", "/workforce/report/update", "/schedules"]:
        assert b.client.post(BASE + path, data={"display_name": "unauthorized"}).status_code in {
            404,
            405,
        }
    assert b.client.get("/manager").status_code == 404
    assert b.client.get("/app/assets/workforce.css").status_code == 200
    assert b.client.get("/app/report-assets/dashboard.css").status_code == 200


def test_browser_csrf_logout_expiry_and_cookie_flags(browser):
    b = browser
    response = sign_in(b)
    cookie = response.headers.get("set-cookie")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/app" in cookie
    assert b.client.post("/app/sign-out", data={}).status_code == 403
    csrf = b.client.get("/app/session").json()["csrf_token"]
    assert (
        b.client.post(
            "/app/sign-out", data={"csrf_token": csrf}, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        b.client.post(
            "/app/sign-out", data={"csrf_token": csrf}, headers={"Origin": "http://testserver"}
        ).status_code
        == 303
    )
    assert b.client.get(BASE + "/workspace").status_code == 303
    sign_in(b)
    token = b.client.cookies.get("zaza_session")
    auth = b.w.saas.runtime.auth
    auth.logout(auth.validate_session(token))
    assert b.client.get("/app/organizations").status_code == 303
    auth.settings = replace(auth.settings, cookie_secure=True)
    with TestClient(b.app, base_url="https://testserver", follow_redirects=False) as secure:
        assert "Secure" in sign_in(b, client=secure).headers["set-cookie"]


def test_shared_browser_json_login_limiter_and_origin(browser):
    b = browser
    token = re.search(
        r'name="login_token" value="([^"]+)"', b.client.get("/app/sign-in").text
    ).group(1)
    data = {"email": "unknown@example.test", "password": "wrong", "login_token": token}
    assert (
        b.client.post(
            "/app/sign-in", data=data, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    statuses = []
    for i in range(6):
        if i % 2:
            challenge = b.client.get("/app/login").json()["login_token"]
            response = b.client.post(
                "/app/login",
                data={**data, "login_token": challenge},
                headers={"Origin": "http://testserver"},
            )
        else:
            challenge = re.search(
                r'name="login_token" value="([^"]+)"', b.client.get("/app/sign-in").text
            ).group(1)
            response = b.client.post(
                "/app/sign-in",
                data={**data, "login_token": challenge},
                headers={"Origin": "http://testserver"},
            )
        statuses.append(response.status_code)
        assert "unknown@example.test" not in response.text
    assert statuses[:5] == [401] * 5 and statuses[5] == 429
    assert "retry-after" in response.headers


def test_expired_session_and_generic_browser_failure(browser):
    b = browser
    sign_in(b)
    c = b.w.saas.control
    auth = b.w.saas.runtime.auth
    original_clock = auth.clock
    auth.clock = lambda: original_clock() + timedelta(days=2)
    assert b.client.get("/app/organizations").status_code == 303
    auth.clock = original_clock
    for disabled in [False, True]:
        if disabled:
            c.set_user_status(b.w.manager.user_id, "DISABLED")
        challenge = re.search(
            r'name="login_token" value="([^"]+)"', b.client.get("/app/sign-in").text
        ).group(1)
        response = b.client.post(
            "/app/sign-in",
            data={"email": b.w.manager.email, "password": "incorrect", "login_token": challenge},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 401 and "invalid email or password" in response.text
        assert b.w.manager.email not in response.text
    challenge = re.search(
        r'name="login_token" value="([^"]+)"', b.client.get("/app/sign-in").text
    ).group(1)
    assert (
        b.client.post(
            "/app/sign-in", data={"login_token": "invalid"}, headers={"Origin": "http://testserver"}
        ).status_code
        == 403
    )


def test_non_employee_admin_and_monitored_owner(browser):
    b = browser
    c = b.w.saas.control
    b.w.admin = c.create_user("admin@browser.test", "Non-employee administrator")
    c.add_membership(b.w.ctx["a"]["owner"].organization_id, b.w.admin.user_id, "ADMIN")
    sign_in(b, "admin")
    assert "PRIVATE UNRELATED" in b.client.get(BASE + "/workforce").text
    owner = b.w.members["a"]["owner"]
    b.w.service.revoke_link(b.w.ctx["a"]["owner"], "unrelated")
    b.w.service.link_account(b.w.ctx["a"]["owner"], "unrelated", owner.user_id, owner.membership_id)
    sign_in(b, "owner")
    detail = b.client.get(BASE + "/workforce/unrelated")
    assert detail.status_code == 200 and "Monitored" in detail.text


def test_read_only_and_lock_cleanup_on_error(browser):
    import psycopg

    b = browser
    service = WorkforceService(
        b.w.saas.control, b.w.saas.runtime.resolver, b.w.saas.runtime.manager, clock=lambda: NOW
    )
    with pytest.raises(RuntimeError, match="leave read transaction"):
        with service.dashboard(b.w.ctx["a"]["manager"]) as (conn, scope):
            assert (
                conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
                == "on"
            )
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute(
                        "UPDATE employees SET display_name='forbidden' WHERE employee_id='boss'"
                    )
            raise RuntimeError("leave read transaction")
    with b.w.saas.runtime.manager.repository(b.w.ctx["a"]["owner"]) as repo:
        with repo.connection() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() "
                    "AND locktype='advisory'"
                ).fetchone()["n"]
                == 0
            )
    sign_in(b)
    assert (
        b.client.get(BASE + "/attendance?period=custom&from=invalid&to=2026-11-01").status_code
        == 400
    )
    b.w.service.update_profile(b.w.ctx["a"]["owner"], "report", version=1, monitoring_enabled=False)


def test_dst_app_bucket_requires_its_full_actual_interval(browser):
    from deskmate.zaza_server.workforce.service import AuthorizedWorkforceScope, SubjectScope

    b = browser
    with b.w.saas.runtime.manager.repository(b.w.ctx["a"]["owner"]) as repo:
        repo.add_employee("dst", "DST Employee", timezone="America/New_York")
        register_device(repo, "dst-device", "dst")
        sync = EmployeeSynced(repo, "dst", "dst-device")
        lo = datetime(2026, 11, 1, 4, tzinfo=UTC)
        hi = datetime(2026, 11, 2, 5, tzinfo=UTC)  # 25-hour employee-local day
        sync._put(
            "app_usage_daily",
            str(uuid.uuid4()),
            1,
            {
                "usage_date": "2026-11-01",
                "day_start_utc": iso(lo),
                "day_end_utc": iso(hi),
                "app_name": "DST App",
                "active_seconds": 3600,
                "idle_seconds": 0,
                "unknown_seconds": 0,
                "period_count": 1,
            },
            lo,
        )
    service = WorkforceService(
        b.w.saas.control, b.w.saas.runtime.resolver, b.w.saas.runtime.manager, clock=lambda: NOW
    )
    with service.dashboard(b.w.ctx["a"]["owner"]) as (conn, _):
        for end, count in [(lo + timedelta(hours=24), 0), (hi, 1)]:
            scoped = AuthorizedWorkforceScope((SubjectScope("dst", "direct_report", lo, end),))
            rows = ScopedDashboardRepository(conn, scoped).app_usage(
                ["dst"], date(2026, 11, 1), date(2026, 11, 1)
            )
            assert len(rows) == count


def test_locally_rendered_page_inventory(browser):
    from pathlib import Path

    b = browser
    sign_in(b, "owner")
    folder = Path(".pytest_cache/phase-b-pages")
    folder.mkdir(exist_ok=True)
    for page in PAGES:
        response = b.client.get(BASE + "/" + page + "?period=custom&from=2026-10-31&to=2026-11-02")
        assert response.status_code == 200
        (folder / (page.replace("/", "-") + ".html")).write_text(response.text, encoding="utf-8")
    assert "Show the numbers" in (folder / "analytics.html").read_text(encoding="utf-8")
    assert 'http-equiv="refresh"' not in response.text


def test_same_identity_reports_and_links_remain_tenant_local(browser):
    b = browser
    sign_in(b, "owner")
    query = "?period=custom&from=2026-10-31&to=2026-11-02"
    a = b.client.get(BASE + "/workforce/report" + query)
    other = b.client.get("/app/orgs/tenant-b/workforce/report" + query)
    assert "VISIBLE AFTER a" in a.text and "VISIBLE AFTER b" not in a.text
    assert "VISIBLE AFTER b" in other.text and "VISIBLE AFTER a" not in other.text
    b.w.service.revoke_link(b.w.ctx["b"]["owner"], "boss")
    sign_in(b, "manager")
    assert b.client.get(BASE + "/workforce").status_code == 200
    assert b.client.get("/app/orgs/tenant-b/workforce").status_code == 403


def test_current_status_variants_do_not_imply_work(browser):
    b = browser
    sign_in(b)
    with b.w.saas.runtime.manager.repository(b.w.ctx["a"]["owner"]) as repo:
        sync = EmployeeSynced(repo, "report", "report-device")
        # Use a sequence above the fixture's existing counter; sequences are device-local.
        sync.seq = 100
        sid = sync.session(NOW - timedelta(minutes=5), NOW)
        rid = str(uuid.uuid4())
        for version, state in enumerate(["ACTIVE", "IDLE", "LOCKED", "UNKNOWN"], 1):
            sync.period(
                sid,
                state,
                NOW - timedelta(seconds=60),
                NOW - timedelta(seconds=2),
                rid=rid,
                version=version,
            )
            response = b.client.get(BASE + "/workforce")
            label = "Unknown (monitoring uncertain)" if state == "UNKNOWN" else state.title()
            assert "<td>" + label + "</td>" in response.text
        with repo.connection() as conn:
            conn.execute(
                "UPDATE devices SET last_seen_at=%s WHERE device_id='report-device'",
                (NOW - timedelta(hours=1),),
            )
        assert "<td>Offline</td>" in b.client.get(BASE + "/workforce").text
        repo.set_device_status("report-device", "DISABLED")
        assert "No enabled device" in b.client.get(BASE + "/workforce").text


def test_invalid_filter_and_pages_do_not_log_sensitive_values(browser, caplog):
    b = browser
    sign_in(b)
    caplog.set_level("INFO",logger="zaza_server")
    caplog.clear()
    assert b.client.get(BASE + "/workforce/report?period=invalid").status_code == 400
    assert b.client.get(BASE + "/workspace?employee=unrelated").status_code == 403
    messages = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("zaza_server"))
    assert "db_ref=" in messages  # prove lazy pool logging was actually exercised
    for value in [
        "report-device",
        b.w.saas.tenants["a"].settings.dbname,
        "Shared Report",
        b.w.manager.email,
        PW,
        str(b.w.ctx["a"]["manager"].organization_id),
    ]:
        assert value not in messages
