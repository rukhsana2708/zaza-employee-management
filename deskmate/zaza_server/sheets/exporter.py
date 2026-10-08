"""One-way export: PostgreSQL → Google Sheets.

``init``   check access, create missing tabs, install headers and formatting.
``sync``   full deterministic refresh of the five managed tabs.
``status`` check access and read the Dashboard's refresh status.

Refresh order (each step only starts if the previous one succeeded):

1. Read everything from PostgreSQL (one read-only snapshot) and prepare
   every value in memory. A database problem stops here: Google untouched.
2. Create missing tabs; grow grids if needed; apply formatting. Nothing
   is cleared.
3. Mark the Dashboard status "IN PROGRESS" (the last-successful-refresh
   cell is left as it was).
4. For each data tab: overwrite the managed range from row 1 with the new
   rows, THEN clear only the stale rows below them (and columns to the
   right). A tab is never cleared before its replacement is written.
5. Write the Dashboard last, including "Last successful refresh" and status
   "OK". So that cell only advances when every tab was written.

If Google fails at any step the command stops with a clean error, the
database is unaffected, and the next run repeats the whole refresh — the
result only depends on the database, so retrying is always safe. One
database row produces exactly one Sheet row; rows are sorted
deterministically, so repeating a refresh produces identical tabs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import SheetsClient, SpreadsheetInfo, TabInfo, a1
from .config import SheetsSettings
from .formatter import (
    LAST_REFRESH_LABEL,
    STATUS_LABEL,
    STATUS_ROW,
    activity_tab,
    daily_tab,
    dashboard_layout,
    dashboard_tab,
    data_tab_layout,
    grid_request,
    in_progress_text,
    monthly_tab,
    report_timezone,
    weekly_tab,
)
from .models import DASHBOARD_WIDTHS, DATA_SPECS, REQUIRED_TABS, TAB_DASHBOARD, TabValues

logger = logging.getLogger("zaza_server.sheets")
UTC = timezone.utc
CHUNK_ROWS = 5000  # rows per write request (keeps each request well under Google's size limits)


@dataclass(frozen=True)
class InitResult:
    title: str
    created: tuple[str, ...]
    existing: tuple[str, ...]


@dataclass(frozen=True)
class SyncResult:
    refreshed_at: datetime
    report_timezone: str
    rows: dict[str, int]  # data rows per tab (header excluded)


@dataclass(frozen=True)
class StatusResult:
    title: str
    present: tuple[str, ...]
    missing: tuple[str, ...]
    last_successful_refresh: str | None
    last_status: str | None


class SheetsExporter:
    def __init__(self, client: SheetsClient, settings: SheetsSettings, *, source=None,  # noqa: ANN001
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), chunk_rows: int = CHUNK_ROWS) -> None:
        self.client = client
        self.settings = settings
        self.source = source
        self.clock = clock
        self.chunk_rows = chunk_rows

    # ── shared steps ──────────────────────────────────────────────────────
    def _ensure_tabs(self) -> tuple[SpreadsheetInfo, tuple[str, ...]]:
        """Create any missing managed tab; existing tabs (and any other tabs
        in the spreadsheet) are reused as they are."""
        meta = self.client.get_metadata()
        missing = tuple(t for t in REQUIRED_TABS if meta.tab(t) is None)
        if missing:
            self.client.batch_update([{"addSheet": {"properties": {"title": t}}} for t in missing])
            meta = self.client.get_metadata()
        return meta, missing

    @staticmethod
    def _grow(tab: TabInfo, rows: int, columns: int) -> list[dict]:
        """Never shrinks: shrinking happens implicitly by clearing stale rows."""
        if tab.row_count >= rows and tab.column_count >= columns:
            return []
        return [grid_request(tab.sheet_id, max(tab.row_count, rows), max(tab.column_count, columns))]

    def _replace(self, tab: TabInfo, values: TabValues) -> None:
        """Write the new rows, then clear what is left of the old ones."""
        n = len(values.rows)
        for start in range(0, n, self.chunk_rows):
            self.client.write_values(a1(tab.title, 0, start + 1), values.rows[start:start + self.chunk_rows])
        if tab.row_count > n:
            self.client.clear_values(a1(tab.title, 0, n + 1, max(tab.column_count, values.width) - 1,
                                        tab.row_count))
        if tab.column_count > values.width and n:
            self.client.clear_values(a1(tab.title, values.width, 1, tab.column_count - 1, n))

    # ── commands ──────────────────────────────────────────────────────────
    def init(self) -> InitResult:
        meta, created = self._ensure_tabs()
        requests: list[dict] = []
        for spec in DATA_SPECS:
            tab = meta.tab(spec.title)
            requests += self._grow(tab, 2, len(spec.columns)) + data_tab_layout(tab.sheet_id, spec)
        dash = meta.tab(TAB_DASHBOARD)
        requests += self._grow(dash, 40, len(DASHBOARD_WIDTHS)) + dashboard_layout(dash.sheet_id, [], [], 0)
        self.client.batch_update(requests)
        for spec in DATA_SPECS:
            self.client.write_values(a1(spec.title, 0, 1), [spec.headers])
        if not self.client.read_values(a1(TAB_DASHBOARD, 0, 1, 1, STATUS_ROW)):
            self.client.write_values(a1(TAB_DASHBOARD, 0, 1), [
                ["ZaZa Attendance Report", ""],
                ["Read-only report generated from the ZaZa database. Run sheets-sync to fill it.", ""],
                ["", ""], [LAST_REFRESH_LABEL, "Never"], [STATUS_LABEL, "Not refreshed yet"],
            ])
        logger.info("Google Sheets initialised: %d tab(s) created", len(created))
        return InitResult(meta.title, created, tuple(t for t in REQUIRED_TABS if t not in created))

    def sync(self) -> SyncResult:
        if self.source is None:
            raise RuntimeError("sync needs a report source")
        now = self.clock()
        s = self.settings
        with self.source.lock():
            # 1. database first: prepare every value before touching Google
            data = self.source.load(now, activity_days=s.activity_days, summary_months=s.summary_months)
            tz = report_timezone(data.employees, s.report_timezone)
            tabs = [activity_tab(data), daily_tab(data), weekly_tab(data), monthly_tab(data)]
            dash, formats, bold = dashboard_tab(data, now, report_tz=tz, activity_days=s.activity_days,
                                                summary_months=s.summary_months)
            # 2. tabs, grid sizes, formatting (nothing cleared)
            meta, _ = self._ensure_tabs()
            requests: list[dict] = []
            for values, spec in zip(tabs, DATA_SPECS, strict=True):
                tab = meta.tab(spec.title)
                requests += self._grow(tab, len(values.rows), values.width) + data_tab_layout(tab.sheet_id, spec)
            dash_tab = meta.tab(TAB_DASHBOARD)
            requests += self._grow(dash_tab, len(dash.rows), dash.width)
            requests += dashboard_layout(dash_tab.sheet_id, formats, bold, len(dash.rows))
            self.client.batch_update(requests)
            meta = self.client.get_metadata()
            # 3. mark the refresh as in progress; "last successful" untouched
            self.client.write_values(a1(TAB_DASHBOARD, 0, STATUS_ROW), [[STATUS_LABEL, in_progress_text(now, tz)]])
            # 4. data tabs: replace, then clear stale rows
            for values in tabs:
                self._replace(meta.tab(values.title), values)
            # 5. Dashboard last: only now does "Last successful refresh" advance
            self._replace(meta.tab(TAB_DASHBOARD), dash)
        rows = {v.title: len(v.rows) - 1 for v in tabs}
        logger.info("Google Sheets refreshed (%s)", ", ".join(f"{k}: {n} rows" for k, n in rows.items()))
        return SyncResult(now, tz, rows)

    def status(self) -> StatusResult:
        meta = self.client.get_metadata()
        present = tuple(t for t in REQUIRED_TABS if meta.tab(t) is not None)
        missing = tuple(t for t in REQUIRED_TABS if t not in present)
        last = state = None
        if TAB_DASHBOARD in present:
            for row in self.client.read_values(a1(TAB_DASHBOARD, 0, 1, 1, 12)):
                if len(row) >= 2 and row[0] == LAST_REFRESH_LABEL:
                    last = row[1]
                elif len(row) >= 2 and row[0] == STATUS_LABEL:
                    state = row[1]
        return StatusResult(meta.title, present, missing, last, state)
