"""OPTIONAL live test against the real Google Sheets API. Skipped by default.

It needs a DISPOSABLE spreadsheet (its contents are overwritten):

1. Create a new spreadsheet whose title contains "ZaZa Test".
2. Share it with the service account's email as Editor.
3. Set, in the same shell:
       ZAZA_TEST_GOOGLE_SHEET_ID=<that spreadsheet's ID>
       ZAZA_TEST_GOOGLE_SERVICE_ACCOUNT_FILE=<path to the key file>
4. pip install -e .[zaza-sheets] && pytest tests/zaza_agent/test_sheets_google_live.py

No database is used: the data comes from the in-memory source. The test
refuses to run if the spreadsheet title doesn't contain "ZaZa Test", so a
real report can't be overwritten by mistake.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_sheets import NOW, by_header, dash, standard_office

SHEET_ENV = "ZAZA_TEST_GOOGLE_SHEET_ID"
KEY_ENV = "ZAZA_TEST_GOOGLE_SERVICE_ACCOUNT_FILE"


@pytest.fixture(scope="module")
def live_client():
    if not (os.environ.get(SHEET_ENV) and os.environ.get(KEY_ENV)):
        pytest.skip(f"live Google Sheets test is opt-in: set {SHEET_ENV} and {KEY_ENV}")
    pytest.importorskip("googleapiclient")
    from deskmate.zaza_server.sheets.client import GoogleSheetsClient
    from deskmate.zaza_server.sheets.config import SheetsSettings

    settings = SheetsSettings(os.environ[SHEET_ENV], Path(os.environ[KEY_ENV]))
    client = GoogleSheetsClient.from_settings(settings)
    title = client.get_metadata().title
    if "ZaZa Test" not in title:
        pytest.fail(f"refusing to run: the spreadsheet title must contain 'ZaZa Test' (it is {title!r})")
    return client, settings


class _Reading:
    """Reads the live tabs in the shape the fake-based helpers expect."""

    def __init__(self, client) -> None:  # noqa: ANN001
        self.client = client

    def values(self, tab: str) -> list[list]:
        rows = self.client.read_values(f"'{tab}'!A1:Z")
        width = max(map(len, rows), default=0)
        return [row + [""] * (width - len(row)) for row in rows]  # Google trims trailing empty cells


def test_live_init_sync_and_resync(live_client):
    from deskmate.zaza_server.sheets.exporter import SheetsExporter
    from deskmate.zaza_server.sheets.models import REQUIRED_TABS

    client, settings = live_client
    source = standard_office().source()
    exporter = SheetsExporter(client, settings, source=source, clock=lambda: NOW)
    exporter.init()
    exporter.sync()
    exporter.sync()  # repeated: no duplicates
    reading = _Reading(client)
    assert len(by_header(reading, "Activity Log")) == len(source.activity)
    assert len(by_header(reading, "Daily Summary")) == len(source.daily)
    assert dash(reading)["Last refresh status"][0] == "OK"
    assert exporter.status().missing == ()
    assert set(REQUIRED_TABS) <= {t.title for t in client.get_metadata().tabs}
