"""SaaS user session API (Phase 10F-B), mounted by ``create_saas_app``.

JSON endpoints under ``/app`` (separate HTML routes are installed alongside):

- ``GET  /app/login``   issue the pre-session double-submit login token
  (cookie + body).
- ``POST /app/login``   form ``email``, ``password``, ``login_token``.
  Checks the same origin (``http_security.same_origin``), the double-submit
  token and the SHARED rate limit, then signs in. Every failure is the same
  401 "invalid email or password".
- ``GET  /app/session`` the signed-in USER, its CSRF token and the
  organizations it can actually open (each resolved through
  ``TenantResolver.for_member``).
- ``POST /app/logout``  session + CSRF token + same origin.
- ``GET  /app/orgs/{slug}``  slug -> immutable organization_id -> membership
  resolution. The slug is only a handle: an unknown slug and an organization
  without the user's membership give the SAME 404.
- ``GET  /app/orgs/{slug}/employees``  needs ``employees.view_all``. Reads
  ONLY the resolved tenant's repository.

- ``POST /api/v1/enroll``  (Phase 10G-A; only when the runtime has an
  enrollment service) JSON ``{enrollment_code, device_id, device_name?}``,
  at most 4 KiB, unknown fields refused. Returns the device token ONCE.
  The organization, tenant and employee come from the server-side
  enrollment record — never from the request.

No route takes an organization id, a database name or a host. No SQL lives
here (SaasAuthService, the control repository and the tenant repositories
own it).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..dashboard.security import SECURITY_HEADERS, new_token
from .resolver import TenantAccessDenied
from .saas_auth import (
    LOGIN_PATH,
    SESSION_COOKIE,
    SESSION_PATH,
    Permission,
    PermissionDenied,
    permissions,
    require,
)
from .web_access import LOGIN_FAILED as LOGIN_FAILED
from .web_access import MAX_FORM_BYTES as MAX_FORM_BYTES

ENROLL_PATH = "/api/v1/enroll"
MAX_ENROLL_BYTES = 4096


def _json(status: int, body: dict, headers: dict | None = None) -> JSONResponse:
    response = JSONResponse(status_code=status, content=body, headers=headers)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _error(status: int, error: str, headers: dict | None = None) -> JSONResponse:
    return _json(status, {"error": error}, headers)



def install_saas_web(app: FastAPI, runtime) -> None:  # noqa: ANN001, C901 — SaasRuntime
    from .web_access import SaasWebAccess, WebAuthError

    access = SaasWebAccess(runtime)
    session, member_context = access.session, access.member_context

    if getattr(runtime, "enrollment", None) is not None:
        _install_enrollment(app, runtime.enrollment)

    @app.get(LOGIN_PATH)
    def login_form():  # noqa: ANN202
        token = new_token()
        response = _json(200, {"login_token": token})
        access.challenge(response, token=token)
        return response

    @app.post(LOGIN_PATH)
    async def login(request: Request):
        try:
            token, ctx = await access.login(request)
        except WebAuthError as exc:
            return _error(exc.status, exc.message, exc.headers)
        response = _json(200, {"user_id": str(ctx.user_id), "csrf_token": ctx.csrf_token})
        access.accept_session(response, token)
        return response

    @app.get(f"{SESSION_PATH}/session")
    def whoami(request: Request):  # noqa: ANN202
        ctx = session(request)
        if ctx is None:
            return _error(401, "not signed in")
        organizations = []
        for membership in runtime.control.list_memberships_for_user(ctx.user_id):
            try:
                tenant = runtime.resolver.for_member(ctx.user_id, membership.organization_id)
            except TenantAccessDenied:
                continue  # inactive membership/organization or tenant not READY: not offered
            organizations.append({"slug": tenant.slug, "role": tenant.role.value})
        return _json(200, {"user": {"user_id": str(ctx.user_id), "email": ctx.user.email,
                                    "display_name": ctx.user.display_name},
                           "csrf_token": ctx.csrf_token, "organizations": sorted(organizations, key=lambda o: o["slug"])})

    @app.post(f"{SESSION_PATH}/logout")
    async def logout(request: Request):  # noqa: ANN202
        try:
            await access.logout(request)
        except WebAuthError as exc:
            return _error(exc.status, exc.message, exc.headers)
        response = _json(200, {"signed_out": True})
        response.delete_cookie(SESSION_COOKIE, path=SESSION_PATH)
        return response

    @app.get(f"{SESSION_PATH}/orgs/{{slug}}")
    def organization(request: Request, slug: str):  # noqa: ANN202
        ctx = session(request)
        if ctx is None:
            return _error(401, "not signed in")
        try:
            tenant = member_context(ctx, slug)
        except TenantAccessDenied:
            return _error(404, "not found")
        return _json(200, {"organization": tenant.slug, "role": tenant.role.value,
                           "permissions": sorted(p.value for p in permissions(tenant))})

    @app.get(f"{SESSION_PATH}/orgs/{{slug}}/employees")
    def employees(request: Request, slug: str):  # noqa: ANN202
        ctx = session(request)
        if ctx is None:
            return _error(401, "not signed in")
        try:
            tenant = member_context(ctx, slug)
        except TenantAccessDenied:
            return _error(404, "not found")
        try:
            require(tenant, Permission.EMPLOYEES_VIEW_ALL)
        except PermissionDenied:
            return _error(403, "forbidden")
        with runtime.manager.repository(tenant) as repo:
            rows = repo.list_employees()
        return _json(200, {"employees": [{"employee_id": e.employee_id, "display_name": e.display_name,
                                          "is_active": e.is_active} for e in rows]})

    from ..workforce_dashboard.routes import install_browser

    install_browser(app, runtime, access)


def _install_enrollment(app: FastAPI, enrollment) -> None:  # noqa: ANN001 — EnrollmentService
    from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: PLC0415
    from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

    from .enrollment import EnrollmentRateLimited, EnrollmentRefused  # noqa: PLC0415

    class EnrollRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        enrollment_code: str = Field(min_length=1, max_length=64)
        device_id: str = Field(min_length=1, max_length=128)
        device_name: str | None = Field(default=None, max_length=200)

    @app.post(ENROLL_PATH)
    async def enroll(request: Request):  # noqa: ANN202
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_ENROLL_BYTES):
            return _error(413, "request too large")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_ENROLL_BYTES:
                return _error(413, "request too large")
        try:
            data = EnrollRequest.model_validate_json(body)
        except ValidationError:
            return _error(422, "invalid enrollment request")
        try:
            enrolled = await run_in_threadpool(enrollment.enroll, data.enrollment_code, data.device_id,
                                               data.device_name)
        except EnrollmentRateLimited as exc:
            return _error(429, "too many enrollment attempts; try again later", {"Retry-After": str(exc.retry_after)})
        except EnrollmentRefused as exc:
            return _error(exc.status_code, exc.error)
        # the token is in this response ONLY: never logged, never stored
        return _json(200, {"device_id": enrolled.device_id, "employee_id": enrolled.employee_id,
                           "device_token": enrolled.token, "api_version": "v1"})
