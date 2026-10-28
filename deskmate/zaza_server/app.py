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

Two applications share these routes and differ only in how a request finds
its device and repository:

- :func:`create_app` (legacy, single database: local development, tests,
  the Windows server): one :class:`CentralRepository`.
- :func:`create_saas_app` (Phase 10F-B): the device credential is routed to
  its organization in the control plane, then authenticated INSIDE that
  organization's tenant database, whose repository stays leased for the
  whole request (``tenancy/saas_sync.py``). Nothing the client sends selects
  a tenant.

The app contains no SQL. Repository calls are blocking (SQLite /
PostgreSQL), so they run in the thread pool, never on the event loop. If the
store is unreachable the API answers 503 (agents back off and retry; nothing
is lost on the device).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
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

from .auth import AuthError, authenticate_request
from .repository import CentralRepository, DeviceRecord, RepositoryUnavailable
from .service import SyncService, validation_message

logger = logging.getLogger("zaza_server.api")

# (Authorization header, X-ZaZa-Device-Id header) -> context manager yielding
# (authenticated device, sync service bound to THAT device's repository)
DeviceAccess = Callable[[str | None, str | None], AbstractContextManager[tuple[DeviceRecord, SyncService]]]


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


def _new_app() -> FastAPI:
    app = FastAPI(title="ZaZa Sync API", version="1", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RepositoryUnavailable)
    async def _unavailable(request: Request, exc: RepositoryUnavailable) -> JSONResponse:
        # The message is already free of credentials; keep details server-side.
        logger.error("storage unavailable: %s", exc)
        return _error(503, "storage unavailable", headers={"Retry-After": "30"})

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok", "api_version": "v1", "server_time": datetime.now(timezone.utc).isoformat()}

    return app


def _install_sync_api(app: FastAPI, access: DeviceAccess, max_body_bytes: int) -> None:
    @app.get(DEVICE_PATH)
    def device_me(request: Request):  # noqa: ANN202
        try:
            with access(request.headers.get("authorization"), request.headers.get(DEVICE_ID_HEADER)) as (device, _):
                return {"device_id": device.device_id, "employee_id": device.employee_id, "status": device.status}
        except AuthError as exc:
            return _error(exc.status_code, exc.error, headers={"WWW-Authenticate": "Bearer"})

    @app.post(BATCH_PATH)
    async def sync_batch(request: Request):  # noqa: ANN202
        stack = ExitStack()
        exc_info: tuple = (None, None, None)
        try:
            try:
                device, service = await run_in_threadpool(
                    stack.enter_context,
                    access(request.headers.get("authorization"), request.headers.get(DEVICE_ID_HEADER)))
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
        except BaseException:
            exc_info = sys.exc_info()
            raise
        finally:
            # leave the device's repository (a tenant lease in SaaS mode) with
            # the outcome, so a storage failure retires only that tenant's pool
            await run_in_threadpool(stack.__exit__, *exc_info)


def _single_repository_access(repo: CentralRepository) -> DeviceAccess:
    service = SyncService(repo)

    @contextmanager
    def access(authorization: str | None, device_header: str | None) -> Iterator[tuple[DeviceRecord, SyncService]]:
        ctx = authenticate_request(repo, authorization, device_header)
        try:
            # API connectivity only — never treated as employee activity.
            repo.touch_device(ctx.device.device_id, token_id=ctx.token_id)
        except RepositoryUnavailable:
            raise
        except Exception:  # noqa: BLE001 — bookkeeping must not fail an authenticated request
            logger.warning("could not update last_seen_at for %s", ctx.device.device_id)
        yield ctx.device, service

    return access


def create_app(repo: CentralRepository, *, max_body_bytes: int = MAX_BATCH_BYTES) -> FastAPI:
    """The legacy single-database sync API (unchanged behaviour)."""
    app = _new_app()
    app.state.repo = repo
    _install_sync_api(app, _single_repository_access(repo), max_body_bytes)
    return app


def create_saas_app(runtime, *, max_body_bytes: int = MAX_BATCH_BYTES) -> FastAPI:  # noqa: ANN001 — SaasRuntime
    """The multi-tenant SaaS application: the sync API with credential-derived
    tenant routing, plus the SaaS user session API under ``/app``
    (``tenancy/saas_web.py``). Health stays tenant-independent."""
    from .tenancy.saas_web import install_saas_web  # noqa: PLC0415

    app = _new_app()
    app.state.saas_runtime = runtime

    @contextmanager
    def access(authorization: str | None, device_header: str | None) -> Iterator[tuple[DeviceRecord, SyncService]]:
        with runtime.devices.access(authorization, device_header) as authenticated:
            yield authenticated.device, authenticated.service

    _install_sync_api(app, access, max_body_bytes)
    install_saas_web(app, runtime)
    return app
