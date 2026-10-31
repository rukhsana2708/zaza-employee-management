"""Phase C explicit-link self-service on isolated PostgreSQL tenant databases."""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from deskmate.zaza_server.app import create_saas_app
from deskmate.zaza_server.attendance import SummaryService
from deskmate.zaza_server.attendance.postgres_store import PostgresAttendanceStore
from deskmate.zaza_server.auth import register_device
from deskmate.zaza_server.workforce.service import WorkforceService
from deskmate.zaza_server.workforce_dashboard.queries import ScopedDashboardRepository

from .test_attendance_postgres import iso
from .test_saas_browser import EmployeeSynced, sign_in
from .test_saas_postgres import saas  # noqa: F401
from .test_workforce_foundation import workforce  # noqa: F401

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
BASE = "/app/orgs/tenant-a"
MY = ["my-work", "my-work/activity", "my-work/attendance", "my-work/applications"]
QUERY = "?period=custom&from=2026-11-02&to=2026-11-03"


def seed_person(repo, employee_id, device_id, suffix):
    if repo.get_device(device_id) is None:
        register_device(repo, device_id, employee_id)
    zone = ZoneInfo(repo.get_employee(employee_id).timezone)
    store = PostgresAttendanceStore(repo)
    for dow in range(1, 8):
        store.add_schedule(
            employee_id,
            is_working_day=True,
            day_of_week=dow,
            effective_from=date(2026, 1, 1),
            start_time=time(9),
            end_time=time(17),
            expected_work_seconds=8 * 3600,
        )
    sync = EmployeeSynced(repo, employee_id, device_id)
    for day in [date(2026, 11, 2), date(2026, 11, 3)]:
        begin = datetime.combine(day, time(9), tzinfo=zone).astimezone(UTC)
        sid = sync.session(begin, begin + timedelta(hours=1))
        rid = sync.period(sid, "ACTIVE", begin, begin + timedelta(hours=1))
        with repo.connection() as conn:
            conn.execute(
                "UPDATE activity_periods SET app_name=%s,window_title=%s WHERE period_id=%s",
                ("APP-" + suffix, "EVIDENCE-" + suffix, rid),
            )
        sync._put(
            "app_usage_daily",
            str(uuid.uuid4()),
            1,
            {
                "usage_date": day.isoformat(),
                "day_start_utc": iso(datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)),
                "day_end_utc": iso(
                    datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
                ),
                "app_name": "APP-" + suffix,
                "active_seconds": 3600,
                "idle_seconds": 0,
                "unknown_seconds": 0,
                "period_count": 1,
            },
            begin,
        )
    with repo.connection() as conn:
        conn.execute(
            "UPDATE devices SET last_seen_at=%s WHERE device_id=%s",
            (NOW - timedelta(seconds=10), device_id),
        )


@pytest.fixture
def personal(workforce):  # noqa: F811
    w = workforce
    c = w.saas.control
    w.admin = c.create_user("admin@mywork.test", "Admin")
    member = c.add_membership(w.ctx["a"]["owner"].organization_id, w.admin.user_id, "ADMIN")
    w.members["a"]["admin"] = member
    w.ctx["a"]["admin"] = w.saas.runtime.resolver.for_member(
        w.admin.user_id, member.organization_id
    )
    with w.saas.runtime.manager.repository(w.ctx["a"]["owner"]) as repo:
        repo.add_employee("admin-person", "Person admin A", timezone="Asia/Dhaka")
        for employee_id, device_id in [
            ("report", "report-device"),
            ("boss", "boss-device"),
            ("employee-1", "device-1"),
            ("admin-person", "admin-device"),
        ]:
            with repo.connection() as conn:
                conn.execute(
                    "UPDATE employees SET display_name=%s WHERE employee_id=%s",
                    ("Person " + employee_id + " A", employee_id),
                )
            seed_person(repo, employee_id, device_id, employee_id + "-A")
        SummaryService(PostgresAttendanceStore(repo), clock=lambda: NOW).recalculate(
            date(2026, 11, 2), date(2026, 11, 3)
        )
    for role, employee_id in [("owner", "employee-1"), ("admin", "admin-person")]:
        member = w.members["a"][role]
        w.service.link_account(
            w.ctx["a"]["owner"], employee_id, member.user_id, member.membership_id
        )
    w.service.add_reporting(w.ctx["a"]["owner"], "report", "boss", NOW - timedelta(days=1))
    clock = SimpleNamespace(now=NOW)
    w.saas.runtime.browser_clock = lambda: clock.now
    app = create_saas_app(w.saas.runtime)
    client = TestClient(app, follow_redirects=False)
    yield SimpleNamespace(w=w, client=client, app=app, clock=clock)
    client.close()


def test_personal_routes_security_and_no_management_controls(personal):
    p = personal
    for path in MY:
        assert p.client.get(BASE + "/" + path).status_code == 303
    sign_in(p, "employee")
    organizations = p.client.get("/app/organizations")
    assert BASE + "/my-work" in organizations.text
    for path in MY:
        response = p.client.get(BASE + "/" + path + QUERY)
        assert response.status_code == 200, response.text
        assert "Person report A" in response.text
        assert "Person boss A" not in response.text and "Person employee-1 A" not in response.text
        assert 'name="employee"' not in response.text and 'name="employee_id"' not in response.text
        assert BASE + "/workforce" not in response.text
        assert (
            "What ZaZa records" in response.text and "does not collect screenshots" in response.text
        )
        assert "Actual Work Time" not in response.text and "Productive Hours" not in response.text
        for header in [
            "content-security-policy",
            "x-frame-options",
            "x-content-type-options",
            "referrer-policy",
        ]:
            assert header in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert p.client.post(BASE + "/" + path, data={"employee_id": "boss"}).status_code == 405


@pytest.mark.parametrize(
    "role,eid",
    [
        ("employee", "report"),
        ("manager", "boss"),
        ("owner", "employee-1"),
        ("admin", "admin-person"),
    ],
)
def test_linked_roles_have_self_only_work_and_separate_reporting(personal, role, eid, monkeypatch):
    p = personal
    sign_in(p, role)
    captured = []
    original = ScopedDashboardRepository._rows

    def scoped(self, query, params=()):
        assert [(s.employee_id, s.kind) for s in self.scope.subjects] == [(eid, "self")]
        captured.append(query)
        return original(self, query, params)

    with monkeypatch.context() as scoped_queries:
        scoped_queries.setattr(ScopedDashboardRepository, "_rows", scoped)
        response = p.client.get(BASE + "/my-work" + QUERY)
    assert response.status_code == 200, response.text
    assert "EVIDENCE-" + eid + "-A" in response.text and "APP-" + eid + "-A" in response.text
    for other in {"report", "boss", "employee-1", "admin-person"} - {eid}:
        assert (
            "EVIDENCE-" + other + "-A" not in response.text
            and "APP-" + other + "-A" not in response.text
        )
    assert captured and any("activity_periods" in q for q in captured)
    if role in {"manager", "owner", "admin"}:
        assert BASE + "/workforce" in response.text
        assert p.client.get(BASE + "/workforce").status_code == 200
    else:
        assert BASE + "/workforce" not in response.text


@pytest.mark.parametrize(
    "role,eid",
    [
        ("employee", "report"),
        ("manager", "boss"),
        ("owner", "employee-1"),
        ("admin", "admin-person"),
    ],
)
def test_personal_lifecycle_cannot_be_bypassed_by_org_permissions(personal, role, eid):
    p = personal
    sign_in(p, role)
    owner = p.w.ctx["a"]["owner"]
    for version, status, expected in [
        (1, "LEAVE", 200),
        (2, "INACTIVE", 403),
        (3, "TERMINATED", 403),
    ]:
        p.w.service.update_profile(owner, eid, version=version, employment_status=status)
        response = p.client.get(BASE + "/my-work")
        assert response.status_code == expected
        if status == "LEAVE":
            assert "On leave" in response.text
        if expected == 403:
            assert "EVIDENCE-" + eid not in response.text
        if role in {"owner", "admin"}:
            assert p.client.get(BASE + "/workforce").status_code == 200
    with p.w.saas.runtime.manager.repository(owner) as repo:
        assert repo.get_employee(eid).is_active
        assert repo.count_records() > 0


@pytest.mark.parametrize(
    "role,eid", [("owner", "employee-1"), ("admin", "admin-person"), ("employee", "report")]
)
def test_unlinked_users_get_no_artificial_personal_workspace(personal, role, eid):
    p = personal
    p.w.service.revoke_link(p.w.ctx["a"]["owner"], eid)
    sign_in(p, role)
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        before = repo.list_employees()
    for path in MY:
        assert p.client.get(BASE + "/" + path).status_code == 403
    if role in {"owner", "admin"}:
        company = p.client.get(BASE + "/workforce")
        assert company.status_code == 200 and BASE + "/my-work" not in company.text
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        assert repo.list_employees() == before


def test_viewer_link_does_not_grant_self_capability(personal):
    sign_in(personal, "viewer")
    assert personal.client.get(BASE + "/workspace").status_code == 200
    for path in MY:
        assert personal.client.get(BASE + "/" + path).status_code == 403


def test_identity_query_and_form_impersonation_is_ignored_or_refused(personal):
    p = personal
    sign_in(p, "owner")
    for path in MY:
        normal = p.client.get(BASE + "/" + path + QUERY)
        attack = p.client.get(
            BASE
            + "/"
            + path
            + QUERY
            + "&"
            + urlencode(
                {
                    "employee": "report",
                    "employee_id": "boss",
                    "user_id": str(p.w.employee.user_id),
                    "display_name": "Person report A",
                }
            )
        )
        assert normal.status_code == attack.status_code == 200
        assert normal.text == attack.text
    assert p.client.get(BASE + "/my-work/report").status_code == 404


def test_link_revocation_and_explicit_replacement_after_login(personal):
    p = personal
    sign_in(p, "employee")
    assert "EVIDENCE-report-A" in p.client.get(BASE + "/my-work" + QUERY).text
    p.w.service.revoke_link(p.w.ctx["a"]["owner"], "report")
    assert p.client.get(BASE + "/my-work").status_code == 403
    p.w.service.revoke_link(p.w.ctx["a"]["owner"], "employee-1")
    member = p.w.members["a"]["employee"]
    p.w.service.link_account(
        p.w.ctx["a"]["owner"], "employee-1", member.user_id, member.membership_id
    )
    after = p.client.get(BASE + "/my-work" + QUERY)
    assert after.status_code == 200 and "EVIDENCE-employee-1-A" in after.text
    assert "EVIDENCE-report-A" not in after.text


def test_tenant_local_links_and_identical_ids(personal):
    p = personal
    with p.w.saas.runtime.manager.repository(p.w.ctx["b"]["owner"]) as repo:
        seed_person(repo, "report", "report-device", "report-B")
    sign_in(p, "employee")
    a = p.client.get(BASE + "/my-work" + QUERY)
    b = p.client.get("/app/orgs/tenant-b/my-work" + QUERY)
    assert a.status_code == b.status_code == 200
    assert "EVIDENCE-report-A" in a.text and "EVIDENCE-report-B" not in a.text
    assert "EVIDENCE-report-B" in b.text and "EVIDENCE-report-A" not in b.text
    p.w.service.revoke_link(p.w.ctx["b"]["owner"], "report")
    assert p.client.get("/app/orgs/tenant-b/my-work").status_code == 403
    assert p.client.get(BASE + "/my-work").status_code == 200
    member = p.w.members["b"]["employee"]
    # A valid second membership can link to a DIFFERENT employee, but never reuse A's link.
    p.w.service.revoke_link(p.w.ctx["b"]["owner"], "boss")
    p.w.service.link_account(p.w.ctx["b"]["owner"], "boss", member.user_id, member.membership_id)
    with p.w.saas.runtime.manager.repository(p.w.ctx["b"]["owner"]) as repo:
        seed_person(repo, "boss", "boss-device", "boss-B")
    different = p.client.get("/app/orgs/tenant-b/my-work" + QUERY)
    assert different.status_code == 200 and "EVIDENCE-boss-B" in different.text
    assert "EVIDENCE-report-A" not in different.text and "EVIDENCE-report-B" not in different.text
    assert "EVIDENCE-report-A" in p.client.get(BASE + "/my-work" + QUERY).text
    p.w.saas.control.set_membership_status(member.organization_id, member.user_id, "DISABLED")
    assert p.client.get("/app/orgs/tenant-b/my-work").status_code == 404
    assert p.client.get("/app/orgs/unknown/my-work").status_code == 404


@pytest.mark.parametrize(
    "change", ["user", "membership", "organization", "tenant", "role", "legacy_active"]
)
def test_fresh_lifecycle_and_capability_checks(personal, change):
    p = personal
    sign_in(p, "employee")
    c = p.w.saas.control
    m = p.w.members["a"]["employee"]
    if change == "user":
        c.set_user_status(m.user_id, "DISABLED")
    if change == "membership":
        c.set_membership_status(m.organization_id, m.user_id, "DISABLED")
    if change == "organization":
        c.set_organization_status(m.organization_id, "SUSPENDED")
    if change == "tenant":
        c.mark_tenant_database(m.organization_id, "ERROR")
    if change == "role":
        c.change_membership_role(m.organization_id, m.user_id, "VIEWER")
    if change == "legacy_active":
        with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
            repo.set_employee_active("report", False)
    response = p.client.get(BASE + "/my-work")
    assert response.status_code in {303, 403, 404}
    assert "EVIDENCE-report-A" not in response.text


def test_manager_self_survives_reporting_and_role_changes(personal):
    p = personal
    sign_in(p, "manager")
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        before = repo.list_records(device_id="boss-device")
        with repo.connection() as conn:
            relation = conn.execute(
                "SELECT id FROM employee_reporting_relationships WHERE employee_id='report'"
            ).fetchone()["id"]
    assert p.client.get(BASE + "/workforce/report").status_code == 200
    p.w.service.end_reporting(p.w.ctx["a"]["owner"], relation, NOW - timedelta(seconds=1))
    assert p.client.get(BASE + "/workforce/report").status_code == 404
    assert "EVIDENCE-boss-A" in p.client.get(BASE + "/my-work/activity" + QUERY).text
    p.w.saas.control.change_membership_role(
        p.w.ctx["a"]["manager"].organization_id, p.w.manager.user_id, "EMPLOYEE"
    )
    response = p.client.get(BASE + "/my-work" + QUERY)
    assert response.status_code == 200 and BASE + "/workforce" not in response.text
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        assert repo.list_records(device_id="boss-device") == before
        assert p.w.service.profile(p.w.ctx["a"]["owner"], "boss")["monitoring_enabled"]


def test_unmonitored_leave_timezone_and_no_private_metadata(personal):
    p = personal
    p.w.service.update_profile(p.w.ctx["a"]["owner"], "report", version=1, monitoring_enabled=False)
    sign_in(p, "employee")
    response = p.client.get(BASE + "/my-work" + QUERY)
    assert response.status_code == 200 and "Monitoring not enabled" in response.text
    assert "EVIDENCE-report-A" in response.text
    for value in [
        str(p.w.members["a"]["employee"].membership_id),
        str(p.w.employee.user_id),
        p.w.saas.tenants["a"].settings.dbname,
        p.w.saas.tenants["a"].token,
    ]:
        assert value not in response.text
    sign_in(p, "admin")
    p.clock.now = datetime(2026, 11, 3, 22, tzinfo=UTC)
    response = p.client.get(BASE + "/my-work/attendance")
    assert (
        response.status_code == 200
        and "Asia/Dhaka" in response.text
        and "2026-11-04" in response.text
    )
    assert "No calculated attendance summary" in response.text


def test_read_only_sessions_privacy_and_rendered_pages(personal, caplog):
    p = personal
    sign_in(p, "employee")
    svc = WorkforceService(p.w.saas.control, p.w.saas.runtime.resolver, p.w.saas.runtime.manager)
    with svc.self_dashboard(p.w.ctx["a"]["employee"]) as (conn, scope):
        assert (
            conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        )
        assert len(scope.subjects) == 1 and scope.subjects[0].employee_id == "report"
    caplog.set_level("INFO", logger="zaza_server")
    caplog.clear()
    folder = Path(".pytest_cache/phase-c-pages")
    folder.mkdir(exist_ok=True)
    for path in MY:
        response = p.client.get(BASE + "/" + path + QUERY)
        assert response.status_code == 200
        (folder / (path.replace("/", "-") + ".html")).write_text(response.text, encoding="utf-8")
    logs = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("zaza_server"))
    for secret in [
        "report-device",
        p.w.employee.email,
        p.w.saas.tenants["a"].token,
        str(p.w.employee.user_id),
    ]:
        assert secret not in logs
    csrf = p.client.get("/app/session").json()["csrf_token"]
    assert (
        p.client.post(
            "/app/sign-out", data={"csrf_token": csrf}, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        p.client.post(
            "/app/sign-out", data={"csrf_token": csrf}, headers={"Origin": "http://testserver"}
        ).status_code
        == 303
    )
    assert p.client.get(BASE + "/my-work").status_code == 303


def test_personal_xss_and_no_mutation_or_upload_features(personal):
    p = personal
    marker = '<script>alert("private")</script>'
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        with repo.connection() as conn:
            conn.execute(
                "UPDATE employees SET display_name=%s WHERE employee_id='report'", (marker,)
            )
            conn.execute(
                "UPDATE activity_periods SET window_title=%s WHERE employee_id='report'", (marker,)
            )
        before = repo.list_records(device_id="report-device")
    sign_in(p, "employee")
    for path in MY:
        response = p.client.get(BASE + "/" + path + QUERY)
        assert (
            response.status_code == 200
            and marker not in response.text
            and "&lt;script&gt;" in response.text
        )
        assert 'type="file"' not in response.text and 'name="employee"' not in response.text
        assert "My Tasks" not in response.text and "Start Task" not in response.text
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        assert repo.list_records(device_id="report-device") == before


def test_expired_session_and_denied_org_never_keep_my_identity(personal):
    p = personal
    sign_in(p, "employee")
    auth = p.w.saas.runtime.auth
    clock = auth.clock
    auth.clock = lambda: clock() + timedelta(days=2)
    for path in MY:
        response = p.client.get(BASE + "/" + path)
        assert response.status_code == 303 and "EVIDENCE-report-A" not in response.text
    auth.clock = clock
    # Existing JSON login/session contracts still share exactly this session system.
    assert p.client.get("/app/session").status_code == 200
    p.w.saas.control.set_membership_status(
        p.w.ctx["a"]["employee"].organization_id, p.w.employee.user_id, "DISABLED"
    )
    assert p.client.get(BASE + "/my-work").status_code == 404


def test_link_revocation_survives_database_clock_regression(personal):
    p = personal
    sign_in(p, "employee")
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        with repo.connection() as conn:
            # Deterministically model a clock which moved backwards after link creation.
            conn.execute(
                "UPDATE employee_account_links SET created_at=clock_timestamp()+interval '1 minute' "
                "WHERE employee_id='report' AND revoked_at IS NULL"
            )
    assert p.w.service.revoke_link(p.w.ctx["a"]["owner"], "report") == 1
    assert p.client.get(BASE + "/my-work").status_code == 403
    with p.w.saas.runtime.manager.repository(p.w.ctx["a"]["owner"]) as repo:
        with repo.connection() as conn:
            row = conn.execute(
                "SELECT created_at,revoked_at FROM employee_account_links "
                "WHERE employee_id='report'"
            ).fetchone()
            assert row["revoked_at"] >= row["created_at"]
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM employee_account_links "
                    "WHERE employee_id='report' AND revoked_at IS NULL"
                ).fetchone()["n"]
                == 0
            )
