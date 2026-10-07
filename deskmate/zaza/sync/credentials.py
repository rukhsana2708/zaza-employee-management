"""Device credentials for the sync API.

Stored in ``<ZAZA_HOME>/device_credentials.json``, separate from the SQLite
activity database. On Windows the token is encrypted with DPAPI
(``CryptProtectData``, current-user scope), so the file is useless if copied
to another account or machine. Elsewhere (development/tests only) it is
stored with ``"protection": "none"`` and owner-only file permissions.

The token never appears in ``repr()``, logs, URLs, or the database.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths

CREDENTIALS_FILE = "device_credentials.json"
_DPAPI_DESCRIPTION = "ZaZa device token"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialsError(Exception):
    pass


@dataclass(frozen=True)
class DeviceCredentials:
    device_id: str
    token: str = field(repr=False)


def credentials_path() -> Path:
    return paths.root() / CREDENTIALS_FILE


# ─── DPAPI (Windows) ───────────────────────────────────────────────────────


def _dpapi_available() -> bool:
    return os.name == "nt"


def _blob_type():  # noqa: ANN202
    import ctypes.wintypes as wt  # noqa: PLC0415

    class DATA_BLOB(ctypes.Structure):  # noqa: N801
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    return DATA_BLOB


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    DATA_BLOB = _blob_type()  # noqa: N806
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if protect:
        ok = fn(ctypes.byref(blob_in), _DPAPI_DESCRIPTION, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise CredentialsError(f"DPAPI {'protect' if protect else 'unprotect'} failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


# ─── load / save ───────────────────────────────────────────────────────────


def save_credentials(creds: DeviceCredentials, path: Path | None = None, *, protection: str | None = None) -> Path:
    path = path or credentials_path()
    protection = protection or ("dpapi" if _dpapi_available() else "none")
    if protection == "dpapi":
        token_field = base64.b64encode(_dpapi(creds.token.encode("utf-8"), protect=True)).decode("ascii")
    elif protection == "none":
        token_field = creds.token
    else:
        raise ValueError(f"unknown protection: {protection}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "device_id": creds.device_id, "protection": protection, "token": token_field}),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def load_credentials(path: Path | None = None) -> DeviceCredentials | None:
    """None if no credentials file exists; CredentialsError if it is unusable."""
    path = path or credentials_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        device_id = str(raw["device_id"])
        protection = raw.get("protection")
        token_field = str(raw["token"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CredentialsError(f"credentials file is unreadable: {type(exc).__name__}") from None
    if protection == "dpapi":
        if not _dpapi_available():
            raise CredentialsError("credentials are DPAPI-protected but DPAPI is unavailable")
        token = _dpapi(base64.b64decode(token_field), protect=False).decode("utf-8")
    elif protection == "none":
        token = token_field
    else:
        raise CredentialsError(f"unknown credentials protection: {protection!r}")
    if not device_id or not token:
        raise CredentialsError("credentials file is incomplete")
    return DeviceCredentials(device_id=device_id, token=token)


def delete_credentials(path: Path | None = None) -> bool:
    path = path or credentials_path()
    if path.exists():
        path.unlink()
        return True
    return False
