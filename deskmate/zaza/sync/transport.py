"""HTTP transport for the sync API (httpx).

Every failure is mapped to one :class:`SyncTransportError` subclass so the
worker can choose the right retry policy. No local data is touched here.

- :class:`NetworkError`   DNS failure, connection refused, timeouts, TLS and
                          other transport errors -> OFFLINE, backoff.
- :class:`AuthError`      401/403 -> AUTH_ERROR, long fixed retry interval.
- :class:`RateLimited`    429 (honours Retry-After) -> backoff.
- :class:`ServerError`    5xx -> backoff.
- :class:`PayloadTooLarge` 413 -> worker shrinks the batch.
- :class:`ProtocolError`  other statuses, malformed JSON, schema mismatch,
                          or a response for a different batch.

Credentials go only in headers (``Authorization: Bearer`` +
``X-ZaZa-Device-Id``) and never into exception messages or logs. Plain HTTP
is refused except to localhost, unless explicitly allowed for development.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .. import __version__
from .credentials import DeviceCredentials
from .protocol import BATCH_PATH, DEVICE_ID_HEADER, DEVICE_PATH, DeviceInfoResponse, SyncBatchResponse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class SyncTransportError(Exception):
    kind = "error"


class NetworkError(SyncTransportError):
    kind = "network"


class AuthError(SyncTransportError):
    kind = "auth"

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimited(SyncTransportError):
    kind = "rate_limited"

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(SyncTransportError):
    kind = "server"

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class PayloadTooLarge(SyncTransportError):
    kind = "too_large"


class ProtocolError(SyncTransportError):
    kind = "protocol"


def validate_base_url(url: str, *, allow_insecure: bool = False) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("sync URL must be an http(s) URL with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("sync URL must not contain credentials, a query string, or a fragment")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS and not allow_insecure:
        raise ValueError("plain HTTP is only allowed to localhost; production must use HTTPS")
    return url.rstrip("/")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class SyncTransport:
    def __init__(
        self,
        base_url: str,
        credentials: DeviceCredentials,
        *,
        timeout_seconds: float = 15.0,
        allow_insecure: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = validate_base_url(base_url, allow_insecure=allow_insecure)
        self._credentials = credentials
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials.token}",
            DEVICE_ID_HEADER: self._credentials.device_id,
            "User-Agent": f"zaza-agent/{__version__}",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
        try:
            return self._client.request(method, self.base_url + path, json=json_body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise NetworkError(f"timeout: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            # ConnectError covers DNS failure, refused connections and TLS
            # handshake errors. Only the class name and httpx's message
            # (which never contains our headers) are kept.
            raise NetworkError(f"{type(exc).__name__}: {str(exc)[:200]}") from None

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        if status in (401, 403):
            raise AuthError(status, f"HTTP {status}: {_error_text(response)}")
        if status == 429:
            raise RateLimited("HTTP 429", _retry_after_seconds(response.headers.get("retry-after")))
        if status == 413:
            raise PayloadTooLarge("HTTP 413")
        if status >= 500:
            raise ServerError(status, f"HTTP {status}")
        raise ProtocolError(f"HTTP {status}: {_error_text(response)}")

    def send_batch(self, payload: dict) -> SyncBatchResponse:
        response = self._request("POST", BATCH_PATH, json_body=payload)
        self._raise_for_status(response)
        try:
            parsed = SyncBatchResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise ProtocolError(f"malformed batch response ({exc.error_count()} errors)") from None
        if str(parsed.batch_id) != str(payload["batch_id"]):
            raise ProtocolError("response is for a different batch")
        return parsed

    def check_device(self) -> DeviceInfoResponse:
        response = self._request("GET", DEVICE_PATH)
        self._raise_for_status(response)
        try:
            return DeviceInfoResponse.model_validate_json(response.content)
        except ValidationError:
            raise ProtocolError("malformed device response") from None


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error", ""))[:200] if isinstance(body, dict) else ""
