"""Separate SaaS HTML surface. Existing /app JSON contracts remain unchanged."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import ChoiceLoader, FileSystemLoader

from ..dashboard.presentation import _templates
from ..dashboard.security import SECURITY_HEADERS, new_token
from ..dashboard.service import NotFound
from ..repository import RepositoryUnavailable
from ..tenancy.domain import TenancyError
from ..tenancy.resolver import TenantAccessDenied
from ..tenancy.saas_auth import (
    SESSION_COOKIE,
    SESSION_PATH,
    Permission,
    PermissionDenied,
    permissions,
)
from ..tenancy.web_access import BROWSER_LOGIN_PATH, WebAuthError
from .service import BrowserDashboardService

HERE = Path(__file__).parent
NAV = (
    ("workspace", "Overview"),
    ("workforce", "Workforce"),
    ("attendance", "Attendance"),
    ("applications", "Applications"),
    ("analytics", "Analytics"),
)


def install_browser(app, runtime, access):
    env = _templates()
    env.loader = ChoiceLoader([FileSystemLoader(str(HERE / "templates")), env.loader])
    reporting = BrowserDashboardService(runtime)
    app.mount("/app/assets", StaticFiles(directory=str(HERE / "static")), name="saas-assets")
    app.mount(
        "/app/report-assets",
        StaticFiles(directory=str(HERE.parent / "dashboard" / "static")),
        name="report-assets",
    )

    @app.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        if request.url.path == "/app" or request.url.path.startswith("/app/"):
            for name, value in SECURITY_HEADERS.items():
                response.headers[name] = value
        return response

    def render(template, *, ctx=None, tenant=None, status=200, headers=None, **values):
        can_read = bool(
            tenant
            and (
                {
                    Permission.EMPLOYEES_VIEW_ALL,
                    Permission.EMPLOYEES_VIEW_SELF,
                    Permission.EMPLOYEES_VIEW_SCOPED,
                }
                & permissions(tenant)
            )
        )
        return HTMLResponse(
            env.get_template(template).render(
                user=ctx.user if ctx else None,
                csrf=ctx.csrf_token if ctx else "",
                tenant=tenant,
                nav=NAV if can_read else (),
                **values,
            ),
            status_code=status,
            headers=headers,
        )

    def sign_in_page(error=None, status=200, headers=None):
        token = new_token()
        response = render(
            "sign_in.html", error=error, login_token=token, status=status, headers=headers
        )
        access.challenge(response, browser=True, token=token)
        return response

    @app.get(BROWSER_LOGIN_PATH, response_class=HTMLResponse)
    def sign_in():
        return sign_in_page()

    @app.post(BROWSER_LOGIN_PATH, response_class=HTMLResponse)
    async def browser_login(request: Request):
        try:
            token, _ = await access.login(request, browser=True)
        except WebAuthError as exc:
            return sign_in_page(exc.message, exc.status, exc.headers)
        response = RedirectResponse("/app/organizations", status_code=303)
        access.accept_session(response, token, browser=True)
        return response

    @app.post("/app/sign-out")
    async def browser_logout(request: Request):
        try:
            await access.logout(request)
        except WebAuthError as exc:
            return render("error.html", message=exc.message, status=exc.status, headers=exc.headers)
        response = RedirectResponse(BROWSER_LOGIN_PATH, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path=SESSION_PATH)
        return response

    @app.get("/app/organizations", response_class=HTMLResponse)
    def organizations(request: Request):
        ctx = access.session(request)
        if ctx is None:
            return RedirectResponse(BROWSER_LOGIN_PATH, status_code=303)
        return render("organizations.html", ctx=ctx, organizations=access.organizations(ctx))

    def workforce_page(request, slug, page, employee_id=None):
        ctx = access.session(request)
        if ctx is None:
            return RedirectResponse(BROWSER_LOGIN_PATH, status_code=303)
        try:
            tenant = access.member_context(ctx, slug)
        except TenantAccessDenied:
            return render("error.html", ctx=ctx, message="Not found.", status=404)
        if page == "workspace" and not (
            {
                Permission.EMPLOYEES_VIEW_ALL,
                Permission.EMPLOYEES_VIEW_SELF,
                Permission.EMPLOYEES_VIEW_SCOPED,
            }
            & permissions(tenant)
        ):
            return render("landing.html", ctx=ctx, tenant=tenant)
        try:
            view = reporting.view(tenant, page, request.query_params, employee_id)
        except (PermissionDenied, NotFound):
            return render(
                "error.html",
                ctx=ctx,
                tenant=tenant,
                message="Not found."
                if employee_id
                else "Workforce reporting is not available for this account.",
                status=404 if employee_id else 403,
            )
        except TenantAccessDenied:
            return render("error.html", ctx=ctx, message="Not found.", status=404)
        except TenancyError:
            return render(
                "error.html",
                ctx=ctx,
                tenant=tenant,
                message="Workforce reporting is not available.",
                status=503,
            )
        except RepositoryUnavailable:
            return render(
                "error.html",
                ctx=ctx,
                tenant=tenant,
                message="Reporting is temporarily unavailable.",
                status=503,
            )
        except ValueError:
            return render(
                "error.html",
                ctx=ctx,
                tenant=tenant,
                message="Choose a valid reporting period.",
                status=400,
            )
        return render(
            "employee.html" if employee_id else "report.html",
            ctx=ctx,
            tenant=tenant,
            page=page,
            view=view,
            f={
                "period": request.query_params.get("period", "today"),
                "employee": request.query_params.get("employee", ""),
                "from": request.query_params.get("from", ""),
                "to": request.query_params.get("to", ""),
            },
        )

    for page, _ in NAV:

        def endpoint(page_name):
            def get(request: Request, slug: str):
                return workforce_page(request, slug, page_name)

            return get

        app.add_api_route(
            "/app/orgs/{slug}/" + page,
            endpoint(page),
            methods=["GET"],
            response_class=HTMLResponse,
            name="saas-" + page,
        )

    @app.get("/app/orgs/{slug}/workforce/{employee_id}", response_class=HTMLResponse)
    def employee_detail(request: Request, slug: str, employee_id: str):
        return workforce_page(request, slug, "workforce", employee_id)
