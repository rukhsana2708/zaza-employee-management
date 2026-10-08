"""Device enrollment for the installed agent (Phase 9).

Secret and non-secret configuration are kept apart:

- ``config.json`` (data directory) — server URL, device ID, employee ID,
  enrolment time, optional privacy exclusions. No secret, ever.
- the device token — only in the DPAPI-protected credentials file
  (``sync/credentials.py``, Windows current-user scope), written atomically.

Enrollment reuses the Phase 3 device authentication: the token is checked
against ``GET /api/v1/devices/me`` before anything is saved, and the
employee ID comes from the server's answer. The token is never accepted as a
command-line argument, never logged and never shown again.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import paths
from .config import AgentConfig
from .sync.credentials import (
    CredentialsError,
    DeviceCredentials,
    load_credentials,
    save_credentials,
)
from .sync.transport import (
    AuthError,
    NetworkError,
    SyncTransport,
    SyncTransportError,
    validate_base_url,
)

CONFIG_VERSION = 1
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


class EnrollmentError(ValueError):
    """A problem the employee can fix; the message is safe to show."""


@dataclass(frozen=True)
class Enrollment:
    server_url: str
    device_id: str
    employee_id: str
    enrolled_at: str = ""
    excluded_apps: tuple[str, ...] = ()
    hidden_app_names: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    version: int = CONFIG_VERSION

    @property
    def server_host(self) -> str:
        return urlsplit(self.server_url).hostname or ""

    def agent_config(self, db_path: str | None = None) -> AgentConfig:
        """The recorder/sync configuration of the installed agent. Plain HTTP
        beyond loopback is never allowed here (no override exists)."""
        return AgentConfig(
            employee_id=self.employee_id, device_id=self.device_id,
            db_path=db_path or str(paths.db_path()), sync_url=self.server_url,
            excluded_apps=self.excluded_apps, hidden_app_names=self.hidden_app_names,
            excluded_domains=self.excluded_domains, sync_allow_insecure_http=False,
        )


def validate_server_url(url: str) -> str:
    """HTTPS for every server; plain HTTP only to loopback (local testing).
    TLS certificates are always verified — there is no option to turn that off."""
    url = (url or "").strip()
    if not url:
        raise EnrollmentError("Enter the server address, e.g. https://zaza.example.com")
    parts = urlsplit(url)
    if parts.scheme == "http" and (parts.hostname or "") not in _LOOPBACK:
        raise EnrollmentError("The server address must start with https:// (plain http is only allowed for "
                              "127.0.0.1 / localhost during testing).")
    try:
        return validate_base_url(url, allow_insecure=False)
    except ValueError:
        raise EnrollmentError("The server address is not valid. Use the https:// address you were given.") from None


def validate_device_id(device_id: str) -> str:
    device_id = (device_id or "").strip()
    if not device_id or len(device_id) > 128 or not set(device_id) <= _ID_CHARS:
        raise EnrollmentError("Enter the device ID you were given (letters, digits, '.', '_', ':' or '-').")
    return device_id


def load_enrollment(path: Path | None = None) -> Enrollment | None:
    path = path or paths.config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Enrollment(
            server_url=str(raw["server_url"]), device_id=str(raw["device_id"]), employee_id=str(raw["employee_id"]),
            enrolled_at=str(raw.get("enrolled_at", "")),
            excluded_apps=tuple(raw.get("excluded_apps", ())), hidden_app_names=tuple(raw.get("hidden_app_names", ())),
            excluded_domains=tuple(raw.get("excluded_domains", ())),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_enrollment(enrollment: Enrollment, path: Path | None = None) -> Path:
    path = path or paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(enrollment)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def current_state() -> tuple[Enrollment | None, bool]:
    """(enrollment, credentials usable for it)."""
    enrollment = load_enrollment()
    if enrollment is None:
        return None, False
    try:
        creds = load_credentials()
    except CredentialsError:
        return enrollment, False
    return enrollment, creds is not None and creds.device_id == enrollment.device_id


@dataclass
class EnrollResult:
    ok: bool
    message: str
    enrollment: Enrollment | None = None
    warnings: list[str] = field(default_factory=list)


TransportFactory = Callable[[str, DeviceCredentials], SyncTransport]


def _default_transport(url: str, creds: DeviceCredentials) -> SyncTransport:
    return SyncTransport(url, creds, timeout_seconds=15.0, allow_insecure=False)


def pending_records(db_path: Path | None = None) -> int:
    """Unsynced records in the local database (0 if there is none)."""
    db = db_path or paths.db_path()
    if not db.exists():
        return 0
    from .storage import ActivityStore  # noqa: PLC0415

    store = ActivityStore(str(db))
    try:
        counts = store.sync_counts()
        return counts["pending"] + counts["failed"] + counts["open"]
    finally:
        store.close()


def enroll(server_url: str, device_id: str, token: str, *, transport_factory: TransportFactory = _default_transport,
           allow_device_change: bool = False, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
           ) -> EnrollResult:
    """Verify the credentials with the server, then store them. Nothing is
    saved unless the server accepts the token for exactly this device."""
    url = validate_server_url(server_url)
    device_id = validate_device_id(device_id)
    token = (token or "").strip()
    if not token:
        raise EnrollmentError("Enter the device token you were given.")
    previous = load_enrollment()
    if previous and previous.device_id != device_id and not allow_device_change:
        unsynced = pending_records()
        raise EnrollmentError(
            f"This computer is already enrolled as device {previous.device_id}. Enrolling it as a different device "
            f"is a deliberate change" + (f": {unsynced} unsynced record(s) belong to {previous.device_id} and will "
                                          "not be uploaded under the new device." if unsynced else ".")
            + " Confirm the change to continue.")
    creds = DeviceCredentials(device_id=device_id, token=token)
    transport = transport_factory(url, creds)
    try:
        info = transport.check_device()
    except AuthError:
        return EnrollResult(False, "Device token was not accepted. Check the device ID and token you were given.")
    except NetworkError:
        return EnrollResult(False, "Could not reach the server. Check the address and the network connection.")
    except SyncTransportError:
        return EnrollResult(False, "The server's answer was not understood. Check that the address is the ZaZa "
                                   "server.")
    finally:
        transport.close()
    if info.device_id != device_id:
        return EnrollResult(False, "The server registered this token for a different device ID.")
    save_credentials(creds)  # DPAPI, atomic replace
    enrollment = Enrollment(url, device_id, info.employee_id, now().isoformat(timespec="seconds"),
                            *(getattr(previous, k) if previous else () for k in
                              ("excluded_apps", "hidden_app_names", "excluded_domains")))
    save_enrollment(enrollment)  # written last: config never points at missing credentials
    return EnrollResult(True, "Connection successful. This device is enrolled.", enrollment)
