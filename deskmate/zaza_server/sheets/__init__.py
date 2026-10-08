"""Phase 6 — one-way Google Sheets reporting from PostgreSQL.

PostgreSQL is the source of truth; the spreadsheet is a read-only view of
it. Nothing ever flows back: manual edits in the Sheet are overwritten at
the next refresh and are never read into the database.

- ``config.py``     environment settings, key-file validation, secret scrubbing
- ``models.py``     the five managed tabs and their columns; report data; window
- ``queries.py``    read-only PostgreSQL snapshot (activity periods + summaries)
- ``formatter.py``  rows → Sheet values and formatting (pure, deterministic)
- ``client.py``     SheetsClient interface; GoogleSheetsClient; FakeSheetsClient
- ``exporter.py``   sheets-init / sheets-sync / sheets-status

The Google libraries are an optional extra (``pip install -e .[zaza-sheets]``)
and are only imported by ``GoogleSheetsClient.from_settings``; the sync API
server never imports this package. Details: ARCHITECTURE.md §5.
"""

from .client import FakeSheetsClient, SheetsClient, SheetsUnavailable
from .config import SheetsConfigError, SheetsSettings, sheets_settings_from_env
from .exporter import SheetsExporter

__all__ = ["FakeSheetsClient", "SheetsClient", "SheetsConfigError", "SheetsExporter", "SheetsSettings",
           "SheetsUnavailable", "sheets_settings_from_env"]
