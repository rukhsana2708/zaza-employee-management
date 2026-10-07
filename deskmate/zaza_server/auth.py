"""Per-device bearer-token authentication.

- Tokens are 256-bit random secrets (``secrets.token_urlsafe(32)``) with a
  recognizable ``zzd_`` prefix (helps secret scanners). The server stores
  only their SHA-256 hash; a high-entropy random token doesn't need a slow
  password hash.
- Every request carries ``Authorization: Bearer <token>`` *and*
  ``X-ZaZa-Device-Id``; the token must belong to that device. Nothing secret
  ever goes in a URL.
- A device can hold several ACTIVE tokens, so rotation is: issue new token
  -> agent switches -> revoke the old ones. Revoked tokens and DISABLED
  devices are rejected (403).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from .repository import CentralRepository, DeviceRecord

TOKEN_PREFIX = "zzd_"


@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    token: str  # shown once at issue time; never stored or logged


class AuthError(Exception):
    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(repo: CentralRepository, device_id: str) -> IssuedToken:
    if repo.get_device(device_id) is None:
        raise KeyError(device_id)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = str(uuid.uuid4())
    repo.add_token(token_id, device_id, hash_token(token))
    return IssuedToken(token_id=token_id, token=token)


def register_device(repo: CentralRepository, device_id: str, employee_id: str) -> IssuedToken:
    repo.add_device(device_id, employee_id)
    return issue_token(repo, device_id)


def rotate_token(repo: CentralRepository, device_id: str, *, revoke_old: bool = False) -> IssuedToken:
    issued = issue_token(repo, device_id)
    if revoke_old:
        repo.revoke_tokens(device_id, except_token_id=issued.token_id)
    return issued


def authenticate(repo: CentralRepository, authorization: str | None, device_header: str | None) -> DeviceRecord:
    """Return the authenticated device or raise :class:`AuthError`.

    401: missing/malformed/unknown credentials or token/device mismatch.
    403: valid token but revoked, or device disabled."""
    if not authorization or not device_header:
        raise AuthError(401, "missing credentials")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(401, "malformed authorization header")
    record = repo.find_token(hash_token(token.strip()))
    if record is None or not hmac.compare_digest(record.device_id.encode(), device_header.encode()):
        raise AuthError(401, "invalid credentials")
    if record.status != "ACTIVE":
        raise AuthError(403, "token revoked")
    device = repo.get_device(record.device_id)
    if device is None:
        raise AuthError(401, "invalid credentials")
    if device.status != "ACTIVE":
        raise AuthError(403, "device disabled")
    return device
