"""Phase 6 — one-way Google Sheets export (no Google account, no database).

The exporter runs against FakeSheetsClient (an in-memory spreadsheet that
enforces Google's grid limits) and an in-memory report source filled with
REAL Phase 5 summaries, calculated by the attendance engine from synced
activity periods. The Google adapter itself is exercised offline with the
official library's HttpMockSequence. PostgreSQL behaviour is in
test_sheets_postgres.py (opt-in).
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from deskmate.zaza_server.attendance.models import DeviceInput, PeriodInput, ScheduleRule
from deskmate.zaza_server.attendance.store import InMemoryAttendanceStore
from deskmate.zaza_server.attendance.summary_service import SummaryService
from deskmate.zaza_server.sheets import client as client_mod
from deskmate.zaza_server.sheets.client import (
    FakeSheetsClient,
    GoogleSheetsClient,
    SheetsUnavailable,
    a1,
    column_letter,
    parse_a1,
)
from deskmate.zaza_server.sheets.config import (
    Scrubber,
    SheetsConfigError,
    SheetsSettings,
    load_service_account_key,
    mask_id,
    sheets_settings_from_env,
)
from deskmate.zaza_server.sheets.exporter import SheetsExporter
from deskmate.zaza_server.sheets.formatter import NUMBER_FORMATS
from deskmate.zaza_server.sheets.models import (
    ACTIVITY_SPEC,
    DAILY_SPEC,
    DATA_SPECS,
    MONTHLY_SPEC,
    REQUIRED_TABS,
    WEEKLY_SPEC,
    ActivityRow,
    EmployeeRow,
    Kind,
)
from deskmate.zaza_server.sheets.queries import ACTIVITY_SQL, InMemoryReportSource

UTC = timezone.utc
H = 3600
DHAKA = "Asia/Dhaka"
NY = "America/New_York"
MON = date(2026, 10, 5)
TUE, WED, THU = (MON + timedelta(days=i) for i in range(1, 4))
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)  # Thursday 12:00 in Dhaka
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-xyz9"
EPOCH = datetime(1899, 12, 30)


def at(day: date, hhmm: str, tz: str = DHAKA) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm)).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def serial(naive: datetime) -> float:
    return (naive - EPOCH).total_seconds() / 86400


def settings(**kw) -> SheetsSettings:
    return SheetsSettings(spreadsheet_id=SHEET_ID, service_account_file=Path("unused.json"), **kw)


class Office:
    """Employees with schedules and synced activity; summaries come from the
    real Phase 5 engine, activity rows from the same periods."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.store = InMemoryAttendanceStore()
        self.employees: list[EmployeeRow] = []
        self.activity: list[ActivityRow] = []

    def employee(self, eid: str, name: str, tz: str = DHAKA, *, start="09:00", end="17:00",
                 active: bool = True) -> None:
        self.employees.append(EmployeeRow(eid, name, tz, active))
        self.store.add_employee(eid, tz, is_active=active)
        self.store.add_device(eid, DeviceInput(f"{eid}-pc", "ACTIVE", datetime(2027, 1, 1, tzinfo=UTC)))
        for dow in range(1, 8):
            working = dow <= 5
            self.store.add_rule(ScheduleRule(
                str(uuid.uuid4()), eid, working, tz, day_of_week=dow, effective_from=date(2026, 1, 1),
                start_time=time.fromisoformat(start) if working else None,
                end_time=time.fromisoformat(end) if working else None,
                expected_work_seconds=8 * H if working else None))

    def period(self, eid: str, status: str, start: datetime, end: datetime, *, app="Code.exe",
               title="main.py - project", domain=None, privacy=False, is_open=False, detail=None,
               end_reason="WINDOW_CHANGE") -> ActivityRow:
        pid = str(uuid.uuid4())
        self.store.add_period(eid, PeriodInput(pid, f"{eid}-pc", start, end, status, is_open))
        row = ActivityRow(pid, eid, start, end, (end - start).total_seconds(), is_open, status, detail, app,
                          title, domain, privacy, end_reason)
        self.activity.append(row)
        return row

    def source(self) -> InMemoryReportSource:
        service = SummaryService(self.store, clock=lambda: self.now)
        service.recalculate(date(2026, 9, 1), self.now.date())
        daily = [s for _, s in self.store.daily.values()]
        weekly = [p for (_, kind, _), (_, p) in self.store.period_summaries.items() if kind == "WEEK"]
        monthly = [p for (_, kind, _), (_, p) in self.store.period_summaries.items() if kind == "MONTH"]
        return InMemoryReportSource(self.employees, self.activity, daily, weekly, monthly,
                                    summaries_updated_at=self.now - timedelta(minutes=5))


def standard_office() -> Office:
    o = Office()
    o.employee("alice", "Alice")
    o.employee("bob", "Bob")
    o.employee("carol", "Carol", start="20:00", end="04:00")  # night shift
    o.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "17:00"))
    o.period("alice", "ACTIVE", at(TUE, "09:30"), at(TUE, "17:00"))           # 30 min late
    o.period("alice", "IDLE", at(WED, "09:00"), at(WED, "17:00"))             # PC on, nobody there: absent
    o.period("alice", "ACTIVE", at(THU, "09:00"), at(THU, "12:00"))           # today, in progress
    o.period("bob", "UNKNOWN", at(MON, "09:00"), at(MON, "17:00"), detail="MONITORING_UNAVAILABLE")
    o.period("bob", "ACTIVE", at(THU, "09:45"), at(THU, "12:00"))             # today: late
    o.period("carol", "ACTIVE", at(MON, "20:00"), at(TUE, "04:00"))           # overnight shift
    o.period("carol", "LOCKED", at(TUE, "04:00"), at(TUE, "04:30"))
    return o


def run(source=None, client: FakeSheetsClient | None = None, **kw):  # noqa: ANN001
    client = client or FakeSheetsClient()
    exporter = SheetsExporter(client, settings(**kw), source=source or standard_office().source(),
                              clock=lambda: NOW)
    return exporter, client


def by_header(client: FakeSheetsClient, tab: str) -> list[dict]:
    header, *rows = client.values(tab)
    return [dict(zip(header, r, strict=True)) for r in rows]


def dash(client: FakeSheetsClient) -> dict[str, list]:
    return {row[0]: row[1:] for row in client.values("Dashboard") if row and row[0]}


# ─── tabs ──────────────────────────────────────────────────────────────────


def test_init_creates_the_five_required_tabs_and_leaves_other_tabs_alone():
    client = FakeSheetsClient(tabs=("Sheet1", "Manager notes"))
    client.tabs["Manager notes"].cells[(0, 0)] = "keep me"
    result = SheetsExporter(client, settings()).init()
    assert result.created == REQUIRED_TABS and len(REQUIRED_TABS) == 5
    assert set(client.tabs) == {"Sheet1", "Manager notes", *REQUIRED_TABS}
    assert client.values("Manager notes") == [["keep me"]]
    for spec in DATA_SPECS:
        assert client.values(spec.title) == [spec.headers]
        assert client.tabs[spec.title].frozen_rows == 1
    assert dash(client)["Last successful refresh"][0] == "Never"


def test_missing_tabs_are_created_and_existing_tabs_reused():
    client = FakeSheetsClient(tabs=("Daily Summary", "Dashboard"))
    ids = {t: client.tabs[t].sheet_id for t in ("Daily Summary", "Dashboard")}
    exporter, _ = run(client=client)
    result = exporter.init()
    assert result.created == ("Activity Log", "Weekly Summary", "Monthly Summary")
    assert result.existing == ("Daily Summary", "Dashboard")
    assert {t: client.tabs[t].sheet_id for t in ids} == ids  # same sheets, not recreated
    exporter.sync()
    adds = [r for m, reqs in client.calls if m == "batch_update" for r in reqs if "addSheet" in r]
    assert len(adds) == 3  # nothing re-added on sync


def test_sync_creates_missing_tabs_itself():
    exporter, client = run(client=FakeSheetsClient(tabs=()))
    exporter.sync()
    assert set(REQUIRED_TABS) <= set(client.tabs)


def test_headers_formatting_widths_and_number_formats():
    exporter, client = run()
    exporter.sync()
    daily = client.tabs["Daily Summary"]
    header_fmt = next(f for f in daily.formats if f["range"].get("endRowIndex") == 1)
    assert header_fmt["cell"]["userEnteredFormat"]["textFormat"]["bold"] is True
    assert daily.frozen_rows == 1 and daily.widths[0] == 150
    formats = {f["range"]["startColumnIndex"]: f["cell"]["userEnteredFormat"]["numberFormat"]
               for f in daily.formats if f["range"]["startRowIndex"] == 1}
    col = DAILY_SPEC.headers.index
    assert formats[col("Date")]["pattern"] == "yyyy-mm-dd"
    assert formats[col("Scheduled Start")]["pattern"] == "yyyy-mm-dd hh:mm"
    assert formats[col("Active Hours")]["pattern"] == "[h]:mm"
    assert formats[col("Attendance %")] == {"type": "PERCENT", "pattern": "0.00%"}
    act = {f["range"]["startColumnIndex"]: f["cell"]["userEnteredFormat"]["numberFormat"]
           for f in client.tabs["Activity Log"].formats if f["range"]["startRowIndex"] == 1}
    assert act[ACTIVITY_SPEC.headers.index("Time")]["pattern"] == "hh:mm:ss"
    assert act[ACTIVITY_SPEC.headers.index("Active Duration")]["pattern"] == "[h]:mm:ss"


def test_required_column_names():
    assert "Active Hours" in DAILY_SPEC.headers and "Actual Work Hours" not in DAILY_SPEC.headers
    assert "Detected Break / Idle" in DAILY_SPEC.headers
    assert not any("Break Taken" in h for spec in DATA_SPECS for h in spec.headers)
    for needed in ("Scheduled Start", "Scheduled End", "Late Minutes", "Early Leave Minutes", "Overtime",
                   "Attendance Status", "Attendance %", "Data Quality", "Notes / Quality Flags"):
        assert needed in DAILY_SPEC.headers
    assert "Average Active Hours / Worked Day" in WEEKLY_SPEC.headers
    assert MONTHLY_SPEC.headers[:5] == ["Employee", "Employee ID", "Month", "Days Scheduled", "Days Worked"]


# ─── idempotency, ordering, stale rows ─────────────────────────────────────


def test_rows_are_sorted_deterministically_whatever_the_input_order():
    source = standard_office().source()
    _, first = run(source)
    SheetsExporter(first, settings(), source=source, clock=lambda: NOW).sync()
    rng = random.Random(7)
    for attr in ("activity", "daily", "weekly", "monthly", "employees"):
        rng.shuffle(getattr(source, attr))
    second = FakeSheetsClient()
    SheetsExporter(second, settings(), source=source, clock=lambda: NOW).sync()
    for tab in REQUIRED_TABS:
        assert first.values(tab) == second.values(tab)
    log = by_header(first, "Activity Log")
    stamps = [r["Timestamp"] for r in log]
    assert stamps == sorted(stamps, reverse=True)  # newest first
    daily = by_header(first, "Daily Summary")
    keys = [(-r["Date"], r["Employee"]) for r in daily]
    assert keys == sorted(keys)  # newest date first, then employee name


def test_repeated_sync_produces_no_duplicates():
    exporter, client = run()
    exporter.sync()
    snapshot = {t: client.values(t) for t in REQUIRED_TABS}
    exporter.sync()
    exporter.sync()
    assert {t: client.values(t) for t in REQUIRED_TABS} == snapshot
    log = by_header(client, "Activity Log")
    assert len(log) == len(exporter.source.activity) == 8


def test_one_row_per_database_row():
    exporter, client = run()
    exporter.sync()
    src = exporter.source
    assert len(by_header(client, "Daily Summary")) == len(src.daily)
    assert len(by_header(client, "Weekly Summary")) == len(src.weekly)
    assert len(by_header(client, "Monthly Summary")) == len(src.monthly)
    keys = [(r["Employee ID"], r["Date"]) for r in by_header(client, "Daily Summary")]
    assert len(keys) == len(set(keys))


def test_stale_trailing_rows_are_cleared_after_the_replacement():
    exporter, client = run()
    exporter.sync()
    assert len(by_header(client, "Activity Log")) == 8
    client.tabs["Activity Log"].cells[(0, 30)] = "old extra column"  # outside the managed columns
    exporter.source.activity = exporter.source.activity[:2]
    mark = len(client.calls)
    exporter.sync()
    assert len(by_header(client, "Activity Log")) == 2
    assert len(client.values("Activity Log")) == 3  # header + 2: nothing left below
    assert (0, 30) not in client.tabs["Activity Log"].cells
    ops = [m for m, arg in client.calls[mark:] if m in ("write_values", "clear_values") and "Activity" in str(arg)]
    assert ops == ["write_values", "clear_values", "clear_values"]  # replace first, then clear rows / columns


def test_large_tabs_are_written_in_chunks_with_the_same_result():
    exporter, client = run()
    exporter.chunk_rows = 3
    exporter.sync()
    chunked = client.values("Activity Log")
    activity_writes = [a for m, a in client.calls if m == "write_values" and a.startswith("'Activity Log'")]
    assert activity_writes == ["'Activity Log'!A1", "'Activity Log'!A4", "'Activity Log'!A7"]
    _, whole = run(exporter.source)
    SheetsExporter(whole, settings(), source=exporter.source, clock=lambda: NOW).sync()
    assert chunked == whole.values("Activity Log")


def test_grid_is_grown_before_writing_beyond_it():
    client = FakeSheetsClient(tabs=())
    exporter, _ = run(client=client)
    exporter.init()
    client.tabs["Activity Log"].row_count = 3  # smaller than the data
    exporter.sync()
    assert len(by_header(client, "Activity Log")) == 8 and client.tabs["Activity Log"].row_count >= 9


# ─── activity log ──────────────────────────────────────────────────────────


def test_activity_log_is_built_from_activity_periods_not_raw_events():
    assert "FROM activity_periods" in ACTIVITY_SQL and "activity_events" not in ACTIVITY_SQL
    exporter, client = run()
    exporter.sync()
    types = {r["Event / Period Type"] for r in by_header(client, "Activity Log")}
    assert types == {"Active period", "Idle period", "Unknown period", "Locked period"}
    assert not any("LOGIN" in t.upper() or "LOGOUT" in t.upper() for t in types)


@pytest.mark.parametrize(("status", "column"), [("ACTIVE", "Active Duration"), ("IDLE", "Idle Duration"),
                                                 ("UNKNOWN", "Unknown Duration"), ("LOCKED", "Locked Duration")])
def test_each_status_fills_only_its_own_duration_column(status, column):
    o = Office()
    o.employee("alice", "Alice")
    o.period("alice", status, at(MON, "10:00"), at(MON, "10:12:30"))
    _, client = run(o.source())
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    (row,) = by_header(client, "Activity Log")
    durations = {c: row[c] for c in ("Active Duration", "Idle Duration", "Unknown Duration", "Locked Duration")}
    assert durations.pop(column) == pytest.approx(750 / 86400)  # 12:30 shown as 0:12:30
    assert set(durations.values()) == {""}  # never duplicated into another column
    assert row["Status"] == status.capitalize()


def test_privacy_excluded_periods_stay_redacted_exactly_as_stored():
    o = Office()
    o.employee("alice", "Alice")
    o.period("alice", "ACTIVE", at(MON, "10:00"), at(MON, "11:00"), app="Excluded Application",
             title="Excluded / Private", domain="Excluded / Private Site", privacy=True)
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    (row,) = by_header(client, "Activity Log")
    assert (row["Application"], row["Window / Activity"], row["Domain"]) == (
        "Excluded Application", "Excluded / Private", "Excluded / Private Site")
    assert "Privacy-excluded" in row["Notes"]


def test_formula_like_titles_are_written_as_text():
    o = Office()
    o.employee("alice", "Alice")
    o.period("alice", "ACTIVE", at(MON, "10:00"), at(MON, "11:00"), title='=IMPORTXML("http://x","//a")')
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    assert by_header(client, "Activity Log")[0]["Window / Activity"] == '=IMPORTXML("http://x","//a")'


def test_activity_timestamps_are_shown_in_the_employee_timezone():
    o = Office()
    o.employee("alice", "Alice", DHAKA)
    o.employee("dan", "Dan", NY)
    moment = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
    o.period("alice", "ACTIVE", moment, moment + timedelta(hours=1))
    o.period("dan", "ACTIVE", moment, moment + timedelta(hours=1))
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    rows = {r["Employee ID"]: r for r in by_header(client, "Activity Log")}
    assert rows["alice"]["Timestamp"] == pytest.approx(serial(datetime(2026, 10, 5, 9, 0)))  # UTC+6
    assert rows["dan"]["Timestamp"] == pytest.approx(serial(datetime(2026, 10, 4, 23, 0)))   # EDT, previous date
    assert rows["dan"]["Date"] == (date(2026, 10, 4) - EPOCH.date()).days
    assert rows["alice"]["Time"] == pytest.approx(9 / 24) and rows["dan"]["Time Zone"] == NY


def test_activity_log_window_is_configurable_per_employee_local_dates():
    o = Office()
    o.employee("alice", "Alice", DHAKA)
    o.employee("dan", "Dan", NY)
    for days_ago in range(0, 40):
        day = THU - timedelta(days=days_ago)
        o.period("alice", "ACTIVE", at(day, "10:00"), at(day, "10:30"))
        if at(day, "10:00", NY) < NOW:  # no data from the future
            o.period("dan", "ACTIVE", at(day, "10:00", NY), at(day, "10:30", NY))
    source = o.source()
    for days, alice, dan in ((1, 1, 0), (7, 7, 6), (30, 30, 29)):
        # NOW = Thu 12:00 Dhaka = Thu 02:00 New York: Dan's Thursday 10:00 hasn't happened yet
        client = FakeSheetsClient()
        SheetsExporter(client, settings(activity_days=days), source=source, clock=lambda: NOW).sync()
        ids = [r["Employee ID"] for r in by_header(client, "Activity Log")]
        assert (ids.count("alice"), ids.count("dan")) == (alice, dan), days
    assert "30 day(s)" in dash(client)["Activity Log shows"][0]
    assert len(source.activity) == 79  # the source (PostgreSQL) keeps everything


def test_open_and_interrupted_periods_are_annotated():
    o = Office()
    o.employee("alice", "Alice")
    o.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "10:00"), end_reason="AGENT_INTERRUPTED")
    o.period("alice", "ACTIVE", at(THU, "09:00"), at(THU, "11:00"), is_open=True)
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    notes = [r["Notes"] for r in by_header(client, "Activity Log")]
    assert notes == ["Still in progress at last sync", "Agent stopped unexpectedly"]


# ─── summary tabs ──────────────────────────────────────────────────────────


def test_daily_summary_mapping():
    exporter, client = run()
    exporter.sync()
    rows = {(r["Employee ID"], r["Date"]): r for r in by_header(client, "Daily Summary")}
    tue = rows[("alice", (TUE - EPOCH.date()).days)]
    stored = next(s for s in exporter.source.daily if s.employee_id == "alice" and s.local_date == TUE)
    assert tue["Employee"] == "Alice" and tue["Attendance Status"] == "Late"
    assert tue["Late Minutes"] == 30.0 and tue["Early Leave Minutes"] == 0.0
    assert tue["Scheduled Hours"] == pytest.approx(8 / 24)
    assert tue["Active Hours"] == pytest.approx(stored.active_seconds / 86400)
    assert tue["Tracked Hours"] == pytest.approx(stored.tracked_seconds / 86400)
    assert tue["Scheduled Start"] == pytest.approx(serial(datetime(2026, 10, 6, 9, 0)))
    assert tue["First Activity"] == pytest.approx(serial(datetime(2026, 10, 6, 9, 30)))
    assert tue["Last Activity"] == pytest.approx(serial(datetime(2026, 10, 6, 17, 0)))
    assert tue["Attendance %"] == pytest.approx(stored.attendance_percentage / 100)
    assert tue["Data Quality"] == "Complete" and tue["Time Zone"] == DHAKA
    wed = rows[("alice", (WED - EPOCH.date()).days)]
    assert wed["Attendance Status"] == "Absent" and wed["Attendance %"] == 0.0
    assert wed["Detected Break / Idle"] == pytest.approx(8 / 24)  # the IDLE run, labelled as detected only
    sat = rows[("alice", (MON - timedelta(days=2) - EPOCH.date()).days)]
    assert sat["Attendance Status"] == "Day off" and sat["Scheduled Start"] == "" and sat["Attendance %"] == ""


def test_attendance_percentage_is_a_fraction_formatted_as_percent():
    o = Office()
    o.employee("alice", "Alice")
    o.period("alice", "ACTIVE", at(MON, "09:00"), at(MON, "16:00"))  # 7 of 8 h
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    mon = next(r for r in by_header(client, "Daily Summary") if r["Date"] == (MON - EPOCH.date()).days)
    assert mon["Attendance %"] == pytest.approx(0.875)  # displayed as 87.50%
    assert NUMBER_FORMATS[Kind.PERCENT] == {"type": "PERCENT", "pattern": "0.00%"}


def test_overnight_shift_is_shown_with_its_next_day_end():
    exporter, client = run()
    exporter.sync()
    mon = next(r for r in by_header(client, "Daily Summary")
               if r["Employee ID"] == "carol" and r["Date"] == (MON - EPOCH.date()).days)
    assert mon["Scheduled Start"] == pytest.approx(serial(datetime(2026, 10, 5, 20, 0)))
    assert mon["Scheduled End"] == pytest.approx(serial(datetime(2026, 10, 6, 4, 0)))  # Tuesday 04:00
    assert mon["Active Hours"] == pytest.approx(8 / 24) and mon["Attendance Status"] == "Present"
    assert mon["Last Activity"] == pytest.approx(serial(datetime(2026, 10, 6, 4, 0)))


def test_data_incomplete_is_shown_as_such_not_as_absent():
    exporter, client = run()
    exporter.sync()
    mon = next(r for r in by_header(client, "Daily Summary")
               if r["Employee ID"] == "bob" and r["Date"] == (MON - EPOCH.date()).days)
    assert mon["Attendance Status"] == "Data incomplete"
    assert mon["Attendance %"] == "" and mon["Data Quality"] == "Insufficient"
    assert "not counted as absent" in mon["Notes / Quality Flags"]
    assert "Some monitoring time unknown" in mon["Notes / Quality Flags"]
    assert mon["Unknown Hours"] == pytest.approx(8 / 24) and mon["Idle Hours"] == 0


def test_weekly_summary_mapping():
    exporter, client = run()
    exporter.sync()
    week = next(r for r in by_header(client, "Weekly Summary")
                if r["Employee ID"] == "alice" and r["Week Start"] == (MON - EPOCH.date()).days)
    stored = next(p for p in exporter.source.weekly if p.employee_id == "alice" and p.period_start == MON)
    assert week["Week End"] == (MON + timedelta(days=6) - EPOCH.date()).days
    assert (week["Working Days"], week["Days Worked"]) == (stored.working_days, stored.days_worked)
    assert (week["Absent Days"], week["Late Days"], week["Incomplete Days"]) == (1, 1, 0)
    assert week["Active Hours"] == pytest.approx(stored.active_seconds / 86400)
    assert week["Average Active Hours / Worked Day"] == pytest.approx(
        stored.average_active_seconds_per_worked_day / 86400)
    assert week["Attendance %"] == pytest.approx(stored.attendance_percentage / 100)
    assert "Provisional" in week["Notes"]


def test_monthly_summary_mapping():
    exporter, client = run()
    exporter.sync()
    month = next(r for r in by_header(client, "Monthly Summary")
                 if r["Employee ID"] == "bob" and r["Month"] == (date(2026, 10, 1) - EPOCH.date()).days)
    stored = next(p for p in exporter.source.monthly
                  if p.employee_id == "bob" and p.period_start == date(2026, 10, 1))
    assert month["Days Scheduled"] == stored.working_days and month["Incomplete Days"] == 1
    assert month["Late Days"] == stored.late_days
    assert month["Unknown Hours"] == pytest.approx(stored.unknown_seconds / 86400)
    assert month["Data Quality"] == stored.data_quality.value.capitalize()


def test_summary_window_is_display_only_and_configurable():
    source = standard_office().source()
    client = FakeSheetsClient()
    SheetsExporter(client, settings(summary_months=1), source=source, clock=lambda: NOW).sync()
    dates = {r["Date"] for r in by_header(client, "Daily Summary")}
    assert min(dates) == (date(2026, 10, 1) - EPOCH.date()).days  # September not shown
    weeks = {r["Week Start"] for r in by_header(client, "Weekly Summary")}
    assert (date(2026, 9, 28) - EPOCH.date()).days in weeks  # the week overlapping October is kept
    client2 = FakeSheetsClient()
    SheetsExporter(client2, settings(summary_months=0), source=source, clock=lambda: NOW).sync()
    assert len(by_header(client2, "Daily Summary")) == len(source.daily)  # all history


# ─── dashboard ─────────────────────────────────────────────────────────────


def test_dashboard_today_values():
    exporter, client = run()
    exporter.sync()
    d = dash(client)
    today = [s for s in exporter.source.daily if s.local_date == THU]
    assert d["Reporting date"][0] == (THU - EPOCH.date()).days
    assert d["Active employees"][0] == 3
    assert d["Active Hours"][0] == pytest.approx((3 + 2.25) * H / 86400)  # Alice 09-12, Bob 09:45-12
    assert d["Scheduled Hours"][0] == pytest.approx(sum(s.scheduled_seconds for s in today) / 86400)
    assert (d["Late employees"][0], d["Absent employees"][0], d["Data-incomplete employees"][0]) == (1, 0, 0)
    assert d["Late (names)"][0] == "Bob"
    credit = sum(s.attendance_credit_seconds for s in today)
    basis = sum(s.attendance_basis_seconds for s in today)
    assert d["Attendance %"][0] == (pytest.approx(credit / basis, abs=1e-6) if basis else "")
    by_employee = {r[0]: r for r in client.values("Dashboard")[-3:]}
    assert by_employee["Bob"][1] == "Late" and by_employee["Carol"][1].startswith("Pending")


def test_dashboard_this_week_values():
    exporter, client = run()
    exporter.sync()
    d = dash(client)
    weeks = [p for p in exporter.source.weekly if p.period_start == MON]
    assert len(weeks) == 3
    assert d["Period"][1] == "2026-10-05 – 2026-10-11"
    assert d["Active Hours"][1] == pytest.approx(sum(p.active_seconds for p in weeks) / 86400)
    assert d["Idle Hours"][1] == pytest.approx(sum(p.idle_seconds for p in weeks) / 86400)
    assert d["Unknown Hours"][1] == pytest.approx(8 / 24)
    assert d["Attendance %"][1] == pytest.approx(
        sum(p.attendance_credit_seconds for p in weeks) / sum(p.attendance_basis_seconds for p in weeks), abs=1e-6)
    assert d["Late employees"][1] == 2 and d["Late (names)"][1] == "Alice, Bob"
    assert d["Absent (names)"][1] == "Alice, Bob, Carol" and d["Data incomplete (names)"][1] == "Bob"


def test_dashboard_this_month_values():
    exporter, client = run()
    exporter.sync()
    d = dash(client)
    months = [p for p in exporter.source.monthly if p.period_start == date(2026, 10, 1)]
    assert d["Period"][2] == "October 2026"
    assert d["Scheduled Hours"][2] == pytest.approx(sum(p.scheduled_seconds for p in months) / 86400)
    assert d["Locked Hours"][2] == pytest.approx(sum(p.locked_seconds for p in months) / 86400)
    assert d["Overtime"][2] == pytest.approx(sum(p.overtime_seconds for p in months) / 86400)
    assert d["Attendance %"][2] == pytest.approx(
        sum(p.attendance_credit_seconds for p in months) / sum(p.attendance_basis_seconds for p in months), abs=1e-6)
    assert d["Employees without a calculated summary"] == [0, 0, 0, ""]


def test_dashboard_uses_each_employee_local_today_and_documents_the_zone():
    o = Office()
    o.employee("alice", "Alice", DHAKA)
    o.employee("dan", "Dan", NY)
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    d = dash(client)
    assert d["Report time zone"][0].startswith("UTC")  # mixed zones -> UTC for team-level times
    assert d["Last successful refresh"][0] == "2026-10-08 06:00:00 (UTC)"
    assert "each employee's own local date" in d["How to read this"][0]
    client2 = FakeSheetsClient()
    SheetsExporter(client2, settings(report_timezone=DHAKA), source=o.source(), clock=lambda: NOW).sync()
    assert dash(client2)["Last successful refresh"][0] == "2026-10-08 12:00:00 (Asia/Dhaka)"


def test_inactive_employees_keep_history_but_leave_the_dashboard():
    o = standard_office()
    o.employees[1] = replace(o.employees[1], is_active=False)  # Bob left
    _, client = run()
    SheetsExporter(client, settings(), source=o.source(), clock=lambda: NOW).sync()
    assert dash(client)["Active employees"][0] == 2
    assert any(r["Employee ID"] == "bob" for r in by_header(client, "Daily Summary"))


# ─── failure behaviour ─────────────────────────────────────────────────────


def test_failed_refresh_does_not_advance_last_successful_refresh():
    exporter, client = run()
    exporter.sync()
    before = {t: client.values(t) for t in REQUIRED_TABS}
    exporter.clock = lambda: NOW + timedelta(hours=1)
    exporter.source.activity = exporter.source.activity[:1]
    client.fail_on("write_values", 3)  # 1 = status, 2 = Activity Log, 3 = Daily Summary
    with pytest.raises(SheetsUnavailable):
        exporter.sync()
    d = dash(client)
    assert d["Last successful refresh"][0] == "2026-10-08 12:00:00 (Asia/Dhaka)"  # unchanged
    assert d["Last refresh status"][0].startswith("IN PROGRESS since 2026-10-08 13:00:00")
    assert client.values("Daily Summary") == before["Daily Summary"]  # not cleared, not half-written
    client.fail.clear()
    exporter.sync()  # retry repairs everything
    d = dash(client)
    assert d["Last refresh status"][0] == "OK" and d["Last successful refresh"][0].startswith("2026-10-08 13:00")
    assert len(by_header(client, "Activity Log")) == 1


def test_failed_write_never_leaves_a_tab_empty():
    exporter, client = run()
    exporter.sync()
    old = client.values("Activity Log")
    client.fail_on("write_values", 2)
    with pytest.raises(SheetsUnavailable):
        exporter.sync()
    assert client.values("Activity Log") == old
    assert not any(m == "clear_values" for m, _ in client.calls[-3:])


def test_google_unreachable_fails_before_writing_and_after_reading_the_database():
    exporter, client = run()
    client.fail_on("get_metadata")
    with pytest.raises(SheetsUnavailable, match="could not reach Google"):
        exporter.sync()
    assert exporter.source.loads == 1  # data was read (read-only) ...
    assert [m for m, _ in client.calls] == ["get_metadata"]  # ... and nothing was written


def test_status_reports_the_last_refresh():
    exporter, client = run()
    st = exporter.status()
    assert st.missing == REQUIRED_TABS and st.last_successful_refresh is None
    exporter.sync()
    st = exporter.status()
    assert st.present == REQUIRED_TABS and st.missing == ()
    assert st.last_successful_refresh == "2026-10-08 12:00:00 (Asia/Dhaka)" and st.last_status == "OK"


def test_init_keeps_an_existing_refresh_stamp():
    exporter, client = run()
    exporter.sync()
    exporter.init()
    assert dash(client)["Last successful refresh"][0] == "2026-10-08 12:00:00 (Asia/Dhaka)"


# ─── nothing sensitive exported ────────────────────────────────────────────

FORBIDDEN = ("token", "hash", "payload", "json", "password", "secret", "credential", "version", "seq",
             "session", "device", "record", "raw", "event_id", "period id")


def test_no_token_hash_or_payload_columns_or_values_are_exported():
    exporter, client = run()
    exporter.sync()
    for spec in DATA_SPECS:
        for header in spec.headers:
            assert not any(word in header.lower() for word in FORBIDDEN), header
    cells = [str(v) for t in REQUIRED_TABS for row in client.values(t) for v in row]
    assert not any(re.fullmatch(r"[0-9a-f]{64}", c) for c in cells)  # no content / summary hashes
    period_ids = {a.period_id for a in exporter.source.activity}
    assert not period_ids & set(cells)  # internal IDs not exported
    assert not any("{" in c and '"' in c for c in cells)  # no JSON


# ─── configuration ─────────────────────────────────────────────────────────


def _env(**kw) -> dict:
    env = {"ZAZA_GOOGLE_SHEET_ID": SHEET_ID, "ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE": "key.json"}
    env.update(kw)
    return {k: v for k, v in env.items() if v is not None}


def test_settings_from_env_defaults_and_values():
    s = sheets_settings_from_env(_env())
    assert (s.activity_days, s.summary_months, s.report_timezone, s.timeout_seconds) == (30, 12, None, 30)
    s = sheets_settings_from_env(_env(ZAZA_SHEETS_ACTIVITY_DAYS="7", ZAZA_SHEETS_SUMMARY_MONTHS="0",
                                      ZAZA_SHEETS_TIMEZONE=DHAKA))
    assert (s.activity_days, s.summary_months, s.report_timezone) == (7, 0, DHAKA)
    assert SHEET_ID not in repr(s) and s.masked_id == "1AbC…xyz9" == mask_id(SHEET_ID)


@pytest.mark.parametrize(("env", "message"), [
    ({"ZAZA_GOOGLE_SHEET_ID": None}, "ZAZA_GOOGLE_SHEET_ID is not set"),
    ({"ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE": None}, "ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE is not set"),
    ({"ZAZA_GOOGLE_SHEET_ID": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"}, "not the whole URL"),
    ({"ZAZA_GOOGLE_SHEET_ID": "abc"}, "not a valid spreadsheet ID"),
    ({"ZAZA_GOOGLE_SHEET_ID": "x" * 30 + " ;drop"}, "not a valid spreadsheet ID"),
    ({"ZAZA_SHEETS_ACTIVITY_DAYS": "0"}, "ZAZA_SHEETS_ACTIVITY_DAYS must be 1..366"),
    ({"ZAZA_SHEETS_ACTIVITY_DAYS": "thirty"}, "ZAZA_SHEETS_ACTIVITY_DAYS must be a whole number"),
    ({"ZAZA_SHEETS_SUMMARY_MONTHS": "-1"}, "ZAZA_SHEETS_SUMMARY_MONTHS must be 0..120"),
    ({"ZAZA_SHEETS_TIMEZONE": "Mars/Base"}, "not a known IANA time zone"),
])
def test_invalid_configuration_gives_a_clear_error(env, message):
    with pytest.raises(SheetsConfigError, match=re.escape(message)):
        sheets_settings_from_env(_env(**env))


PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcSECRETSECRET\n-----END PRIVATE KEY-----\n"


def _key_file(tmp_path: Path, **overrides) -> Path:
    info = {"type": "service_account", "project_id": "p", "private_key_id": "abc123privatekeyid",
            "private_key": PEM, "client_email": "zaza-sheets@p.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token"}
    info.update(overrides)
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps({k: v for k, v in info.items() if v is not None}), encoding="utf-8")
    return path


@pytest.mark.parametrize(("content", "message"), [
    (None, "file not found"),
    ("{not json", "not a valid JSON key file"),
    (json.dumps({"installed": {"client_id": "x", "client_secret": "SECRETSECRET"}}), "OAuth client file"),
    (json.dumps({"type": "authorized_user", "refresh_token": "SECRETSECRET"}), "not a service-account key"),
    (json.dumps({"type": "service_account", "private_key": PEM}), "missing client_email, token_uri"),
])
def test_bad_key_files_give_clear_errors_without_their_contents(tmp_path, content, message):
    path = tmp_path / "key.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(SheetsConfigError, match=re.escape(message)) as err:
        load_service_account_key(path)
    assert "SECRET" not in str(err.value) and "PRIVATE KEY" not in str(err.value)


def test_valid_key_file_is_parsed_without_exposing_the_key(tmp_path):
    key = load_service_account_key(_key_file(tmp_path))
    assert key.client_email == "zaza-sheets@p.iam.gserviceaccount.com"
    assert "PRIVATE" not in repr(key) and PEM in key.secrets()


def test_corrupt_private_key_error_does_not_echo_it(tmp_path):
    pytest.importorskip("googleapiclient")
    s = SheetsSettings(SHEET_ID, _key_file(tmp_path))
    with pytest.raises(SheetsConfigError, match="could not be loaded") as err:
        GoogleSheetsClient.from_settings(s)
    assert "SECRET" not in str(err.value) and "BEGIN" not in str(err.value)


def test_scrubber_removes_keys_tokens_and_the_sheet_id():
    scrub = Scrubber(SHEET_ID, ["abc123privatekeyid", PEM])
    text = (f'error for {SHEET_ID}: {{"private_key": "{json.dumps(PEM)[1:-1]}", "private_key_id": '
            '"abc123privatekeyid"}} Authorization: Bearer ya29.a0AfH6SMBsecret access_token=ya29.zzz')
    out = scrub(text)
    for secret in ("SECRET", "abc123privatekeyid", "ya29", "a0AfH6SMB", SHEET_ID):
        assert secret not in out
    assert mask_id(SHEET_ID) in out


# ─── Google adapter (official client library, offline) ─────────────────────


class _Boom(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.resp = type("R", (), {"status": status})()
        self.reason = reason


class _Service:
    """Just enough of the discovery service object to make requests fail."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def spreadsheets(self):  # noqa: ANN201
        return self

    def get(self, **kw):  # noqa: ANN003, ANN201
        return self

    def execute(self, num_retries: int = 0):  # noqa: ANN201
        raise self.exc


@pytest.mark.parametrize(("exc", "message"), [
    (_Boom(404, "Not Found"), "spreadsheet not found"),
    (_Boom(403, "The caller does not have permission"), "share the spreadsheet with the service account"),
    (_Boom(429, "Quota"), "quota exceeded"),
    (_Boom(500, f"backend error for {SHEET_ID} token ya29.leaked {PEM}"), "Google API error 500"),
    (ConnectionResetError("reset"), "could not reach Google"),
    (type("RefreshError", (Exception,), {})(f"invalid_grant {PEM}"), "could not authenticate"),
])
def test_google_errors_become_clean_safe_messages(exc, message, caplog):
    caplog.set_level(logging.DEBUG)
    client = GoogleSheetsClient(_Service(exc), SHEET_ID, Scrubber(SHEET_ID, [PEM]),
                                service_account_email="zaza-sheets@p.iam.gserviceaccount.com")
    with pytest.raises(SheetsUnavailable, match=message) as err:
        client.get_metadata()
    text = str(err.value) + caplog.text
    assert "PRIVATE KEY" not in text and "ya29" not in text and SHEET_ID not in text
    assert err.value.__cause__ is None and err.value.__suppress_context__  # original not chained


def _mock_service(responses: list[tuple[dict, str]]):  # noqa: ANN202
    googleapiclient = pytest.importorskip("googleapiclient")  # noqa: F841
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    class Recording(HttpMockSequence):
        def __init__(self, iterable) -> None:  # noqa: ANN001
            super().__init__(iterable)
            self.requests: list[tuple[str, str, object]] = []

        def request(self, uri, method="GET", body=None, headers=None, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
            self.requests.append((method, uri, body))
            return super().request(uri, method, body, headers, *args, **kwargs)

    http = Recording(responses)
    return build("sheets", "v4", http=http, cache_discovery=False, static_discovery=True), http


def test_google_client_reads_metadata_and_writes_raw_values_offline():
    meta = {"properties": {"title": "ZaZa Reports"}, "sheets": [
        {"properties": {"sheetId": 7, "title": "Dashboard", "gridProperties": {"rowCount": 50, "columnCount": 5}}}]}
    service, http = _mock_service([({"status": "200"}, json.dumps(meta)), ({"status": "200"}, "{}"),
                                   ({"status": "200"}, "{}"), ({"status": "200"}, json.dumps({"values": [["a"]]}))])
    client = GoogleSheetsClient(service, SHEET_ID)
    info = client.get_metadata()
    assert info.title == "ZaZa Reports" and info.tab("Dashboard").row_count == 50
    client.write_values("'Activity Log'!A1", [["=1+1", 2]])
    client.clear_values("'Activity Log'!A5:Q100")
    assert client.read_values("'Dashboard'!A1:B2") == [["a"]]
    method, uri, body = http.requests[1]
    assert method == "PUT" and "valueInputOption=RAW" in uri and json.loads(body) == {"values": [["=1+1", 2]]}
    assert http.requests[2][0] == "POST" and ":clear" in http.requests[2][1]
    assert "valueRenderOption=FORMATTED_VALUE" in http.requests[3][1]


def test_google_client_http_403_is_a_clean_permission_error():
    body = json.dumps({"error": {"code": 403, "message": "The caller does not have permission ya29.secret",
                                 "status": "PERMISSION_DENIED"}})
    service, _ = _mock_service([({"status": "403"}, body)])
    with pytest.raises(SheetsUnavailable, match="permission denied") as err:
        GoogleSheetsClient(service, SHEET_ID).get_metadata()
    assert "ya29" not in str(err.value) and SHEET_ID not in str(err.value)


def test_service_account_credentials_load_from_a_real_key(tmp_path):
    pytest.importorskip("googleapiclient")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    client = GoogleSheetsClient.from_settings(SheetsSettings(SHEET_ID, _key_file(tmp_path, private_key=pem)))
    assert client.service_account_email == "zaza-sheets@p.iam.gserviceaccount.com"  # no network used


# ─── CLI ───────────────────────────────────────────────────────────────────


def test_cli_sheets_commands_report_missing_configuration(monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    monkeypatch.delenv("ZAZA_GOOGLE_SHEET_ID", raising=False)
    assert main(["sheets-status"]) == 2
    assert "ZAZA_GOOGLE_SHEET_ID is not set" in capsys.readouterr().err


def test_cli_sheets_init_and_status_use_the_client(monkeypatch, tmp_path, capsys, caplog):
    from deskmate.zaza_server.__main__ import main

    caplog.set_level(logging.DEBUG)
    fake = FakeSheetsClient()
    fake.service_account_email = "zaza-sheets@p.iam.gserviceaccount.com"
    monkeypatch.setenv("ZAZA_GOOGLE_SHEET_ID", SHEET_ID)
    monkeypatch.setenv("ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE", str(_key_file(tmp_path)))
    monkeypatch.setattr(client_mod.GoogleSheetsClient, "from_settings", classmethod(lambda cls, s: fake))
    assert main(["sheets-init"]) == 0
    assert main(["sheets-status"]) == 0
    out = capsys.readouterr().out
    assert "1AbC…xyz9" in out and SHEET_ID not in out and "zaza-sheets@p.iam" in out
    assert "Last successful refresh: Never" in out
    fake.fail_on("get_metadata")
    assert main(["sheets-status"]) == 3
    err = capsys.readouterr().err
    assert "Google Sheets refresh failed" in err and "Nothing in the database was changed" in err
    assert "PRIVATE KEY" not in caplog.text + err


def test_cli_sheets_sync_requires_postgres(monkeypatch, tmp_path, capsys):
    from deskmate.zaza_server.__main__ import main

    monkeypatch.setenv("ZAZA_GOOGLE_SHEET_ID", SHEET_ID)
    monkeypatch.setenv("ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE", str(_key_file(tmp_path)))
    assert main(["--backend", "sqlite", "sheets-sync"]) == 2
    assert "ZAZA_SERVER_BACKEND=postgres" in capsys.readouterr().err


def test_sync_api_server_does_not_import_the_sheets_package():
    code = ("import sys, deskmate.zaza_server.app, deskmate.zaza_server.service; "
            "sys.exit(any(m.startswith('deskmate.zaza_server.sheets') or m.startswith('googleapiclient') "
            "for m in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# ─── helpers ───────────────────────────────────────────────────────────────


def test_a1_helpers():
    assert [column_letter(i) for i in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]
    assert a1("Activity Log", 0, 1) == "'Activity Log'!A1"
    assert a1("Bob's tab", 2, 5, 16, 100) == "'Bob''s tab'!C5:Q100"
    assert parse_a1("'Bob''s tab'!C5:Q100") == ("Bob's tab", 2, 4, 16, 99)
    assert parse_a1("'Dashboard'!A6:E") == ("Dashboard", 0, 5, 4, None)
