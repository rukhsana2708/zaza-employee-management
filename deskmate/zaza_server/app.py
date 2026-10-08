"""FastAPI application for the sync API.

Endpoints (all JSON):

- ``GET  /api/v1/sync/health`` — unauthenticated liveness probe; no data.
- ``GET  /api/v1/devices/me``  — authenticated credential check.
- ``POST /api/v1/sync/batch``  — authenticated batch upload with per-record
  results.

The request body is read with a hard byte cap *before* JSON parsing (413 if
exceeded), the envelope is validated strictly (422 on unknown fields or
non-UTC timestamps), and each record is validated individually by the
service. Authorization headers and tokens are never logged.

The app works with any :class:`CentralRepository`; it contains no SQL.
Repository calls are blocking (SQLite / PostgreSQL), so they run in the
thread pool, never on the event loop. If the store is unreachable the API
answers 503 (agents back off and retry; nothing is lost on the device).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from deskmate.zaza.sync.protocol import (
    BATCH_PATH,
    DEVICE_ID_HEADER,
    DEVICE_PATH,
    HEALTH_PATH,
    MAX_BATCH_BYTES,
    SyncBatchRequest,
)

from .auth import AuthContext, AuthError, authenticate_request
from .repository import CentralRepository, RepositoryUnavailable
from .service import SyncService, validation_message

logger = logging.getLogger("zaza_server.api")


class _BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise _BodyTooLarge
        except ValueError as exc:
            raise _BodyTooLarge from exc
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _error(status: int, error: str, detail: str | None = None, headers: dict | None = None) -> JSONResponse:
    body = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body, headers=headers)


def create_app(repo: CentralRepository, *, max_body_bytes: int = MAX_BATCH_BYTES) -> FastAPI:
    app = FastAPI(title="ZaZa Sync API", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    service = SyncService(repo)
    app.state.repo = repo

    @app.exception_handler(RepositoryUnavailable)
    async def _unavailable(request: Request, exc: RepositoryUnavailable) -> JSONResponse:
        # The message is already free of credentials; keep details server-side.
        logger.error("storage unavailable: %s", exc)
        return _error(503, "storage unavailable", headers={"Retry-After": "30"})

    def _auth(request: Request) -> AuthContext:
        ctx = authenticate_request(repo, request.headers.get("authorization"), request.headers.get(DEVICE_ID_HEADER))
        try:
            # API connectivity only — never treated as employee activity.
            repo.touch_device(ctx.device.device_id, token_id=ctx.token_id)
        except RepositoryUnavailable:
            raise
        except Exception:  # noqa: BLE001 — bookkeeping must not fail an authenticated request
            logger.warning("could not update last_seen_at for %s", ctx.device.device_id)
        return ctx

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok", "api_version": "v1", "server_time": datetime.now(timezone.utc).isoformat()}

    @app.get(DEVICE_PATH)
    def device_me(request: Request):  # noqa: ANN202
        try:
            device = _auth(request).device
        except AuthError as exc:
            return _error(exc.status_code, exc.error, headers={"WWW-Authenticate": "Bearer"})
        return {"device_id": device.device_id, "employee_id": device.employee_id, "status": device.status}

    @app.post(BATCH_PATH)
    async def sync_batch(request: Request):  # noqa: ANN202
        try:
            device = (await run_in_threadpool(_auth, request)).device
        except AuthError as exc:
            logger.warning("sync rejected: %s (device header %r)", exc.error,
                           (request.headers.get(DEVICE_ID_HEADER) or "")[:64])
            return _error(exc.status_code, exc.error, headers={"WWW-Authenticate": "Bearer"})
        try:
            body = await _read_body(request, max_body_bytes)
        except _BodyTooLarge:
            return _error(413, "payload too large", f"limit is {max_body_bytes} bytes")
        try:
            batch = SyncBatchRequest.model_validate_json(body)
        except ValidationError as exc:
            return _error(422, "invalid batch", validation_message(exc))
        if batch.device_id != device.device_id:
            return _error(422, "invalid batch", "batch device_id does not match the authenticated device")
        response = await run_in_threadpool(service.process_batch, device, batch)
        return JSONResponse(content=response.model_dump(mode="json"))

    return app
