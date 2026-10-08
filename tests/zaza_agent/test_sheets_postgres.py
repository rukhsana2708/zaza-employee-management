"""Phase 6 on PostgreSQL (opt-in: ZAZA_TEST_POSTGRES_URL, see conftest.py).

The export reads the real tables through PostgresReportSource and writes to
FakeSheetsClient. It must be read-only: a refresh — successful or failed —
leaves every table exactly as it was.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from .conftest import PG_TABLES
from .test_attendance_postgres import TZ, Synced, at

psycopg = pytest.importorskip("psycopg")

from deskmate.zaza_server.attendance import SummaryService  # noqa: E402
from deskmate.zaza_server.attendance.postgres_store import PostgresAttendanceStore  # noqa: E402
from deskmate.zaza_server.auth import register_device  # noqa: E402
from deskmate.zaza_server.sheets import client as client_mod  # noqa: E402
from deskmate.zaza_server.sheets.client import (  # noqa: E402
    FakeSheetsClient,
    SheetsSyncBusy,
    SheetsUnavailable,
)
from deskmate.zaza_server.sheets.config import SheetsSettings  # noqa: E402
from deskmate.zaza_server.sheets.exporter import SheetsExporter  # noqa: E402
from deskmate.zaza_server.sheets.queries import PostgresReportSource  # noqa: E402

UTC = timezone.utc
H = 3600
MON = date(2026, 10, 5)
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)  # Thursday 12:00 Dhaka
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-xyz9"
SETTINGS = SheetsSettings(SHEET_ID, service_account_file=Path("unused.json"))


@pytest.fixture
def office(pg_repo):
    pg_repo.add_employee("emp-1", "Employee One", timezone=TZ)
    register_device(pg_repo, "device-1", "emp-1")
    with pg_repo.connection() as conn:
        conn.execute("UPDATE devices SET last_seen_at = %s", (NOW,))
    store = PostgresAttendanceStore(pg_repo)
    for dow in range(1, 6):
        store.add_schedule("emp-1", is_working_day=True, day_of_week=dow, effective_from=date(2026, 1, 1),
                           start_time=time(9), end_time=time(17), expected_work_seconds=8 * H)
    sync = Synced(pg_repo)
    sid = sync.session(at(MON, "09:00"), at(MON, "17:00"))
    sync.period(sid, "ACTIVE", at(MON, "09:00"), at(MON, "12:00"))
    sync.period(sid, "IDLE", at(MON, "12:00"), at(MON, "12:30"))
    sync.period(sid, "LOCKED", at(MON, "12:30"), at(MON, "13:00"))
    sync.period(sid, "UNKNOWN", at(MON, "13:00"), at(MON, "13:10"))
    sync.period(sid, "ACTIVE", at(MON, "13:10"), at(MON, "17:00"))
    SummaryService(store, clock=lambda: NOW).recalculate(date(2026, 10, 1), date(2026, 10, 8))
    return pg_repo


def fingerprint(repo) -> dict:  # noqa: ANN001
    """Row count + content digest of every table."""
    out = {}
    with repo.connection() as conn:
        for table in PG_TABLES:
            row = conn.execute(f"SELECT count(*) AS n, md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
                               f"AS h FROM {table} t").fetchone()
            out[table] = (row["n"], row["h"])
    return out


def exporter(repo, client: FakeSheetsClient) -> SheetsExporter:  # noqa: ANN001
    return SheetsExporter(client, SETTINGS, source=PostgresReportSource(repo), clock=lambda: NOW)


def rows(client: FakeSheetsClient, tab: str) -> list[dict]:
    header, *data = client.values(tab)
    return [dict(zip(header, r, strict=True)) for r in data]


def test_sheets_sync_reads_postgres(office):
    client = FakeSheetsClient()
    result = exporter(office, client).sync()
    log = rows(client, "Activity Log")
    assert result.rows["Activity Log"] == len(log) == 5  # one row per activity period
    assert [r["Status"] for r in log] == ["Active", "Unknown", "Locked", "Idle", "Active"]  # newest first
    daily = {r["Date"]: r for r in rows(client, "Daily Summary")}
    with office.connection() as conn:
        n_daily = conn.execute("SELECT count(*) AS n FROM daily_summaries").fetchone()["n"]
    assert len(daily) == n_daily
    mon = daily[(MON - date(1899, 12, 30)).days]
    assert mon["Attendance Status"] == "Present" and mon["Employee"] == "Employee One"
    assert mon["Active Hours"] == pytest.approx((3 + 3 + 50 / 60) * H / 86400)
    assert mon["Unknown Hours"] == pytest.approx(600 / 86400)
    assert len(rows(client, "Weekly Summary")) == 2 and len(rows(client, "Monthly Summary")) == 1
    assert client.values("Dashboard")[3][1] == "2026-10-08 12:00:00 (Asia/Dhaka)"


def test_successful_and_failed_refreshes_leave_postgres_unchanged(office):
    before = fingerprint(office)
    client = FakeSheetsClient()
    exporter(office, client).sync()
    assert fingerprint(office) == before  # read-only
    client.fail_on("write_values", 3)
    with pytest.raises(SheetsUnavailable):
        exporter(office, client).sync()
    client.fail_on("get_metadata")
    with pytest.raises(SheetsUnavailable):
        exporter(office, client).sync()
    assert fingerprint(office) == before


def test_report_snapshot_is_a_read_only_transaction(office):
    with PostgresReportSource(office).snapshot() as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE employees SET display_name = 'changed'")


def test_only_one_refresh_at_a_time(office):
    first, second = PostgresReportSource(office), PostgresReportSource(office)
    with first.lock():
        with pytest.raises(SheetsSyncBusy):
            with second.lock():
                pass
    with second.lock():  # released afterwards
        pass


def test_cli_sheets_sync(office, pg_settings, monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    s = pg_settings
    for k, v in {"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_HOST": s.host, "ZAZA_DB_PORT": str(s.port),
                 "ZAZA_DB_NAME": s.dbname, "ZAZA_DB_USER": s.user, "ZAZA_DB_PASSWORD": s.password,
                 "ZAZA_DB_SCHEMA": s.schema, "ZAZA_GOOGLE_SHEET_ID": SHEET_ID,
                 "ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE": "unused.json"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ZAZA_DATABASE_URL", raising=False)
    fake = FakeSheetsClient()
    monkeypatch.setattr(client_mod.GoogleSheetsClient, "from_settings", classmethod(lambda cls, st: fake))
    before = fingerprint(office)
    assert main(["sheets-sync"]) == 0
    out = capsys.readouterr().out
    assert "Daily Summary" in out and "row(s)" in out and s.password not in out
    assert len(fake.values("Daily Summary")) > 1
    fake.fail_on("write_values", 2)
    assert main(["sheets-sync"]) == 3
    err = capsys.readouterr().err
    assert "Nothing in the database was changed" in err and s.password not in err
    assert fingerprint(office) == before
