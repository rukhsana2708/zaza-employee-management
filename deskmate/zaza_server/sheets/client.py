"""The boundary to Google Sheets.

:class:`SheetsClient` is the small interface the exporter uses.
:class:`GoogleSheetsClient` implements it with the official Google API
client (``google-api-python-client`` + ``google-auth``, optional extra
``zaza-sheets``); :class:`FakeSheetsClient` implements it in memory for the
automated tests, which never contact Google.

Every Google failure surfaces as :class:`SheetsUnavailable` with a short,
scrubbed message: no key material, no access tokens, and the spreadsheet ID
masked.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from .config import SCOPES, Scrubber, SheetsConfigError, SheetsSettings, load_service_account_key

logger = logging.getLogger("zaza_server.sheets")


class SheetsUnavailable(Exception):
    """Google Sheets could not be reached or refused the request. The
    message is safe to show and log."""


class SheetsSyncBusy(Exception):
    """Another Sheets refresh is already running."""


@dataclass(frozen=True)
class TabInfo:
    sheet_id: int
    title: str
    row_count: int
    column_count: int
    frozen_rows: int = 0


@dataclass(frozen=True)
class SpreadsheetInfo:
    title: str
    tabs: tuple[TabInfo, ...]

    def tab(self, title: str) -> TabInfo | None:
        return next((t for t in self.tabs if t.title == title), None)


class SheetsClient(Protocol):
    def get_metadata(self) -> SpreadsheetInfo: ...
    def batch_update(self, requests: list[dict]) -> None: ...
    def write_values(self, a1_range: str, values: list[list]) -> None:
        """Write ``values`` starting at the range's top-left cell (RAW: text
        is never evaluated as a formula)."""
    def clear_values(self, a1_range: str) -> None: ...
    def read_values(self, a1_range: str) -> list[list[str]]:
        """Displayed (formatted) values."""


# ─── A1 notation ──────────────────────────────────────────────────────────


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def column_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def a1(title: str, col0: int, row1: int, end_col0: int | None = None, end_row1: int | None = None) -> str:
    """``'Tab'!A1`` or ``'Tab'!A1:Q100`` (rows 1-based, columns 0-based)."""
    ref = f"'{title.replace(chr(39), chr(39) * 2)}'!{column_letter(col0)}{row1}"
    if end_col0 is not None:
        ref += f":{column_letter(end_col0)}{end_row1 if end_row1 is not None else ''}"
    return ref


_A1_RE = re.compile(r"^'((?:[^']|'')+)'!([A-Z]+)(\d+)(?::([A-Z]+)(\d+)?)?$")


def parse_a1(ref: str) -> tuple[str, int, int, int | None, int | None]:
    """→ (title, col0, row0, end_col0, end_row0); end row None = open-ended."""
    m = _A1_RE.match(ref)
    if not m:
        raise ValueError(f"unsupported range {ref!r}")
    title = m.group(1).replace("''", "'")
    col0, row0 = column_index(m.group(2)), int(m.group(3)) - 1
    end_col = column_index(m.group(4)) if m.group(4) else None
    end_row = int(m.group(5)) - 1 if m.group(5) else None
    return title, col0, row0, end_col, end_row


# ─── Google ───────────────────────────────────────────────────────────────


class GoogleSheetsClient:
    """The official Sheets API v4, authenticated as a service account that
    the manager has shared the spreadsheet with (Editor)."""

    RETRIES = 3  # google-api-python-client retries 429/5xx with exponential backoff

    def __init__(self, service, spreadsheet_id: str, scrubber: Scrubber | None = None,  # noqa: ANN001
                 *, service_account_email: str | None = None) -> None:
        self._service = service
        self._id = spreadsheet_id
        self._scrub = scrubber or Scrubber(spreadsheet_id)
        self.service_account_email = service_account_email

    @classmethod
    def from_settings(cls, settings: SheetsSettings) -> GoogleSheetsClient:
        key = load_service_account_key(settings.service_account_file)
        try:
            import google_auth_httplib2  # noqa: PLC0415
            import httplib2  # noqa: PLC0415
            from google.oauth2 import service_account  # noqa: PLC0415
            from googleapiclient.discovery import build  # noqa: PLC0415
        except ImportError:
            raise SheetsConfigError(
                "Google Sheets support is not installed: pip install -e .[zaza-sheets]") from None
        scrub = Scrubber(settings.spreadsheet_id, key.secrets())
        try:
            credentials = service_account.Credentials.from_service_account_info(key.info, scopes=list(SCOPES))
        except Exception:  # noqa: BLE001 — never echo the library's message (it may quote the key)
            raise SheetsConfigError(
                f"the service-account key in {settings.service_account_file} could not be loaded "
                "(corrupt or not a private key)") from None
        http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=settings.timeout_seconds))
        service = build("sheets", "v4", http=http, cache_discovery=False, static_discovery=True)
        return cls(service, settings.spreadsheet_id, scrub, service_account_email=key.client_email)

    def _run(self, request, what: str):  # noqa: ANN001, ANN202
        try:
            return request.execute(num_retries=self.RETRIES)
        except Exception as exc:  # noqa: BLE001
            raise SheetsUnavailable(self._describe(exc, what)) from None

    def _describe(self, exc: Exception, what: str) -> str:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status is not None:
            status = int(status)
            if status == 404:
                return f"{what}: spreadsheet not found (check ZAZA_GOOGLE_SHEET_ID)"
            if status == 403:
                who = f" ({self.service_account_email})" if self.service_account_email else ""
                return (f"{what}: permission denied — share the spreadsheet with the service account{who} "
                        "as Editor, and enable the Google Sheets API for its project")
            if status == 429:
                return f"{what}: Google API quota exceeded; try again later"
            reason = getattr(exc, "reason", "") or ""
            return self._scrub(f"{what}: Google API error {status} {reason}".strip())[:300]
        name = type(exc).__name__
        if "RefreshError" in name or "TransportError" in name:
            return f"{what}: could not authenticate with Google (check the service-account key, clock and network)"
        if isinstance(exc, (OSError, TimeoutError)) or "Http" in name or "Socket" in name:
            return f"{what}: could not reach Google ({name})"
        return f"{what}: unexpected Google API failure ({name})"

    def get_metadata(self) -> SpreadsheetInfo:
        data = self._run(self._service.spreadsheets().get(
            spreadsheetId=self._id,
            fields="properties.title,sheets.properties(sheetId,title,gridProperties)"), "open spreadsheet")
        tabs = []
        for sheet in data.get("sheets", []):
            p = sheet["properties"]
            grid = p.get("gridProperties", {})
            tabs.append(TabInfo(p["sheetId"], p["title"], grid.get("rowCount", 0), grid.get("columnCount", 0),
                                grid.get("frozenRowCount", 0)))
        return SpreadsheetInfo(data.get("properties", {}).get("title", ""), tuple(tabs))

    def batch_update(self, requests: list[dict]) -> None:
        if requests:
            self._run(self._service.spreadsheets().batchUpdate(
                spreadsheetId=self._id, body={"requests": requests}), "update layout")

    def write_values(self, a1_range: str, values: list[list]) -> None:
        self._run(self._service.spreadsheets().values().update(
            spreadsheetId=self._id, range=a1_range, valueInputOption="RAW", body={"values": values}),
            f"write {a1_range.split('!')[0]}")

    def clear_values(self, a1_range: str) -> None:
        self._run(self._service.spreadsheets().values().clear(
            spreadsheetId=self._id, range=a1_range, body={}), f"clear {a1_range.split('!')[0]}")

    def read_values(self, a1_range: str) -> list[list[str]]:
        data = self._run(self._service.spreadsheets().values().get(
            spreadsheetId=self._id, range=a1_range, valueRenderOption="FORMATTED_VALUE"),
            f"read {a1_range.split('!')[0]}")
        return [[str(v) for v in row] for row in data.get("values", [])]


# ─── in-memory fake (tests) ───────────────────────────────────────────────


@dataclass
class FakeTab:
    sheet_id: int
    title: str
    row_count: int = 1000
    column_count: int = 26
    frozen_rows: int = 0
    cells: dict[tuple[int, int], object] = field(default_factory=dict)
    formats: list[dict] = field(default_factory=list)
    widths: dict[int, int] = field(default_factory=dict)

    def grid(self) -> list[list]:
        """Values as a list of rows, trimmed after the last non-empty row."""
        filled = [(r, c) for (r, c), v in self.cells.items() if v != ""]
        if not filled:
            return []
        height = max(r for r, _ in filled) + 1
        width = max(c for _, c in filled) + 1
        return [[self.cells.get((r, c), "") for c in range(width)] for r in range(height)]


class FakeSheetsClient:
    """Implements :class:`SheetsClient` in memory, enforcing the same grid
    limits as Google. :meth:`fail_on` makes a method raise
    :class:`SheetsUnavailable`, simulating Google being unavailable."""

    def __init__(self, title: str = "ZaZa Reports", tabs: tuple[str, ...] = ("Sheet1",)) -> None:
        self.title = title
        self.tabs: dict[str, FakeTab] = {}
        self._next_id = 0
        self.calls: list[tuple[str, object]] = []
        self.fail: dict[str, int] = {}
        self._counts: dict[str, int] = {}
        for t in tabs:
            self._add(t)

    def _add(self, title: str) -> FakeTab:
        tab = FakeTab(self._next_id, title)
        self._next_id += 1
        self.tabs[title] = tab
        return tab

    def fail_on(self, method: str, nth: int = 0) -> None:
        """From now on, the ``nth`` call of ``method`` fails (0 = every call)."""
        self.fail[method] = nth
        self._counts[method] = 0

    def _call(self, method: str, arg: object) -> None:
        self.calls.append((method, arg))
        self._counts[method] = self._counts.get(method, 0) + 1
        nth = self.fail.get(method)
        if nth is not None and (nth == 0 or nth == self._counts[method]):
            raise SheetsUnavailable(f"{method}: could not reach Google (simulated)")

    def _by_id(self, sheet_id: int) -> FakeTab:
        return next(t for t in self.tabs.values() if t.sheet_id == sheet_id)

    def get_metadata(self) -> SpreadsheetInfo:
        self._call("get_metadata", None)
        return SpreadsheetInfo(self.title, tuple(
            TabInfo(t.sheet_id, t.title, t.row_count, t.column_count, t.frozen_rows) for t in self.tabs.values()))

    def batch_update(self, requests: list[dict]) -> None:
        self._call("batch_update", requests)
        for req in requests:  # validate everything first: batchUpdate is all-or-nothing
            (kind,) = req
            if kind not in ("addSheet", "updateSheetProperties", "repeatCell", "updateDimensionProperties"):
                raise SheetsUnavailable(f"update layout: unsupported request {kind}")
            if kind == "addSheet" and req[kind]["properties"]["title"] in self.tabs:
                raise SheetsUnavailable("update layout: Google API error 400 duplicate sheet name")
        for req in requests:
            (kind, body), = req.items()
            if kind == "addSheet":
                self._add(body["properties"]["title"])
            elif kind == "updateSheetProperties":
                tab = self._by_id(body["properties"]["sheetId"])
                grid = body["properties"].get("gridProperties", {})
                tab.row_count = grid.get("rowCount", tab.row_count)
                tab.column_count = grid.get("columnCount", tab.column_count)
                tab.frozen_rows = grid.get("frozenRowCount", tab.frozen_rows)
                tab.cells = {(r, c): v for (r, c), v in tab.cells.items()
                             if r < tab.row_count and c < tab.column_count}
            elif kind == "repeatCell":
                self._by_id(body["range"]["sheetId"]).formats.append(body)
            else:
                rng = body["range"]
                self._by_id(rng["sheetId"]).widths[rng["startIndex"]] = body["properties"]["pixelSize"]

    def _tab(self, title: str) -> FakeTab:
        if title not in self.tabs:
            raise SheetsUnavailable(f"Google API error 400 unable to parse range: {title}")
        return self.tabs[title]

    def write_values(self, a1_range: str, values: list[list]) -> None:
        self._call("write_values", a1_range)
        title, col0, row0, _, _ = parse_a1(a1_range)
        tab = self._tab(title)
        if values and (row0 + len(values) > tab.row_count or col0 + max(map(len, values)) > tab.column_count):
            raise SheetsUnavailable(f"write {title}: Google API error 400 range exceeds grid limits")
        for r, row in enumerate(values):
            for c, v in enumerate(row):
                if v is None:
                    raise AssertionError("None leaves the old cell in place: write '' to clear it")
                tab.cells[(row0 + r, col0 + c)] = v

    def clear_values(self, a1_range: str) -> None:
        self._call("clear_values", a1_range)
        title, col0, row0, end_col, end_row = parse_a1(a1_range)
        tab = self._tab(title)
        end_col = col0 if end_col is None else end_col
        end_row = tab.row_count - 1 if end_row is None else end_row
        for key in [k for k in tab.cells if row0 <= k[0] <= end_row and col0 <= k[1] <= end_col]:
            del tab.cells[key]

    def read_values(self, a1_range: str) -> list[list[str]]:
        self._call("read_values", a1_range)
        title, col0, row0, end_col, end_row = parse_a1(a1_range)
        tab = self._tab(title)
        grid = tab.grid()
        end_row = len(grid) - 1 if end_row is None else min(end_row, len(grid) - 1)
        end_col = col0 if end_col is None else end_col
        rows = [[str(v) for v in grid[r][col0:end_col + 1]] for r in range(row0, end_row + 1)]
        while rows and not any(rows[-1]):
            rows.pop()
        return rows

    def values(self, title: str) -> list[list]:
        """Test helper: the tab's values as rows."""
        return self.tabs[title].grid()
