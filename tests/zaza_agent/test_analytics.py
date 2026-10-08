"""Phase 8 — charts and deterministic, rule-based analysis (no AI).

Data comes from REAL Phase 5 summaries built by the attendance engine (the
Phase 7 test fixtures), so every chart and insight is checked against the
stored figures. No database, no Google, no network.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("jinja2")
pytest.importorskip("argon2")

from deskmate.zaza_server.attendance.models import ScheduleRule  # noqa: E402
from deskmate.zaza_server.dashboard.analysis import (  # noqa: E402
    ORDER,
    Analysis,
    previous_range,
    previous_selection,
)
from deskmate.zaza_server.dashboard.charts import render  # noqa: E402
from deskmate.zaza_server.dashboard.config import (  # noqa: E402
    DashboardSettings,
    dashboard_settings_from_env,
)
from deskmate.zaza_server.dashboard.models import AppUsage  # noqa: E402
from deskmate.zaza_server.dashboard.security import CSP  # noqa: E402

from .test_dashboard import (  # noqa: E402
    DHAKA,
    MON,
    NY,
    THU,
    TUE,
    WED,
    Dash,
    H,
    at,
    login,
    standard,
)

UTC = timezone.utc
EVIL_NAME = "<script>alert(1)</script>"
EVIL_APP = '"><img src=x onerror=alert(1)>'


def analysis(d: Dash, kind: str = "this_week", employee: str | None = None, frm=None, to=None, **settings):  # noqa: ANN001, ANN201
    svc = d.service(**settings)
    return Analysis(svc, svc.selection(kind, employee, frm, to))


def chart(a: Analysis, key: str):  # noqa: ANN201
    return next(c for c in a.charts([key]))


def kinds(a: Analysis) -> list[str]:
    return [i.kind for i in a.insights(limit=20)]


def message(a: Analysis, kind: str) -> str:
    return next(i.message for i in a.insights(limit=20) if i.kind == kind)


# ─── chart data ────────────────────────────────────────────────────────────


def test_active_hours_by_employee_is_sorted_by_active_hours():
    a = analysis(standard())
    c = chart(a, "active_by_employee")
    assert c.categories == ["Alice", "Carol", "Bob"]
    assert c.values == [[18.5], [9.25], [2.23]]
    stored = sum(s.active_seconds for (e, day), s in a.service.repo.summaries.items() if MON <= day <= THU)
    assert sum(v[0] for v in c.values) == pytest.approx(stored / 3600, abs=0.02)
    assert "not a productivity score" in c.note


def test_duplicate_display_names_are_disambiguated():
    d = Dash()
    for eid in ("emp-01", "emp-02"):
        d.employee(eid, "John Smith")
    d.period("emp-01", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    d.period("emp-02", "ACTIVE", at(MON, "09:00"), at(MON, "13:00"))
    a = analysis(d.build())
    assert chart(a, "active_by_employee").categories == ["John Smith (emp-01)", "John Smith (emp-02)"]
    assert chart(a, "attendance").categories == ["John Smith (emp-01)", "John Smith (emp-02)"]


def test_status_chart_keeps_unknown_and_locked_separate():
    a = analysis(standard())
    c = chart(a, "status_hours")
    assert [s.label for s in c.series] == ["Active Hours", "Idle Hours", "Unknown Hours", "Locked Hours"]
    rows = dict(zip(c.categories, c.values, strict=True))
    assert rows["Bob"] == [2.23, 0.0, 8.0, 0.0]     # the UNKNOWN Monday stays Unknown, never Idle
    assert rows["Carol"][3] == 0.5                    # LOCKED stays Locked
    assert rows["Alice"][1] == 8.0                    # Wednesday's IDLE


def test_daily_trend_team_totals_per_local_date():
    c = chart(analysis(standard()), "daily_trend")
    assert c.categories == ["Mon 05 Oct", "Tue 06 Oct", "Wed 07 Oct", "Thu 08 Oct"]  # nothing after today
    assert c.values == [[17.25], [7.5], [0.0], [5.23]]
    assert c.note == ""


def test_daily_trend_with_mixed_timezones_uses_each_employees_own_date():
    d = Dash(now=datetime(2026, 10, 7, 18, 30, tzinfo=UTC))  # Dhaka: Thu 8 Oct; New York: Wed 7 Oct
    d.employee("alice", "Alice", DHAKA)
    d.employee("dan", "Dan", NY)
    d.period("dan", "ACTIVE", at(WED, "09:00", NY), at(WED, "12:00", NY))
    a = analysis(d.build(), "today")
    c = chart(a, "daily_trend")
    assert c.categories == ["Wed 07 Oct", "Thu 08 Oct"] and c.values == [[3.0], [0.0]]
    assert "each employee's own local reporting date" in c.note


def test_weekly_trend_buckets_local_iso_weeks():
    a = analysis(standard())
    c = chart(a, "weekly_trend")
    assert c.categories[-1] == "Week of 05 Oct 2026" and len(c.categories) >= 2
    this_week = [s for (e, day), s in a.service.repo.summaries.items() if MON <= day <= MON + timedelta(days=6)]
    assert c.values[-1] == [round(sum(s.scheduled_seconds for s in this_week) / 3600, 2),
                            round(sum(s.active_seconds for s in this_week) / 3600, 2),
                            round(sum(s.idle_seconds for s in this_week) / 3600, 2),
                            round(sum(s.unknown_seconds for s in this_week) / 3600, 2)]
    assert "Monday–Sunday" in c.note


def test_application_chart_top_n_and_aggregation():
    d = standard()
    for i in range(12):
        d.repo.usage.append(AppUsage("bob", TUE, f"Tool {i:02d}", (i + 1) * 60, 0, 0))
    a = analysis(d, top_applications=3)
    c = chart(a, "applications")
    assert c.categories == ["Code.exe", "Chrome", "Tool 11"] and c.title.endswith("(top 3)")
    assert c.values[0] == [13.0]  # Alice 6 h + 7 h (+ Bob 0 h active): aggregated across employees
    assert "not a productivity score" in c.note


def test_attendance_chart_counts_stored_statuses():
    c = chart(analysis(standard()), "attendance")
    assert [s.label for s in c.series] == ["Late Days", "Early Leave Days", "Absent Days", "Data-Incomplete Days"]
    assert dict(zip(c.categories, c.values, strict=True)) == {
        "Alice": [1, 0, 1, 0], "Bob": [1, 0, 2, 1], "Carol": [0, 0, 2, 0]}  # Bob's UNKNOWN day: incomplete, not absent


def test_individual_employee_charts():
    a = analysis(standard(), employee="alice")
    keys = [c.key for c in a.charts()]
    assert "active_by_employee" not in keys and len(keys) == 5
    status = chart(a, "status_hours")
    assert status.categories == ["Selected period"] and status.values == [[18.5, 8.0, 0.0, 0.0]]
    assert chart(a, "attendance").values == [[1, 0, 1, 0]]


def test_empty_data_renders_empty_states():
    d = Dash()
    d.build()
    a = analysis(d)
    for c in a.charts():
        assert c.is_empty and render(c).shapes == []
    assert a.insights() == []
    d.manager()
    client, _ = login(d)
    html = client.get("/manager/analytics").text
    assert "No calculated data for this period." in html and "<rect" not in html


def test_all_zero_values_are_real_data_not_empty():
    d = Dash()
    d.employee("alice", "Alice")
    a = analysis(d.build(), "custom", None, MON, MON)  # Alice absent: 0 active, but a calculated day
    c = chart(a, "active_by_employee")
    assert not c.is_empty and c.all_zero


def test_inactive_employee_history_is_charted_when_selected():
    d = Dash()
    d.employee("fred", "Fred (left)", active=False)
    d.period("fred", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    a = analysis(d.build(), "custom", "fred", MON, MON)
    assert chart(a, "daily_trend").values == [[8.0]]
    assert analysis(d, "custom", None, MON, MON).charts() == [c for c in analysis(d, "custom", None, MON, MON)
                                                             .charts()]  # deterministic
    assert chart(analysis(d, "custom", None, MON, MON), "active_by_employee").is_empty  # not in "All Employees"


def test_overnight_shift_stays_on_its_start_date():
    a = analysis(standard(), "custom", "carol", MON, TUE)
    assert chart(a, "daily_trend").values == [[9.25], [0.0]]  # 19:30 Mon → 04:45 Tue belongs to Monday


def test_charts_never_recalculate_from_activity_periods():
    d = standard()
    before = Analysis(d.service(), d.service().selection("this_week")).as_json()
    d.repo.periods.clear()  # raw periods gone: charts depend only on the stored summaries
    after = Analysis(d.service(), d.service().selection("this_week")).as_json()
    assert before["charts"] == after["charts"] and before["insights"] == after["insights"]
    key = ("alice", MON)
    d.repo.summaries[key] = replace(d.repo.summaries[key], active_seconds=d.repo.summaries[key].active_seconds + H,
                                    tracked_seconds=d.repo.summaries[key].tracked_seconds + H)
    assert chart(analysis(d), "active_by_employee").values[0] == [19.5]  # follows the stored summary


def test_long_ranges_are_shown_per_week_with_unchanged_totals():
    d = standard()
    a = analysis(d, "custom", None, date(2026, 7, 1), THU)
    c = chart(a, "daily_trend")
    assert all(cat.startswith("Week of") for cat in c.categories) and "ISO week" in c.note
    total = sum(s.active_seconds for (e, day), s in d.repo.summaries.items() if date(2026, 7, 1) <= day <= THU)
    assert sum(v[0] for v in c.values) == pytest.approx(total / 3600, abs=0.05)


# ─── previous-period comparison ────────────────────────────────────────────


@pytest.mark.parametrize(("kind", "start", "end", "prev"), [
    ("today", THU, THU, (WED, WED)),
    ("yesterday", WED, WED, (TUE, TUE)),
    ("this_week", MON, MON + timedelta(days=6), (MON - timedelta(days=7), MON - timedelta(days=1))),
    ("last_week", MON - timedelta(days=7), MON - timedelta(days=1), (MON - timedelta(days=14), MON - timedelta(days=8))),
    ("this_month", date(2026, 10, 1), date(2026, 10, 31), (date(2026, 9, 1), date(2026, 9, 30))),
    ("last_month", date(2026, 9, 1), date(2026, 9, 30), (date(2026, 8, 1), date(2026, 8, 31))),
    ("custom", date(2026, 10, 8), date(2026, 10, 14), (date(2026, 10, 1), date(2026, 10, 7))),
    ("this_month", date(2026, 3, 1), date(2026, 3, 31), (date(2026, 2, 1), date(2026, 2, 28))),   # month lengths
    ("last_month", date(2028, 3, 1), date(2028, 3, 31), (date(2028, 2, 1), date(2028, 2, 29))),   # leap year
    ("this_month", date(2026, 1, 1), date(2026, 1, 31), (date(2025, 12, 1), date(2025, 12, 31))),  # year boundary
])
def test_previous_comparable_period(kind, start, end, prev):
    assert previous_range(kind, start, end) == prev


def test_previous_period_with_mixed_timezones_is_per_employee():
    d = Dash(now=datetime(2026, 10, 7, 18, 30, tzinfo=UTC))
    d.employee("alice", "Alice", DHAKA)
    d.employee("dan", "Dan", NY)
    svc = d.build().service()
    prev = previous_selection(svc.selection("today"))
    assert {r.employee.employee_id: (r.start, r.end) for r in prev.ranges} == {"alice": (WED, WED),
                                                                               "dan": (TUE, TUE)}


def _two_weeks(last_week_hours: float, this_week_hours: float) -> Dash:
    d = Dash(now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC))  # Monday 12 Oct 18:00 Dhaka
    d.employee("alice", "Alice")
    if last_week_hours:
        d.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "09:00") + timedelta(hours=last_week_hours))
    if this_week_hours:
        nxt = MON + timedelta(days=7)
        d.period("alice", "ACTIVE", at(nxt, "09:00"), at(nxt, "09:00") + timedelta(hours=this_week_hours))
    return d.build()


def test_active_hours_increase_and_decrease():
    up = message(analysis(_two_weeks(4, 6)), "ACTIVE_CHANGE")
    assert "increased by 50.0% (2h 00m)" in up and "previous equivalent period" in up
    down = message(analysis(_two_weeks(8, 6)), "ACTIVE_CHANGE")
    assert "decreased by 25.0% (2h 00m)" in down
    for text in (up, down):
        assert "because" not in text and "caused" not in text
    cmp = {c.metric: c for c in analysis(_two_weeks(4, 6)).comparisons()}
    assert (cmp["active_seconds"].current, cmp["active_seconds"].previous) == (6.0, 4.0)
    assert cmp["active_seconds"].relative == pytest.approx(0.5)
    assert set(cmp) == {"active_seconds", "idle_seconds", "scheduled_seconds", "overtime_seconds",
                        "attendance_percentage"}


def test_previous_zero_reports_no_percentage():
    a = analysis(_two_weeks(0, 6))
    text = message(a, "ACTIVE_CHANGE")
    assert text.startswith("Previous period (05 Oct 2026 – 11 Oct 2026) had no recorded Active Hours; current period "
                           "recorded 6h 00m.")
    assert "%" not in text and "inf" not in text.lower() and "∞" not in text
    cmp = next(c for c in a.comparisons() if c.metric == "active_seconds")
    assert cmp.relative is None and json.dumps(cmp.as_json())  # no Infinity in JSON


def test_both_periods_zero_says_nothing():
    a = analysis(_two_weeks(0, 0))
    assert "ACTIVE_CHANGE" not in kinds(a)
    active = next(c for c in a.comparisons() if c.metric == "active_seconds")
    assert (active.current, active.previous, active.relative) == (0.0, 0.0, None)


def test_no_comparison_without_previous_summaries():
    d = Dash()
    d.employee("alice", "Alice")
    d.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    d.build()
    for key in [k for k in d.repo.summaries if k[1] < MON]:
        del d.repo.summaries[key]  # nothing calculated before Monday
    a = analysis(d, "custom", None, MON, TUE)
    assert message(a, "ACTIVE_CHANGE") == ("No calculated summaries exist for the previous period "
                                           "(03 Oct 2026 – 04 Oct 2026), so no comparison is made.")


# ─── automatic insights ────────────────────────────────────────────────────


def test_team_summary_highest_and_lowest_with_neutral_wording():
    a = analysis(standard())
    summary = message(a, "ACTIVE_SUMMARY")
    assert "3 employee(s) have comparable calculated attendance data" in summary
    assert "Total Active Hours: 29h 59m" in summary and "Average Active Hours per employee: 10h 00m" in summary
    rng = message(a, "ACTIVE_RANGE")
    assert "Highest recorded Active Hours: Alice — 18h 30m" in rng
    assert "Lowest recorded Active Hours among employees with reportable data: Bob — 2h 14m" in rng
    assert "not a productivity score" in rng


def test_insufficient_data_employee_is_excluded_from_comparison():
    a = analysis(standard(), "today")  # Carol's night shift hasn't started: no comparable data yet
    rng = message(a, "ACTIVE_RANGE")
    assert "Carol" not in rng and "1 employee(s) without comparable data yet" in rng
    d = Dash()
    d.employee("alice", "Alice")
    d.employee("bob", "Bob")
    d.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    d.period("bob", "UNKNOWN", at(MON, "09:00"), at(MON, "17:00"))  # every working day DATA_INCOMPLETE
    a = analysis(d.build(), "custom", None, MON, MON)
    assert "ACTIVE_RANGE" not in kinds(a)  # only one comparable employee: no highest/lowest
    assert "1 employee(s) have comparable" in message(a, "ACTIVE_SUMMARY")


def test_active_share_of_scheduled_is_separate_from_attendance():
    a = analysis(standard())
    text = message(a, "ACTIVE_OF_SCHEDULED")
    assert text.startswith("Recorded Active Hours were 31.2% of scheduled time.")
    assert "not the Attendance %" in text and "interpreted cautiously" in text  # Bob's incomplete Monday


@pytest.mark.parametrize(("threshold", "expected"), [(40, False), (25, True)])
def test_high_idle_threshold(threshold, expected):
    a = analysis(standard(), employee="alice", high_idle_percent=threshold)  # Alice: 8 h idle of 26.5 h tracked
    assert ("HIGH_IDLE" in kinds(a)) is expected
    if expected:
        text = message(a, "HIGH_IDLE")
        assert "30.2% of tracked computer time for Alice" in text
        assert "legitimate non-computer work or breaks" in text


def test_late_absence_overtime_and_data_quality_insights():
    a = analysis(standard())
    assert message(a, "LATE") == "2 employee(s) had at least one late-start day: Alice (1 day), Bob (1 day)."
    assert message(a, "ABSENCE") == ("3 employee(s) had at least one absent day: Alice (1 day), Bob (2 days), "
                                     "Carol (2 days).")  # Bob's DATA_INCOMPLETE Monday is not absence
    assert "incomplete for 1 employee-day(s)" in message(a, "DATA_QUALITY")
    overtime = message(a, "OVERTIME")
    assert "Total recorded overtime activity: 1h 15m." in overtime and "not automatically a payroll" in overtime
    single = analysis(standard(), employee="bob")
    assert message(single, "LATE") == "Bob had 1 late-start day during the selected period."


def test_early_leave_uses_stored_status_and_respects_uncertainty():
    d = Dash()
    d.employee("alice", "Alice")
    d.employee("bob", "Bob")
    d.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "16:00"))   # left an hour early
    d.period("bob", "ACTIVE", at(MON, "09:00"), at(MON, "15:00"))
    d.period("bob", "UNKNOWN", at(MON, "15:00"), at(MON, "17:00"))   # uncertain end: Phase 5 charges nothing
    a = analysis(d.build(), "custom", None, MON, MON)
    assert message(a, "EARLY_LEAVE") == "1 employee(s) had at least one early-leave day: Alice (1 day)."


def test_missing_summaries_are_reported_not_counted_as_absence():
    d = standard()
    del d.repo.summaries[("alice", TUE)]
    a = analysis(d)
    assert "1 employee-day(s) in this period do not have a calculated summary yet" in message(a, "MISSING_SUMMARIES")
    d2 = Dash()
    d2.employee("alice", "Alice")
    d2.employee("newbie", "New Person")
    d2.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    d2.build()
    for key in [k for k in d2.repo.summaries if k[0] == "newbie"]:
        del d2.repo.summaries[key]
    text = message(analysis(d2, "custom", None, MON, MON), "MISSING_SUMMARIES")
    assert text == "Some employees do not yet have calculated summaries for this period: New Person."
    assert "ABSENCE" not in kinds(analysis(d2, "custom", None, MON, MON))


def test_top_application_insight_is_neutral():
    text = message(analysis(standard()), "TOP_APPLICATION")
    assert text.startswith("Code.exe had the most recorded Active Hours among applications during this period (13h 00m).")
    assert "productive" not in text.replace("not a productivity score", "")


def test_insight_limit_and_deterministic_order():
    a = analysis(standard(), max_insights=3)
    first = [i.kind for i in a.insights()]
    assert len(first) == 3 and first[0] == "DATA_QUALITY"
    full = kinds(analysis(standard()))
    assert full == sorted(full, key=lambda k: ORDER[k])
    assert len(analysis(standard()).insights()) <= 8
    assert [i.as_json() for i in analysis(standard()).insights()] == [i.as_json() for i in analysis(standard()).insights()]


def test_insights_are_structured():
    i = analysis(standard()).insights()[0]
    assert set(i.as_json()) == {"kind", "severity", "title", "message", "employee_id", "metric", "current_value",
                                "previous_value"}
    severities = {x.severity.value for x in analysis(standard()).insights(limit=20)}
    assert severities <= {"INFO", "NOTICE", "DATA_QUALITY"}


# ─── pages, JSON, wording, security ────────────────────────────────────────

FORBIDDEN = ("productivity score", "performance score", "employee score", "efficiency score", "top performer",
             "worst performer", "least productive", "most productive", "best employee", "worst employee",
             "actual work hours", "ai analysis", "artificial intelligence analysis")


def _visible(text: str) -> str:
    return (text.lower().replace("not a productivity score", "")
            .replace("not artificial intelligence", "").replace("(not artificial intelligence)", ""))


def test_no_productivity_scoring_anywhere():
    d = standard()
    d.manager()
    client, _ = login(d)
    bodies = [client.get(p, params={"period": "this_week"}).text for p in
              ("/manager", "/manager/analytics", "/manager/employees/alice")]
    bodies.append(json.dumps(client.get("/manager/api/analytics", params={"period": "this_week"}).json()))
    for body in bodies:
        visible = _visible(body)
        for phrase in FORBIDDEN:
            assert phrase not in visible, phrase
    assert "not a productivity score" in bodies[1]


def test_analytics_page_and_navigation():
    d = standard()
    d.manager()
    client, _ = login(d)
    html = client.get("/manager/analytics", params={"period": "this_week", "employee": ""}).text
    assert 'href="/manager/analytics" aria-current="page"' in html
    for title in ("Active Hours by Employee", "Active, Idle, Unknown and Locked Hours", "Daily Active Hours",
                  "Weekly Work Trend", "Application Usage (top 10)", "Attendance: Late, Early Leave, Absent"):
        assert title in html
    assert "Automatic Analysis" in html and "Compared with the previous period" in html
    assert html.count("<svg viewBox") == 6 and html.count("Show the numbers") == 6  # table for every chart
    assert 'role="img"' in html and "<desc id=" in html
    assert "Computer activity is a proxy for work, not proof of productivity." in html
    overview = client.get("/manager", params={"period": "this_week"}).text
    assert "Period Insights" in overview and 'href="/manager/analytics?period=this_week"' in overview
    assert overview.count("<svg viewBox") == 3
    employee = client.get("/manager/employees/alice", params={"period": "this_week"}).text
    assert employee.count("<svg viewBox") == 4 and "Daily attendance" in employee  # tables kept


def test_filters_are_bookmarkable():
    d = standard()
    d.manager()
    client, _ = login(d)
    html = client.get("/manager/analytics?period=custom&from=2026-10-05&to=2026-10-06&employee=alice").text
    assert 'value="custom" selected' in html and 'value="alice" selected' in html
    assert 'value="2026-10-05"' in html and "05 Oct 2026 – 06 Oct 2026" in html


def test_analytics_json_is_versioned_and_display_only():
    d = standard()
    user = d.manager()
    client, _ = login(d)
    body = client.get("/manager/api/analytics", params={"period": "this_week"}).json()
    assert body["version"] == 1 and set(body["charts"]) == {"active_by_employee", "status_hours", "daily_trend",
                                                            "weekly_trend", "applications", "attendance"}
    assert body["analysis"] == "deterministic rule-based analysis (not artificial intelligence)"
    text = json.dumps(body)
    assert not re.search(r"\b[0-9a-f]{64}\b", text)
    token = client.cookies.get("zaza_manager_session")
    for secret in (token, user.password_hash, "payload", "content_hash", "record_version", "session_token_hash",
                   "password", "bearer", "ZAZA_DB", "audit"):
        assert secret.lower() not in text.lower(), secret
    assert "active_seconds" not in json.dumps(body["charts"])  # charts use display units/labels only


def test_analytics_requires_authentication():
    client = standard().client()
    assert client.get("/manager/analytics").status_code == 303
    assert client.get("/manager/api/analytics").status_code == 401


def test_malicious_names_are_escaped_in_charts():
    d = Dash()
    d.employee("evil", EVIL_NAME)
    d.employee("ok", "Okay")
    d.period("evil", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    d.repo.usage.append(AppUsage("evil", MON, EVIL_APP, 8 * H, 0, 0))
    d.build()
    d.manager()
    client, _ = login(d)
    for path in ("/manager/analytics", "/manager", "/manager/employees/evil"):
        html = client.get(path, params={"period": "this_week"}).text
        assert EVIL_NAME not in html and EVIL_APP not in html and "<img src=x" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html or path == "/manager/employees/evil"
    analytics = client.get("/manager/analytics", params={"period": "this_week"}).text
    assert "&lt;img src=x onerror=alert(1)&gt;" in analytics  # the application name, as SVG text
    data = client.get("/manager/api/analytics", params={"period": "this_week"})
    assert data.headers["content-type"].startswith("application/json")
    assert EVIL_NAME in data.json()["charts"]["active_by_employee"]["categories"]  # plain JSON string data


def test_no_inline_scripts_external_resources_or_weaker_csp():
    d = standard()
    d.manager()
    client, _ = login(d)
    response = client.get("/manager/analytics", params={"period": "this_week"})
    html = response.text
    assert response.headers["content-security-policy"] == CSP
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts == ['<script src="/manager/static/dashboard.js" defer>']
    assert not re.search(r'(src|href)="(https?:)?//', html)
    assert " style=" not in html and "onload=" not in html and "onclick=" not in html
    static = Path(__file__).parents[2] / "deskmate/zaza_server/dashboard"
    for f in [*(static / "templates").glob("*.html"), *(static / "static").glob("*")]:
        content = f.read_text(encoding="utf-8")
        assert "cdn" not in content.lower() and "googleapis" not in content and "https://" not in content


def test_new_settings():
    s = dashboard_settings_from_env({"ZAZA_DASHBOARD_TOP_APPLICATIONS": "5", "ZAZA_ANALYSIS_HIGH_IDLE_PERCENT": "30",
                                     "ZAZA_ANALYSIS_MAX_INSIGHTS": "4"})
    assert (s.top_applications, s.high_idle_percent, s.max_insights) == (5, 30, 4)
    assert DashboardSettings() == replace(DashboardSettings(), top_applications=10, high_idle_percent=40,
                                          max_insights=8)
    from deskmate.zaza_server.config import ConfigError
    for env in ({"ZAZA_DASHBOARD_TOP_APPLICATIONS": "2"}, {"ZAZA_DASHBOARD_TOP_APPLICATIONS": "51"},
                {"ZAZA_ANALYSIS_HIGH_IDLE_PERCENT": "0"}, {"ZAZA_ANALYSIS_HIGH_IDLE_PERCENT": "101"},
                {"ZAZA_ANALYSIS_MAX_INSIGHTS": "0"}, {"ZAZA_ANALYSIS_MAX_INSIGHTS": "21"}):
        with pytest.raises(ConfigError):
            dashboard_settings_from_env(env)


def test_date_override_with_short_shift_counts_in_scheduled_hours():
    d = Dash()
    d.employee("alice", "Alice")
    d.rule(ScheduleRule(str(uuid.uuid4()), "alice", True, DHAKA, schedule_date=TUE, start_time=time(9),
                        end_time=time(11), expected_work_seconds=2 * H))
    a = analysis(d.build(), "custom", "alice", MON, TUE)
    weekly = chart(a, "weekly_trend")
    assert weekly.values[-1][0] == 10.0  # 8 h Monday + 2 h override Tuesday (stored scheduled_seconds)
