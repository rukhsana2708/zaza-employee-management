"""Shared browser/JSON SaaS session, login and logout mechanics. No second auth system."""

from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool

from .. import http_security
from ..dashboard.security import new_token, same
from . import domain
from .domain import TenancyError
from .resolver import TenantAccessDenied
from .saas_auth import LOGIN_COOKIE, LOGIN_PATH, SESSION_COOKIE, SESSION_PATH, LoginRateLimited

LOGIN_FAILED = "invalid email or password"
MAX_FORM_BYTES = 8 * 1024
BROWSER_LOGIN_COOKIE = "zaza_browser_login"
BROWSER_LOGIN_PATH = "/app/sign-in"


class WebAuthError(Exception):
    def __init__(self, status, message, headers=None):
        self.status, self.message, self.headers = status, message, headers or {}
        super().__init__(message)


async def form(request):
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise ValueError("unsupported form encoding")
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise ValueError("form too large")
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=10)
    return {k: v[0] for k, v in parsed.items() if len(v) == 1}


class SaasWebAccess:
    def __init__(self, runtime):
        self.runtime, self.auth = runtime, runtime.auth

    def session(self, request):
        return self.auth.validate_session(request.cookies.get(SESSION_COOKIE))

    def member_context(self, ctx, slug):
        try:
            org_id = self.runtime.control.find_organization_id_by_slug(domain.normalize_slug(slug))
        except TenancyError:
            org_id = None
        if org_id is None:
            raise TenantAccessDenied()
        return self.runtime.resolver.for_member(ctx.user_id, org_id)

    def organizations(self, ctx):
        rows = []
        for member in self.runtime.control.list_memberships_for_user(ctx.user_id):
            try:
                tenant = self.runtime.resolver.for_member(ctx.user_id, member.organization_id)
            except TenantAccessDenied:
                continue
            org = self.runtime.control.get_organization(tenant.organization_id)
            rows.append({"slug": tenant.slug, "name": org.display_name, "role": tenant.role.value})
        return sorted(rows, key=lambda r: r["slug"])

    def challenge(self, response, *, browser=False, token=None):
        token = token or new_token()
        response.set_cookie(
            BROWSER_LOGIN_COOKIE if browser else LOGIN_COOKIE,
            token,
            max_age=900,
            path=BROWSER_LOGIN_PATH if browser else LOGIN_PATH,
            httponly=True,
            samesite="strict",
            secure=self.auth.settings.cookie_secure,
        )
        return token

    async def login(self, request, *, browser=False):
        try:
            data = await form(request)
        except ValueError:
            raise WebAuthError(400, LOGIN_FAILED) from None
        cookie = request.cookies.get(BROWSER_LOGIN_COOKIE if browser else LOGIN_COOKIE) or ""
        if (
            not http_security.same_origin(request)
            or not cookie
            or not same(cookie, data.get("login_token", ""))
        ):
            raise WebAuthError(403, "the login form expired; reload and try again")
        try:
            result = await run_in_threadpool(
                self.auth.login, data.get("email", ""), data.get("password", "")
            )
        except LoginRateLimited as exc:
            raise WebAuthError(
                429,
                "too many sign-in attempts; try again later",
                {"Retry-After": str(exc.retry_after)},
            ) from None
        if result is None:
            raise WebAuthError(401, LOGIN_FAILED)
        return result

    def accept_session(self, response, token, *, browser=False):
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=self.auth.settings.session_hours * 3600,
            path=SESSION_PATH,
            httponly=True,
            samesite="strict",
            secure=self.auth.settings.cookie_secure,
        )
        response.delete_cookie(
            BROWSER_LOGIN_COOKIE if browser else LOGIN_COOKIE,
            path=BROWSER_LOGIN_PATH if browser else LOGIN_PATH,
        )

    async def logout(self, request):
        ctx = await run_in_threadpool(self.session, request)
        if ctx is None:
            raise WebAuthError(401, "not signed in")
        try:
            data = await form(request)
        except ValueError:
            data = {}
        submitted = data.get("csrf_token") or request.headers.get("x-csrf-token")
        if not http_security.same_origin(request) or not ctx.csrf_ok(submitted):
            raise WebAuthError(403, "forbidden")
        await run_in_threadpool(self.auth.logout, ctx)
