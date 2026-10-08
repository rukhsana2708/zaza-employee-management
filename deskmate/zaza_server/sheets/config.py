"""Google Sheets export configuration (environment only).

``ZAZA_GOOGLE_SHEET_ID``              the target spreadsheet's ID (the part of
                                      its URL between ``/d/`` and ``/edit``)
``ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE``  path to the service-account JSON key
``ZAZA_SHEETS_ACTIVITY_DAYS``         Activity Log history: the last N local
                                      dates per employee (default 30, 1..366)
``ZAZA_SHEETS_SUMMARY_MONTHS``        Daily/Weekly/Monthly tabs: the current
                                      month and the N-1 before it (default 12;
                                      0 = all history, max 120)
``ZAZA_SHEETS_TIMEZONE``              IANA zone for team-level Dashboard times
                                      (default: the employees' common zone,
                                      or UTC if they differ)
``ZAZA_GOOGLE_API_TIMEOUT_SECONDS``   per-request timeout (default 30, 5..300)

These settings only control what is *displayed*; nothing is ever deleted
from PostgreSQL.

Secrets: the key file is read only to build credentials. Its contents are
never logged, stored, copied into PostgreSQL or put in an exception message;
:class:`Scrubber` removes key material, access tokens and the full
spreadsheet ID from any text that might be shown.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import ConfigError

ENV_SHEET_ID = "ZAZA_GOOGLE_SHEET_ID"
ENV_SERVICE_ACCOUNT_FILE = "ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE"
ENV_ACTIVITY_DAYS = "ZAZA_SHEETS_ACTIVITY_DAYS"
ENV_SUMMARY_MONTHS = "ZAZA_SHEETS_SUMMARY_MONTHS"
ENV_TIMEZONE = "ZAZA_SHEETS_TIMEZONE"
ENV_TIMEOUT = "ZAZA_GOOGLE_API_TIMEOUT_SECONDS"

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)  # this spreadsheet API only; no Drive scope
_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_MAX_KEY_FILE_BYTES = 64 * 1024


class SheetsConfigError(ConfigError):
    """Invalid Sheets configuration. Messages never contain key material."""


def mask_id(spreadsheet_id: str) -> str:
    """``1AbC…xYz9``: enough to recognise the spreadsheet, not to open it."""
    if len(spreadsheet_id) <= 10:
        return "***"
    return f"{spreadsheet_id[:4]}…{spreadsheet_id[-4:]}"


@dataclass(frozen=True)
class SheetsSettings:
    spreadsheet_id: str = field(repr=False)
    service_account_file: Path
    activity_days: int = 30
    summary_months: int = 12
    report_timezone: str | None = None
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not _SHEET_ID_RE.match(self.spreadsheet_id or ""):
            hint = ""
            if "docs.google.com" in (self.spreadsheet_id or "") or "/" in (self.spreadsheet_id or ""):
                hint = " — use only the ID (the part of the URL between /d/ and /edit), not the whole URL"
            raise SheetsConfigError(f"{ENV_SHEET_ID} is not a valid spreadsheet ID{hint}")
        if not 1 <= self.activity_days <= 366:
            raise SheetsConfigError(f"{ENV_ACTIVITY_DAYS} must be 1..366")
        if not 0 <= self.summary_months <= 120:
            raise SheetsConfigError(f"{ENV_SUMMARY_MONTHS} must be 0..120 (0 = all history)")
        if not 5 <= self.timeout_seconds <= 300:
            raise SheetsConfigError(f"{ENV_TIMEOUT} must be 5..300")
        if self.report_timezone is not None:
            try:
                ZoneInfo(self.report_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise SheetsConfigError(f"{ENV_TIMEZONE} is not a known IANA time zone") from None

    @property
    def masked_id(self) -> str:
        return mask_id(self.spreadsheet_id)

    def __repr__(self) -> str:
        return (f"SheetsSettings(spreadsheet={self.masked_id}, activity_days={self.activity_days}, "
                f"summary_months={self.summary_months}, report_timezone={self.report_timezone})")


def _int(env: dict, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SheetsConfigError(f"{name} must be a whole number") from None


def sheets_settings_from_env(env: dict | None = None) -> SheetsSettings:
    env = dict(os.environ if env is None else env)
    sheet_id = (env.get(ENV_SHEET_ID) or "").strip()
    key_file = (env.get(ENV_SERVICE_ACCOUNT_FILE) or "").strip()
    if not sheet_id:
        raise SheetsConfigError(f"{ENV_SHEET_ID} is not set (the target spreadsheet's ID)")
    if not key_file:
        raise SheetsConfigError(f"{ENV_SERVICE_ACCOUNT_FILE} is not set (path to the service-account JSON key)")
    return SheetsSettings(
        spreadsheet_id=sheet_id,
        service_account_file=Path(key_file).expanduser(),
        activity_days=_int(env, ENV_ACTIVITY_DAYS, 30),
        summary_months=_int(env, ENV_SUMMARY_MONTHS, 12),
        report_timezone=(env.get(ENV_TIMEZONE) or "").strip() or None,
        timeout_seconds=_int(env, ENV_TIMEOUT, 30),
    )


@dataclass(frozen=True)
class ServiceAccountKey:
    """A parsed key file. ``info`` holds the private key: pass it straight
    to google-auth and never log or store it (it is excluded from repr)."""

    client_email: str
    info: dict = field(repr=False)

    def secrets(self) -> list[str]:
        return [v for k in ("private_key", "private_key_id") if isinstance(v := self.info.get(k), str) and v]


def load_service_account_key(path: Path) -> ServiceAccountKey:
    """Read and validate the key file. Errors name the file and the problem,
    never its contents."""
    name = f"{ENV_SERVICE_ACCOUNT_FILE} ({path})"
    if not path.is_file():
        raise SheetsConfigError(f"{name}: file not found")
    try:
        raw = path.read_bytes()
    except OSError:
        raise SheetsConfigError(f"{name}: file can't be read") from None
    if len(raw) > _MAX_KEY_FILE_BYTES:
        raise SheetsConfigError(f"{name}: file is too large to be a service-account key")
    try:
        info = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise SheetsConfigError(f"{name}: not a valid JSON key file") from None
    if not isinstance(info, dict):
        raise SheetsConfigError(f"{name}: not a valid JSON key file")
    if info.get("type") != "service_account":
        if "installed" in info or "web" in info:
            raise SheetsConfigError(f"{name}: this is an OAuth client file, not a service-account key")
        raise SheetsConfigError(f"{name}: not a service-account key (\"type\" must be \"service_account\")")
    missing = [k for k in ("client_email", "private_key", "token_uri") if not isinstance(info.get(k), str) or not info[k]]
    if missing:
        raise SheetsConfigError(f"{name}: key file is missing {', '.join(missing)}")
    return ServiceAccountKey(client_email=info["client_email"], info=info)


_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL)
_TOKEN_RES = (
    (re.compile(r"ya29\.[\w.\-]+"), "***"),                      # OAuth access tokens
    (re.compile(r"(?i)(bearer\s+)[\w.\-~+/]+=*"), r"\1***"),
    (re.compile(r'(?i)("?(?:private_key(?:_id)?|access_token|refresh_token|assertion)"?\s*[:=]\s*"?)[^"\s,}&]+'),
     r"\1***"),
)


class Scrubber:
    """Removes secrets from text before it is shown or logged."""

    def __init__(self, spreadsheet_id: str | None = None, secrets: list[str] | None = None) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.secrets = [s for s in (secrets or []) if s]

    def __call__(self, text: str) -> str:
        text = _PEM_RE.sub("***", str(text))
        for secret in self.secrets:
            text = text.replace(secret, "***")
            text = text.replace(secret.replace("\n", "\\n"), "***")  # as it appears inside JSON
        for pattern, repl in _TOKEN_RES:
            text = pattern.sub(repl, text)
        if self.spreadsheet_id:
            text = text.replace(self.spreadsheet_id, mask_id(self.spreadsheet_id))
        return text
