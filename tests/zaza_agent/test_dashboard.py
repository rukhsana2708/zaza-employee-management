"""Phase 7 — manager web dashboard (no database, no Google, no network).

Summaries are REAL Phase 5 results: the attendance engine calculates them
from synced periods, then they are loaded into the in-memory dashboard
repository. The web layer is exercised through FastAPI's TestClient.
PostgreSQL behaviour (migration, constraints, schedule validation, CLI) is
in test_dashboard_postgres.py (opt-in).
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("jinja2")
pytest.importorskip("argon2")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from deskmate.zaza_server.attendance.models import (  # noqa: E402
    AttendanceStatus,
    DeviceInput,
    PeriodInput,
    ScheduleRule,
)
from deskmate.zaza_server.attendance.store import InMemoryAttendanceStore  # noqa: E402
from deskmate.zaza_server.attendance.summary_service import SummaryService  # noqa: E402
from deskmate.zaza_server.dashboard.auth import LOGIN_FAILED, ManagerAuth  # noqa: E402
from deskmate.zaza_server.dashboard.config import (  # noqa: E402
    DashboardSettings,
    dashboard_settings_from_env,
)
from deskmate.zaza_server.dashboard.models import (  # noqa: E402
    AppUsage,
    CurrentStatus,
    Device,
    Employee,
    Period,
)
from deskmate.zaza_server.dashboard.queries import (  # noqa: E402
    DuplicateUsername,
    InMemoryDashboardRepository,
)
from deskmate.zaza_server.dashboard.routes import mount_dashboard  # noqa: E402
from deskmate.zaza_server.dashboard.security import (  # noqa: E402
    COOKIE_NAME,
    PasswordPolicyError,
    check_password_policy,
    token_hash,
)
from deskmate.zaza_server.dashboard.service import (  # noqa: E402
    DashboardService,
    employee_status,
    period_range,
)

UTC = timezone.utc
H = 3600
DHAKA = "Asia/Dhaka"
NY = "America/New_York"
MON = date(2026, 10, 5)
TUE, WED, THU = (MON + timedelta(days=i) for i in range(1, 4))
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)  # Thu 12:00 Dhaka, Thu 02:00 New York
PASSWORD = "correct-horse-battery-staple"


def at(day: date, hhmm: str, tz: str = DHAKA) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm)).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Dash:
    """Employees, schedules and synced activity → real Phase 5 summaries →
    the in-memory dashboard repository."""

    def __init__(self, now: datetime = NOW) -> None:
        self.clock = Clock(now)
        self.attendance = InMemoryAttendanceStore()
        self.repo = InMemoryDashboardRepository()

    def employee(self, eid: str, name: str, tz: str = DHAKA, *, start: str = "09:00", end: str = "17:00",
                 active: bool = True, seen: datetime | None = None) -> None:
        self.repo.add_employee(Employee(eid, name, tz, active))
        self.attendance.add_employee(eid, tz, is_active=active)
        self.attendance.add_device(eid, DeviceInput(f"{eid}-pc", "ACTIVE", datetime(2027, 1, 1, tzinfo=UTC)))
        self.repo.add_device(Device(f"{eid}-pc", eid, "ACTIVE", seen or self.clock.now - timedelta(seconds=30)))
        for dow in range(1, 8):
            working = dow <= 5
            rule = ScheduleRule(str(uuid.uuid4()), eid, working, tz, day_of_week=dow, effective_from=date(2026, 1, 1),
                                start_time=time.fromisoformat(start) if working else None,
                                end_time=time.fromisoformat(end) if working else None,
                                expected_work_seconds=8 * H if working else None)
            self.attendance.add_rule(rule)
            self.repo.rules[rule.schedule_id] = rule

    def rule(self, rule: ScheduleRule) -> None:
        self.attendance.add_rule(rule)
        self.repo.rules[rule.schedule_id] = rule

    def period(self, eid: str, status: str, start: datetime, end: datetime, *, app: str = "Code.exe",
               title: str = "main.py", domain: str | None = None, privacy: bool = False) -> None:
        pid = str(uuid.uuid4())
        self.attendance.add_period(eid, PeriodInput(pid, f"{eid}-pc", start, end, status))
        self.repo.periods.append(Period(pid, eid, f"{eid}-pc", start, end, (end - start).total_seconds(), status,
                                        False, app, title, domain, privacy))

    def build(self) -> Dash:
        SummaryService(self.attendance, clock=self.clock).recalculate(date(2026, 9, 1), self.clock.now.date())
        for _, s in self.attendance.daily.values():
            self.repo.add_summary(s)
        self.repo.updated_at = self.clock.now - timedelta(minutes=3)
        return self

    def service(self, **kw) -> DashboardService:
        return DashboardService(self.repo, DashboardSettings(**kw), self.clock)

    def auth(self) -> ManagerAuth:
        return ManagerAuth(self.repo, DashboardSettings(), self.clock)

    def client(self, settings: DashboardSettings | None = None, base_url: str = "http://testserver") -> TestClient:
        app = FastAPI()
        mount_dashboard(app, self.repo, settings or DashboardSettings(), self.clock)
        return TestClient(app, follow_redirects=False, base_url=base_url)

    def manager(self, username: str = "manager", role: str = "MANAGER", password: str = PASSWORD):  # noqa: ANN201
        return self.auth().create_user(username, "Project Manager", password, role=role)


def standard() -> Dash:
    d = Dash()
    d.employee("alice", "Alice")
    d.employee("bob", "Bob")
    d.employee("carol", "Carol", start="20:00", end="04:00")  # night shift
    d.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    d.period("alice", "ACTIVE", at(TUE, "09:30"), at(TUE, "17:00"))               # late 30 min
    d.period("alice", "IDLE", at(WED, "09:00"), at(WED, "17:00"))                 # absent
    d.period("alice", "ACTIVE", at(THU, "09:00"), at(THU, "12:00"))               # today, in progress
    d.period("bob", "UNKNOWN", at(MON, "09:00"), at(MON, "17:00"))                # data incomplete
    d.period("bob", "ACTIVE", at(THU, "09:45"), at(THU, "11:59"))                 # today: late
    d.period("carol", "ACTIVE", at(MON, "19:30"), at(TUE, "04:45"))               # overnight + overtime
    d.period("carol", "LOCKED", at(TUE, "04:45"), at(TUE, "05:15"))
    d.repo.usage += [AppUsage("alice", MON, "Code.exe", 6 * H, 600, 0), AppUsage("alice", MON, "Chrome", 2 * H, 0, 60),
                     AppUsage("alice", TUE, "Code.exe", 7 * H, 0, 0), AppUsage("bob", MON, "Code.exe", 0, 0, 8 * H)]
    return d.build()


def login(d: Dash, client: TestClient | None = None, username: str = "manager", password: str = PASSWORD):  # noqa: ANN201
    client = client or d.client()
    page = client.get("/manager/login")
    token = re.search(r'name="login_token" value="([^"]+)"', page.text).group(1)
    response = client.post("/manager/login", data={"username": username, "password": password, "login_token": token})
    return client, response


def csrf(client: TestClient, path: str = "/manager/schedules") -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


# ─── passwords and accounts ────────────────────────────────────────────────


def test_password_is_never_stored_in_plaintext():
    d = standard()
    user = d.manager()
    assert user.password_hash.startswith("$argon2id$") and PASSWORD not in user.password_hash
    assert PASSWORD not in repr(user) and "argon2" not in repr(user)
    assert all(PASSWORD not in str(row) for row in d.repo.audit_rows)


def test_usernames_are_case_insensitive():
    d = standard()
    assert d.manager("Manager").username == "manager"
    for name in ("manager", "MANAGER", " Manager "):
        with pytest.raises(DuplicateUsername):
            d.manager(name)
    _, response = login(d, username="MANAGER")
    assert response.status_code == 303 and response.headers["location"] == "/manager"


@pytest.mark.parametrize(("password", "message"), [
    ("short-pass1", "at least 12"), ("manager-is-my-password", "username"), ("aaaaaaaaaaaaaaaa", "repetitive"),
    (" leading-space-password", "spaces")])
def test_password_policy(password, message):
    with pytest.raises(PasswordPolicyError, match=message):
        check_password_policy(password, username="manager")


@pytest.mark.parametrize("case", ["wrong password", "unknown user", "disabled"])
def test_login_failures_are_generic(case):
    d = standard()
    d.manager()
    if case == "disabled":
        d.auth().set_active("manager", False)
    username, password = {"wrong password": ("manager", "not-the-password!"), "unknown user": ("nobody", PASSWORD),
                          "disabled": ("manager", PASSWORD)}[case]
    client, response = login(d, username=username, password=password)
    assert response.status_code == 401 and LOGIN_FAILED in response.text
    assert COOKIE_NAME not in response.cookies
    assert client.get("/manager").status_code == 303


def test_successful_login_records_last_login_and_audits():
    d = standard()
    user = d.manager()
    _, response = login(d)
    assert response.status_code == 303
    assert d.repo.user_rows[user.manager_user_id].last_login_at == NOW
    assert [r["action"] for r in d.repo.audit_rows][-1] == "manager.login"
    assert d.repo.audit_rows[-1]["actor_id"] == "manager"


def test_session_token_is_stored_hashed_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    d = standard()
    d.manager()
    client, response = login(d)
    token = client.cookies.get(COOKIE_NAME)
    assert token and len(token) >= 40
    (session,) = d.repo.session_rows.values()
    assert session.session_token_hash == token_hash(token)
    stored = str(list(d.repo.session_rows.values())) + str(d.repo.audit_rows) + str(d.repo.user_rows)
    assert token not in stored
    page = client.get("/manager").text
    assert token not in page and session.session_token_hash not in page
    assert token not in caplog.text


def test_password_reset_changes_hash_and_revokes_sessions():
    d = standard()
    user = d.manager()
    client, _ = login(d)
    assert client.get("/manager").status_code == 200
    old_hash = d.repo.user_rows[user.manager_user_id].password_hash
    assert d.auth().reset_password("manager", "another-long-passphrase") == 1
    assert d.repo.user_rows[user.manager_user_id].password_hash != old_hash
    assert client.get("/manager").status_code == 303  # signed out immediately
    _, response = login(d)  # old password no longer works
    assert response.status_code == 401
    _, response = login(d, password="another-long-passphrase")
    assert response.status_code == 303


def test_revoked_and_expired_sessions_stop_working():
    d = standard()
    d.manager()
    client, _ = login(d)
    d.auth().revoke_sessions("manager")
    assert client.get("/manager").status_code == 303
    client, _ = login(d)
    d.clock.now += timedelta(hours=12, seconds=1)  # ZAZA_DASHBOARD_SESSION_HOURS=12
    assert client.get("/manager").status_code == 303
    assert client.get("/manager/api/current-status").status_code == 401


def test_disabled_manager_cannot_continue():
    d = standard()
    d.manager()
    client, _ = login(d)
    assert d.auth().set_active("manager", False) == 1
    assert client.get("/manager/attendance").status_code == 303
    assert any(r["action"] == "manager.disable" for r in d.repo.audit_rows)


def test_logout_revokes_the_session():
    d = standard()
    d.manager()
    client, _ = login(d)
    token = client.cookies.get(COOKIE_NAME)
    response = client.post("/manager/logout", data={"csrf_token": csrf(client, "/manager")})
    assert response.status_code == 303 and response.headers["location"] == "/manager/login"
    assert next(iter(d.repo.session_rows.values())).revoked_at == NOW
    client.cookies.set(COOKIE_NAME, token, path="/manager")  # replaying the old cookie fails
    assert client.get("/manager").status_code == 303
    assert d.repo.audit_rows[-1]["action"] == "manager.logout"


# ─── routes, cookies, CSRF, headers ────────────────────────────────────────

PAGES = ["/manager", "/manager/employees", "/manager/employees/alice", "/manager/attendance",
         "/manager/applications", "/manager/schedules"]


@pytest.mark.parametrize("path", PAGES)
def test_unauthenticated_pages_redirect_to_login(path):
    response = standard().client().get(path)
    assert response.status_code == 303 and response.headers["location"] == "/manager/login"


@pytest.mark.parametrize("path", ["/manager/api/current-status", "/manager/api/overview", "/manager/api/attendance",
                                  "/manager/api/applications"])
def test_unauthenticated_api_is_refused(path):
    assert standard().client().get(path).status_code == 401


@pytest.mark.parametrize("path", PAGES)
def test_authenticated_manager_can_open_every_page(path):
    d = standard()
    d.manager()
    client, _ = login(d)
    response = client.get(path)
    assert response.status_code == 200, response.text[:500]
    assert "<script>" not in response.text  # no inline scripts (CSP)


def test_session_cookie_flags_follow_configuration():
    d = standard()
    d.manager()
    _, response = login(d)
    cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/manager" in cookie and "Max-Age=43200" in cookie and "Secure" not in cookie
    _, response = login(d, d.client(DashboardSettings(cookie_secure=True, session_hours=2), "https://testserver"))
    secure_cookie = [c for c in response.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME)][0]
    assert "Secure" in secure_cookie and "Max-Age=7200" in secure_cookie


@pytest.mark.parametrize("path", ["/manager/login", "/manager", "/manager/static/dashboard.css"])
def test_security_headers(path):
    d = standard()
    d.manager()
    client, _ = login(d)
    headers = client.get(path).headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    csp = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and "default-src 'none'" in csp


def _date_rule_form(token: str, **extra) -> dict:
    return {"csrf_token": token, "employee_id": "alice", "schedule_date": "2026-10-20", "start": "10:00",
            "end": "14:00", "expected_hours": "4", **extra}


def test_mutations_require_the_session_csrf_token():
    d = standard()
    d.manager()
    client, _ = login(d)
    rules = dict(d.repo.rules)
    for form in (_date_rule_form(""), _date_rule_form("forged-token"), {k: v for k, v in _date_rule_form("").items()
                                                                       if k != "csrf_token"}):
        response = client.post("/manager/schedules/date", data=form)
        assert response.status_code == 403 and "expired" in response.text
    response = client.post("/manager/schedules/date", data=_date_rule_form(csrf(client)),
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert d.repo.rules == rules
    response = client.post("/manager/logout", data={"csrf_token": "nope"})
    assert response.status_code == 403 and client.get("/manager").status_code == 200  # still signed in
    response = client.post("/manager/schedules/date", data=_date_rule_form(csrf(client)))
    assert response.status_code == 303 and len(d.repo.rules) == len(rules) + 1


def test_another_sessions_csrf_token_is_rejected():
    d = standard()
    d.manager()
    first, _ = login(d)
    second, _ = login(d)
    token_of_first = csrf(first)
    response = second.post("/manager/schedules/date", data=_date_rule_form(token_of_first))
    assert response.status_code == 403


def test_get_requests_cannot_mutate_schedules():
    d = standard()
    d.manager()
    client, _ = login(d)
    rules = dict(d.repo.rules)
    rule_id = next(iter(rules))
    for path in ("/manager/schedules/date", "/manager/schedules/weekly", f"/manager/schedules/{rule_id}/remove",
                 f"/manager/schedules/{rule_id}/update"):
        assert client.get(path, params=_date_rule_form(csrf(client))).status_code == 405
    assert d.repo.rules == rules
    assert not any(r["entity_type"] == "work_schedule" for r in d.repo.audit_rows)


def test_login_form_has_its_own_csrf_token():
    d = standard()
    d.manager()
    client = d.client()
    client.get("/manager/login")
    response = client.post("/manager/login", data={"username": "manager", "password": PASSWORD})
    assert response.status_code == 403 and COOKIE_NAME not in response.cookies


def test_pages_expose_no_secrets():
    d = standard()
    user = d.manager()
    client, _ = login(d)
    token = client.cookies.get(COOKIE_NAME)
    for path in PAGES + ["/manager/api/current-status", "/manager/api/overview", "/manager/api/attendance",
                         "/manager/api/applications"]:
        body = client.get(path).text.replace(csrf(client, "/manager"), "<csrf>")  # the form token is expected
        assert token not in body and user.password_hash not in body and "argon2" not in body
        assert not re.search(r"\b[0-9a-f]{64}\b", body)  # no token / content / summary hashes
        for word in ("payload", "content_hash", "record_version", "password_hash", "ZAZA_DB", "bearer"):
            assert word not in body.lower()


def test_dashboard_does_not_need_google_libraries():
    code = ("import sys; from fastapi import FastAPI; "
            "from deskmate.zaza_server.dashboard.routes import mount_dashboard; "
            "from deskmate.zaza_server.dashboard.queries import InMemoryDashboardRepository; "
            "mount_dashboard(FastAPI(), InMemoryDashboardRepository()); "
            "sys.exit(any(m.startswith(('googleapiclient', 'google.oauth2', 'deskmate.zaza_server.sheets')) "
            "for m in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_settings_from_env():
    s = dashboard_settings_from_env({"ZAZA_DASHBOARD_SESSION_HOURS": "8", "ZAZA_DASHBOARD_COOKIE_SECURE": "true",
                                     "ZAZA_DASHBOARD_ONLINE_THRESHOLD_SECONDS": "120",
                                     "ZAZA_DASHBOARD_ACTIVITY_ROWS": "50"})
    assert (s.session_hours, s.cookie_secure, s.online_threshold_seconds, s.activity_rows) == (8, True, 120, 50)
    assert dashboard_settings_from_env({}) == DashboardSettings(12, False, 300, 200)
    from deskmate.zaza_server.config import ConfigError
    for env in ({"ZAZA_DASHBOARD_SESSION_HOURS": "0"}, {"ZAZA_DASHBOARD_COOKIE_SECURE": "maybe"},
                {"ZAZA_DASHBOARD_ACTIVITY_ROWS": "lots"}):
        with pytest.raises(ConfigError):
            dashboard_settings_from_env(env)


# ─── XSS ───────────────────────────────────────────────────────────────────

EVIL_NAME = "<script>alert(1)</script>"
EVIL_TITLE = "<img src=x onerror=alert(1)>"
EVIL_APP = '"><script>alert(1)</script>'
EVIL_DOMAIN = "javascript:alert(1)"


def test_stored_values_are_rendered_as_text_only():
    d = Dash()
    d.employee("evil", EVIL_NAME)
    d.period("evil", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"), app=EVIL_APP, title=EVIL_TITLE, domain=EVIL_DOMAIN)
    d.repo.usage.append(AppUsage("evil", MON, EVIL_APP, 8 * H, 0, 0))
    d.build()
    d.manager()
    client, _ = login(d)
    week = {"period": "custom", "from": "2026-10-05", "to": "2026-10-11"}
    pages = [client.get("/manager").text, client.get("/manager/employees").text,
             client.get("/manager/employees/evil", params=week).text,
             client.get("/manager/attendance", params=week).text,
             client.get("/manager/applications", params=week).text, client.get("/manager/schedules").text]
    for html in pages:
        assert EVIL_NAME not in html and EVIL_TITLE not in html and EVIL_APP not in html
        assert "<script>alert" not in html and "<img src=x" not in html
        assert 'href="javascript:' not in html
    detail = pages[2]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail      # employee name, escaped
    assert "&lt;img src=x onerror=alert(1)&gt;" in detail          # window title, escaped
    assert "&#34;&gt;&lt;script&gt;" in detail                      # application, escaped
    assert EVIL_DOMAIN in detail                                    # shown as plain text, not a link
    js = (Path(__file__).parents[2] / "deskmate/zaza_server/dashboard/static/dashboard.js").read_text()
    assert "innerHTML" not in js.replace("never innerHTML", "") and "textContent" in js


def test_api_returns_untrusted_text_as_json_strings():
    d = Dash()
    d.employee("evil", EVIL_NAME)
    d.build()
    d.manager()
    client, _ = login(d)
    response = client.get("/manager/api/current-status")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["employees"][0]["employee_id"] == "evil"


# ─── period filters and KPI calculations ───────────────────────────────────


def _rows_in(d: Dash, ranges: dict[str, tuple[date, date]]) -> list:
    return [s for (eid, day), s in d.repo.summaries.items() if eid in ranges and ranges[eid][0] <= day <= ranges[eid][1]]


def _check_totals(totals, rows) -> None:  # noqa: ANN001
    assert totals.scheduled == sum(s.scheduled_seconds for s in rows)
    assert totals.tracked == sum(s.tracked_seconds for s in rows)
    assert totals.active == sum(s.active_seconds for s in rows)
    assert totals.idle == sum(s.idle_seconds for s in rows)
    assert totals.unknown == sum(s.unknown_seconds for s in rows)
    assert totals.locked == sum(s.locked_seconds for s in rows)
    assert totals.overtime == sum(s.overtime_seconds for s in rows)
    credit, basis = sum(s.attendance_credit_seconds for s in rows), sum(s.attendance_basis_seconds for s in rows)
    assert totals.attendance_fraction == (credit / basis if basis else None)


@pytest.mark.parametrize(("kind", "start", "end"), [
    ("today", THU, THU), ("yesterday", WED, WED), ("this_week", MON, MON + timedelta(days=6)),
    ("last_week", MON - timedelta(days=7), MON - timedelta(days=1)),
    ("this_month", date(2026, 10, 1), date(2026, 10, 31)), ("last_month", date(2026, 9, 1), date(2026, 9, 30)),
])
def test_period_filters_all_employees(kind, start, end):
    d = standard()
    svc = d.service()
    sel = svc.selection(kind)
    assert {(r.start, r.end) for r in sel.ranges} == {(start, end)} and sel.uniform
    totals = svc.overview(sel)["totals"]
    _check_totals(totals, _rows_in(d, {e: (start, end) for e in ("alice", "bob", "carol")}))


def test_period_ranges():
    assert period_range("this_week", date(2026, 10, 11)) == (date(2026, 10, 5), date(2026, 10, 11))  # Sunday
    assert period_range("last_month", date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))
    with pytest.raises(ValueError, match="before"):
        period_range("custom", THU, THU, MON)
    with pytest.raises(ValueError, match="at most"):
        period_range("custom", THU, date(2025, 1, 1), date(2026, 6, 1))


def test_today_one_employee_and_all_employees():
    d = standard()
    svc = d.service()
    one = svc.overview(svc.selection("today", "alice"))["totals"]
    alice = d.repo.summaries[("alice", THU)]
    assert (one.active, one.scheduled) == (alice.active_seconds, alice.scheduled_seconds) == (3 * H, 8 * H)
    every = svc.overview(svc.selection("today"))["totals"]
    _check_totals(every, [d.repo.summaries[(e, THU)] for e in ("alice", "bob", "carol")])
    assert every.active == 3 * H + (2 * H + 14 * 60)
    assert every.late_employees == ["Bob"] and every.absent_employees == [] and every.incomplete_employees == []


def test_custom_range_and_employee_filter():
    d = standard()
    svc = d.service()
    sel = svc.selection("custom", "alice", MON, WED)
    t = svc.overview(sel)["totals"]
    _check_totals(t, _rows_in(d, {"alice": (MON, WED)}))
    assert (t.late_employees, t.absent_employees) == (["Alice"], ["Alice"])
    assert (t.days, t.late_days, t.absent_days, t.days_worked) == (3, 1, 1, 2)
    week = svc.overview(svc.selection("custom", None, MON, THU))["totals"]
    assert week.late_employees == ["Alice", "Bob"]
    assert week.absent_employees == ["Alice", "Bob", "Carol"]   # Bob Tue/Wed, Carol Tue/Wed nights
    assert week.incomplete_employees == ["Bob"]                 # Monday: UNKNOWN all shift


def test_attendance_percentage_is_credit_over_basis_not_an_average():
    d = Dash()
    d.employee("alice", "Alice")
    d.rule(ScheduleRule(str(uuid.uuid4()), "alice", True, DHAKA, schedule_date=TUE, start_time=time(9),
                        end_time=time(11), expected_work_seconds=2 * H))       # a 2-hour day
    d.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))           # 8/8 h = 100%
    d.build()                                                                  # Tue: absent, 0/2 h = 0%
    svc = d.service()
    t = svc.overview(svc.selection("custom", "alice", MON, TUE))["totals"]
    assert (t.credit, t.basis) == (8 * H, 10 * H)
    assert t.attendance_fraction == pytest.approx(0.8)  # not (100% + 0%) / 2 = 50%
    client_d = d
    client_d.manager()
    client, _ = login(client_d)
    body = client.get("/manager/api/overview", params={"period": "custom", "from": "2026-10-05", "to": "2026-10-06",
                                                       "employee": "alice"}).json()
    assert body["totals"]["attendance_percentage"] == 80.0


def test_mixed_timezones_use_each_employee_local_period():
    d = Dash(now=datetime(2026, 10, 7, 18, 30, tzinfo=UTC))  # Dhaka: Thu 8 Oct 00:30; New York: Wed 7 Oct 14:30
    d.employee("alice", "Alice", DHAKA)
    d.employee("dan", "Dan", NY)
    d.period("dan", "ACTIVE", at(WED, "09:00", NY), at(WED, "12:00", NY))
    d.build()
    svc = d.service()
    sel = svc.selection("today")
    assert {r.employee.employee_id: r.start for r in sel.ranges} == {"alice": THU, "dan": WED}
    view = svc.overview(sel)
    assert view["period"]["uniform"] is False and "differ" in view["period"]["label"]
    _check_totals(view["totals"], [d.repo.summaries[("alice", THU)], d.repo.summaries[("dan", WED)]])
    assert view["totals"].active == 3 * H  # Dan's Wednesday morning, not Alice's empty Wednesday
    custom = svc.selection("custom", None, WED, WED)
    assert {(r.start, r.end) for r in custom.ranges} == {(WED, WED)}  # custom dates stay local calendar dates
    d.manager()
    client, _ = login(d)
    html = client.get("/manager").text
    assert "local periods differ" in html and "Alice (Asia/Dhaka)" in html and "Dan (America/New_York)" in html


def test_overview_page_shows_kpis_and_employee_table():
    d = standard()
    d.manager()
    client, _ = login(d)
    html = client.get("/manager", params={"period": "this_week"}).text
    for label in ("Active Employees", "Currently Active", "Currently Idle", "Locked", "Unknown", "Offline",
                  "Scheduled Hours", "Tracked Hours", "Active Hours", "Idle Hours", "Unknown Hours", "Locked Hours",
                  "Overtime", "Attendance %", "Late Employees", "Absent Employees", "Data-Incomplete Employees"):
        assert label in html
    assert "Actual Work Hours" not in html and "productivity score" in html
    assert 'href="/manager/employees/alice"' in html


def test_data_quality_and_flags_are_surfaced():
    d = standard()
    d.manager()
    client, _ = login(d)
    html = client.get("/manager/employees/bob", params={"period": "custom", "from": "2026-10-05",
                                                        "to": "2026-10-05"}).text
    assert "Data incomplete" in html and "Insufficient" in html
    assert "not counted as absent" in html and "Some monitoring time unknown" in html


# ─── current status ────────────────────────────────────────────────────────


def _status(devices: list[Device], periods: list[Period]) -> CurrentStatus:
    latest = {}
    for p in sorted(periods, key=lambda p: p.ended_at):
        latest[p.device_id] = p
    return employee_status([dv for dv in devices if dv.status == "ACTIVE"], latest, NOW, 300, "e").status


def _dev(name: str, seconds_ago: int | None, status: str = "ACTIVE") -> Device:
    return Device(name, "e", status, None if seconds_ago is None else NOW - timedelta(seconds=seconds_ago))


def _per(device: str, status: str, ended_ago: int) -> Period:
    end = NOW - timedelta(seconds=ended_ago)
    return Period(str(uuid.uuid4()), "e", device, end - timedelta(minutes=5), end, 300, status)


@pytest.mark.parametrize("status", ["ACTIVE", "IDLE", "LOCKED", "UNKNOWN"])
def test_recent_period_on_an_online_device_gives_its_status(status):
    assert _status([_dev("d1", 20)], [_per("d1", status, 30)]) is CurrentStatus(status)


def test_online_without_a_recent_period():
    assert _status([_dev("d1", 20)], [_per("d1", "ACTIVE", 3600)]) is CurrentStatus.ONLINE_NO_ACTIVITY
    assert _status([_dev("d1", 20)], []) is CurrentStatus.ONLINE_NO_ACTIVITY


def test_device_not_seen_within_the_threshold_is_offline():
    assert _status([_dev("d1", 301)], [_per("d1", "ACTIVE", 400)]) is CurrentStatus.OFFLINE
    assert _status([_dev("d1", None)], []) is CurrentStatus.OFFLINE


def test_several_devices_use_a_deterministic_priority():
    assert _status([_dev("d1", 10), _dev("d2", 10)], [_per("d1", "IDLE", 5), _per("d2", "ACTIVE", 5)]) \
        is CurrentStatus.ACTIVE
    assert _status([_dev("d1", 10), _dev("d2", 10)], [_per("d1", "UNKNOWN", 5), _per("d2", "LOCKED", 5)]) \
        is CurrentStatus.LOCKED
    assert _status([_dev("d1", 10), _dev("d2", 900)], [_per("d1", "UNKNOWN", 5)]) is CurrentStatus.UNKNOWN


def test_disabled_devices_are_ignored_and_no_device_is_reported():
    assert _status([_dev("d1", 10, "DISABLED"), _dev("d2", 900)], [_per("d1", "ACTIVE", 5)]) \
        is CurrentStatus.OFFLINE
    assert _status([_dev("d1", 10, "DISABLED")], [_per("d1", "ACTIVE", 5)]) is CurrentStatus.NO_DEVICE
    assert _status([], []) is CurrentStatus.NO_DEVICE


def test_unknown_is_never_reported_as_idle():
    d = standard()
    d.repo.periods.append(Period("p-x", "bob", "bob-pc", NOW - timedelta(minutes=2), NOW - timedelta(seconds=10),
                                 110, "UNKNOWN"))
    statuses = d.service().current_status()
    assert statuses["bob"].status is CurrentStatus.UNKNOWN and statuses["bob"].label.startswith("Unknown")


def test_current_status_threshold_is_configurable():
    d = standard()
    d.repo.add_device(Device("alice-pc", "alice", "ACTIVE", NOW - timedelta(seconds=200)))
    assert d.service(online_threshold_seconds=300).current_status()["alice"].status is not CurrentStatus.OFFLINE
    assert d.service(online_threshold_seconds=120).current_status()["alice"].status is CurrentStatus.OFFLINE


# ─── overnight shift ───────────────────────────────────────────────────────


def test_overnight_employee_today_schedule_and_detail():
    d = standard()
    svc = d.service()
    row = next(r for r in svc.overview(svc.selection("today"))["rows"] if r["employee"].employee_id == "carol")
    assert row["schedule"].text == "20:00 – 04:00 (+1 day), 8 h expected"  # Thursday's shift, starting today
    detail = svc.employee_detail("carol", svc.selection("custom", "carol", MON, MON))
    (mon,) = detail["days"]
    stored = d.repo.summaries[("carol", MON)]
    assert mon.summary == stored and mon.summary.local_date == MON  # owned by the shift-start date
    assert stored.scheduled_end.astimezone(ZoneInfo(DHAKA)) == datetime(2026, 10, 6, 4, 0, tzinfo=ZoneInfo(DHAKA))
    assert (detail["totals"].overtime, detail["totals"].late_seconds) == (stored.overtime_seconds, stored.late_seconds)
    assert stored.overtime_seconds == 30 * 60 + 45 * 60
    d.manager()
    client, _ = login(d)
    html = client.get("/manager/employees/carol", params={"period": "custom", "from": "2026-10-05",
                                                          "to": "2026-10-05"}).text
    assert "2026-10-05 20:00" in html and "2026-10-06 04:00" in html and "1h 15m" in html


# ─── employee, attendance and applications pages ───────────────────────────


def test_employee_detail_sections_and_activity_limit():
    d = standard()
    for i in range(30):
        d.repo.periods.append(Period(f"p{i:02d}", "alice", "alice-pc", at(MON, "08:00") + timedelta(minutes=i),
                                     at(MON, "08:00") + timedelta(minutes=i, seconds=30), 30, "ACTIVE",
                                     app_name="Excluded Application", window_title="Excluded / Private",
                                     domain="Excluded / Private Site", privacy_excluded=True))
    d.manager()
    client, _ = login(d, d.client(DashboardSettings(activity_rows=10)))
    html = client.get("/manager/employees/alice", params={"period": "this_week"}).text
    for heading in ("Selected period summary", "Daily attendance", "Application usage", "Recent activity periods",
                    "Asia/Dhaka", "Data-Incomplete Days", "Detected Break / Idle"):
        assert heading in html
    assert "is not a productivity score" in html
    assert html.count("<td>0:00:30</td>") <= 10 and "showing the latest 10 only" in html
    assert "Excluded / Private Site" in html


def test_application_usage_is_sorted_by_active_hours():
    d = standard()
    svc = d.service()
    rows = svc.application_rows(svc.selection("this_week", "alice"), by_employee=False)
    assert [(r["app"], r["active"], r["usage_days"]) for r in rows] == [("Code.exe", 13 * H, 2), ("Chrome", 2 * H, 1)]
    every = svc.applications(svc.selection("this_week"))
    assert every["group"] == "app" and every["rows"][0]["app"] == "Code.exe"
    assert every["rows"][0]["unknown"] == 8 * H and every["rows"][0]["employee_count"] == 2
    split = svc.applications(svc.selection("this_week"), "employee")
    assert {(r["app"], r["employee"].employee_id) for r in split["rows"]} == {
        ("Code.exe", "alice"), ("Chrome", "alice"), ("Code.exe", "bob")}


def test_attendance_page_sorts_on_the_server():
    d = standard()
    svc = d.service()
    sel = svc.selection("custom", None, MON, THU)
    rows = svc.attendance(sel, "active", "desc")["rows"]
    actives = [r.summary.active_seconds for r in rows]
    assert actives == sorted(actives, reverse=True)
    assert svc.attendance(sel, "nonsense")["sort"] == "date"
    d.manager()
    client, _ = login(d)
    html = client.get("/manager/attendance", params={"period": "custom", "from": "2026-10-05", "to": "2026-10-08",
                                                     "sort": "employee", "dir": "asc"}).text
    assert html.index(">Alice</a>") < html.index(">Bob</a>") < html.index(">Carol</a>")
    assert 'aria-sort="ascending"' in html


def test_invalid_filters_show_a_clear_error():
    d = standard()
    d.manager()
    client, _ = login(d)
    for params in ({"period": "custom", "from": "2026-10-08", "to": "2026-10-01"}, {"period": "custom"},
                   {"period": "decade"}, {"period": "custom", "from": "08/10/2026", "to": "2026-10-09"}):
        assert client.get("/manager/attendance", params=params).status_code == 400
    assert client.get("/manager/employees/nobody").status_code == 404


# ─── schedule management ───────────────────────────────────────────────────


def _weekly(d: Dash, eid: str, dow: int) -> ScheduleRule:
    return next(r for r in d.repo.rules.values() if r.employee_id == eid and r.day_of_week == dow)


def test_schedule_changes_apply_from_today_and_are_audited():
    d = standard()
    svc = d.service()
    actor = ("MANAGER", "manager")
    with pytest.raises(ValueError, match="today"):
        svc.add_weekly(actor, "alice", [1], effective_from=WED, effective_to=None, hours=(time(8), time(16), 8 * H))
    with pytest.raises(ValueError, match="today"):
        svc.add_date(actor, "alice", WED, None)
    (new_id,) = svc.add_date(actor, "alice", date(2026, 10, 20), None)
    assert d.repo.rules[new_id].is_working_day is False
    # a running weekly rule changes from a date >= today: the old rule ends the day before
    monday = _weekly(d, "alice", 1)
    split_id = svc.update_rule(actor, monday.schedule_id, hours=(time(10), time(18), 8 * H),
                               from_date=date(2026, 10, 12))
    assert d.repo.rules[monday.schedule_id].effective_to == date(2026, 10, 11)
    assert d.repo.rules[split_id].effective_from == date(2026, 10, 12)
    assert d.repo.rules[split_id].start_time == time(10)
    with pytest.raises(ValueError, match="today"):
        svc.update_rule(actor, _weekly(d, "alice", 2).schedule_id, hours=None, from_date=MON)
    # a future rule changes in place and can be removed
    svc.update_rule(actor, split_id, hours=(time(11), time(19), 8 * H))
    assert d.repo.rules[split_id].start_time == time(11)
    svc.remove_rule(actor, split_id)
    assert split_id not in d.repo.rules
    # a running rule is stopped from a date, never deleted
    tuesday = _weekly(d, "alice", 2)
    svc.remove_rule(actor, tuesday.schedule_id, date(2026, 10, 13))
    assert d.repo.rules[tuesday.schedule_id].effective_to == date(2026, 10, 12)
    # past rules are read-only
    past = ScheduleRule(str(uuid.uuid4()), "alice", False, DHAKA, schedule_date=MON)
    d.rule(past)
    with pytest.raises(ValueError, match="past"):
        svc.remove_rule(actor, past.schedule_id)
    with pytest.raises(ValueError, match="past"):
        svc.update_rule(actor, past.schedule_id, hours=None)
    actions = [(r["action"], r["actor_id"]) for r in d.repo.audit_rows]
    assert ("work_schedule.create", "manager") in actions and ("work_schedule.delete", "manager") in actions
    delete = next(r for r in d.repo.audit_rows if r["action"] == "work_schedule.delete")
    assert delete["old_values"]["start_time"] == time(11) and delete["new_values"] is None


def test_schedule_page_add_weekly_through_the_form():
    d = standard()
    d.manager()
    client, _ = login(d)
    token = csrf(client)
    response = client.post("/manager/schedules/weekly", data={
        "csrf_token": token, "employee_id": "bob", "weekday": ["6", "7"], "start": "10:00", "end": "14:00",
        "expected_hours": "4", "effective_from": "2026-10-10", "return_employee": "bob"})
    assert response.status_code == 303 and response.headers["location"] == "/manager/schedules?employee=bob&done=created"
    new = [r for r in d.repo.rules.values() if r.employee_id == "bob" and r.effective_from == date(2026, 10, 10)]
    assert sorted(r.day_of_week for r in new) == [6, 7] and all(r.expected_work_seconds == 4 * H for r in new)
    assert [r["actor_type"] for r in d.repo.audit_rows if r["action"] == "work_schedule.create"] == ["MANAGER"] * 2
    response = client.post("/manager/schedules/weekly", data={
        "csrf_token": token, "employee_id": "bob", "weekday": ["1"], "start": "10:00", "end": "14:00",
        "expected_hours": "4", "effective_from": "2026-10-01"})
    assert response.status_code == 400 and "today (2026-10-08) or later" in response.text


def test_today_is_the_employees_local_date_for_schedule_rules():
    d = Dash(now=datetime(2026, 10, 7, 18, 30, tzinfo=UTC))  # Dhaka already on 8 Oct, New York on 7 Oct
    d.employee("alice", "Alice", DHAKA)
    d.employee("dan", "Dan", NY)
    d.build()
    svc = d.service()
    actor = ("MANAGER", "manager")
    svc.add_date(actor, "dan", WED, None)  # still "today" in New York
    with pytest.raises(ValueError, match="2026-10-08"):
        svc.add_date(actor, "alice", WED, None)  # already yesterday in Dhaka


def test_attendance_status_values_are_the_stored_ones():
    d = standard()
    svc = d.service()
    rows = svc.attendance(svc.selection("custom", "alice", MON, WED))["rows"]
    assert [r.summary.attendance_status for r in sorted(rows, key=lambda r: r.summary.local_date)] == [
        AttendanceStatus.PRESENT, AttendanceStatus.LATE, AttendanceStatus.ABSENT]
