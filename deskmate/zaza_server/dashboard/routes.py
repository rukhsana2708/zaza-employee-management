"""Manager dashboard web routes (server-rendered; mounted under /manager).

Every page and JSON endpoint needs a valid manager session; pages redirect
to /manager/login, JSON endpoints answer 401. Every POST needs the
session's CSRF token (and a same-origin Origin header when one is sent);
GET requests never change data. All responses under /manager carry
no-store caching and a strict Content-Security-Policy; no external scripts,
styles or fonts are used.

Routes only parse input and render: the logic is in ``service.py`` and
``auth.py``, the SQL in ``queries.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from ..attendance.labels import FLAG_NOTES, QUALITY_LABELS, STATUS_LABELS, STATUS_NOTES
from .auth import LOGIN_FAILED, ManagerAuth, SessionContext
from .config import DashboardSettings
from .models import STATUS_LABELS as CURRENT_LABELS
from .models import CurrentStatus, Selection
from .queries import DashboardRepository
from .security import COOKIE_NAME, COOKIE_PATH, LOGIN_COOKIE_NAME, SECURITY_HEADERS, new_token, same
from .service import PERIODS, DashboardService, NotFound, parse_hours

logger = logging.getLogger("zaza_server.dashboard")
HERE = Path(__file__).parent
MAX_FORM_BYTES = 64 * 1024
UTC = timezone.utc


# ─── template helpers (display formatting only) ───────────────────────────


def fmt_hours(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    total = int(round(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def fmt_minutes(seconds: int | None) -> str:
    return "–" if not seconds else f"{round(seconds / 60)} min"


def fmt_pct(value: float | None, *, fraction: bool = False) -> str:
    if value is None:
        return "–"
    return f"{value * 100 if fraction else value:.1f}%"


def fmt_local(moment: datetime | None, tz: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return "–" if moment is None else moment.astimezone(ZoneInfo(tz)).strftime(fmt)


def fmt_ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _templates():  # noqa: ANN202
    import jinja2  # noqa: PLC0415

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(HERE / "templates")),
        autoescape=True,  # every template, whatever its extension: database text is untrusted
        undefined=jinja2.StrictUndefined,
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update(hours=fmt_hours, duration=fmt_duration, minutes=fmt_minutes, pct=fmt_pct, local=fmt_local, ago=fmt_ago)
    env.globals.update(PERIODS=PERIODS, STATUS_LABELS=STATUS_LABELS, STATUS_NOTES=STATUS_NOTES,
                       FLAG_NOTES=FLAG_NOTES, QUALITY_LABELS=QUALITY_LABELS, CURRENT_LABELS=CURRENT_LABELS,
                       CurrentStatus=CurrentStatus, WEEKDAYS=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    return env


# ─── mounting ─────────────────────────────────────────────────────────────


def mount_dashboard(app: FastAPI, repo: DashboardRepository, settings: DashboardSettings | None = None,
                    clock: Callable[[], datetime] | None = None) -> None:
    settings = settings or DashboardSettings()
    clock = clock or (lambda: datetime.now(UTC))
    service = DashboardService(repo, settings, clock)
    auth = ManagerAuth(repo, settings, clock)
    env = _templates()

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        if request.url.path == "/manager" or request.url.path.startswith("/manager/"):
            for name, value in SECURITY_HEADERS.items():
                response.headers[name] = value
        return response

    app.mount("/manager/static", StaticFiles(directory=str(HERE / "static")), name="manager-static")
    app.include_router(build_router(service, auth, settings, env))


def build_router(service: DashboardService, auth: ManagerAuth, settings: DashboardSettings, env) -> APIRouter:  # noqa: ANN001, C901
    router = APIRouter(prefix="/manager")

    def render(name: str, ctx: SessionContext | None, status_code: int = 200, **values) -> HTMLResponse:  # noqa: ANN003
        values.setdefault("employees", service.employees() if ctx else [])
        values.setdefault("page", "")
        html = env.get_template(name).render(user=ctx.user if ctx else None, csrf=ctx.csrf_token if ctx else "",
                                             now=service.clock(), **values)
        return HTMLResponse(html, status_code=status_code)

    def session(request: Request) -> SessionContext | None:
        return auth.validate(request.cookies.get(COOKIE_NAME))

    def to_login() -> RedirectResponse:
        response = RedirectResponse("/manager/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
        return response

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin == f"{request.url.scheme}://{request.url.netloc}"

    async def form(request: Request) -> dict[str, list[str]]:
        if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            raise ValueError("unsupported form encoding")
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            raise ValueError("form too large")
        return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

    def one(data: dict, key: str, default: str = "") -> str:
        return (data.get(key) or [default])[0].strip()

    def opt_date(text: str | None) -> date | None:
        return date.fromisoformat(text) if text else None

    def selection(request: Request, employee_id: str | None = None) -> Selection:
        q = request.query_params
        kind = q.get("period") or "today"
        emp = employee_id if employee_id is not None else (q.get("employee") or None)
        try:
            frm, to = opt_date(q.get("from")), opt_date(q.get("to"))
        except ValueError:
            raise ValueError("dates must look like 2026-10-08") from None
        return service.selection(kind, emp, frm, to)

    def filter_values(request: Request) -> dict:
        q = request.query_params
        return {"period": q.get("period") or "today", "employee": q.get("employee") or "",
                "from": q.get("from") or "", "to": q.get("to") or ""}

    def guarded(page: Callable) -> Callable:
        """GET page: session required; filter errors shown as a 400 page."""
        def handler(request: Request):  # noqa: ANN202 — FastAPI sees only ``request``
            ctx = session(request)
            if ctx is None:
                return to_login()
            try:
                return page(request, ctx)
            except NotFound as exc:
                return render("error.html", ctx, 404, message=str(exc).capitalize() + ".")
            except ValueError as exc:
                return render("error.html", ctx, 400, message=str(exc).capitalize() + ".")
        return handler

    async def mutation(request: Request) -> tuple[SessionContext | None, dict | None, Response | None]:
        """POST: session + CSRF + same origin. Returns (ctx, form, error response)."""
        ctx = await run_in_threadpool(session, request)
        if ctx is None:
            return None, None, to_login()
        try:
            data = await form(request)
        except ValueError as exc:
            return ctx, None, render("error.html", ctx, 400, message=str(exc).capitalize() + ".")
        if not same_origin(request) or not ctx.csrf_ok(one(data, "csrf_token")):
            logger.warning("dashboard: rejected a request without a valid CSRF token (%s)", request.url.path)
            return ctx, None, render("error.html", ctx, 403,
                                     message="This form has expired or was not sent from the dashboard. "
                                             "Reload the page and try again.")
        return ctx, data, None

    # ── login / logout ────────────────────────────────────────────────────
    def login_page(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        token = new_token()
        response = render("login.html", None, status_code, error=error, login_token=token)
        response.set_cookie(LOGIN_COOKIE_NAME, token, max_age=1800, path="/manager/login", httponly=True,
                            samesite="strict", secure=settings.cookie_secure)
        return response

    @router.get("/login")
    def get_login(request: Request):  # noqa: ANN202
        if session(request) is not None:
            return RedirectResponse("/manager", status_code=303)
        return login_page(request)

    @router.post("/login")
    async def post_login(request: Request):  # noqa: ANN202
        try:
            data = await form(request)
        except ValueError:
            return login_page(request, LOGIN_FAILED, 400)
        cookie = request.cookies.get(LOGIN_COOKIE_NAME) or ""
        if not same_origin(request) or not cookie or not same(cookie, one(data, "login_token")):
            return login_page(request, "The login form expired. Please try again.", 403)
        result = await run_in_threadpool(auth.login, one(data, "username"), (data.get("password") or [""])[0])
        if result is None:
            return login_page(request, LOGIN_FAILED, 401)
        token, ctx = result
        response = RedirectResponse("/manager", status_code=303)
        response.set_cookie(COOKIE_NAME, token, max_age=settings.session_hours * 3600, path=COOKIE_PATH,
                            httponly=True, samesite="strict", secure=settings.cookie_secure)
        response.delete_cookie(LOGIN_COOKIE_NAME, path="/manager/login")
        return response

    @router.post("/logout")
    async def post_logout(request: Request):  # noqa: ANN202
        ctx, _, error = await mutation(request)
        if error is not None:
            return error
        await run_in_threadpool(auth.logout, ctx)
        return to_login()

    # ── pages ─────────────────────────────────────────────────────────────
    @router.get("")
    @guarded
    def overview(request: Request, ctx: SessionContext):  # noqa: ANN202
        return render("overview.html", ctx, page="overview", f=filter_values(request),
                      view=service.overview(selection(request)))

    @router.get("/employees")
    @guarded
    def employees(request: Request, ctx: SessionContext):  # noqa: ANN202
        statuses = service.current_status()
        return render("employees.html", ctx, page="employees",
                      rows=[(e, statuses[e.employee_id]) for e in service.employees()])

    @router.get("/employees/{employee_id}")
    def employee(request: Request, employee_id: str):  # noqa: ANN202
        @guarded
        def page(request: Request, ctx: SessionContext):  # noqa: ANN202
            view = service.employee_detail(employee_id, selection(request, employee_id))
            return render("employee.html", ctx, page="employees", f=filter_values(request), view=view)
        return page(request)

    @router.get("/attendance")
    @guarded
    def attendance(request: Request, ctx: SessionContext):  # noqa: ANN202
        q = request.query_params
        view = service.attendance(selection(request), q.get("sort") or "date", q.get("dir") or "desc")
        f = filter_values(request)
        base = {k: v for k, v in f.items() if v}
        return render("attendance.html", ctx, page="attendance", f=f, view=view,
                      sort_link=lambda key: "?" + urlencode({**base, "sort": key, "dir": "asc" if (
                          view["sort"] == key and view["direction"] == "desc") else "desc"}))

    @router.get("/applications")
    @guarded
    def applications(request: Request, ctx: SessionContext):  # noqa: ANN202
        view = service.applications(selection(request), request.query_params.get("group") or "app")
        return render("applications.html", ctx, page="applications", f=filter_values(request), view=view)

    @router.get("/schedules")
    @guarded
    def schedules(request: Request, ctx: SessionContext):  # noqa: ANN202
        done = request.query_params.get("done")
        message = {"created": "Schedule added.", "updated": "Schedule changed.",
                   "removed": "Schedule removed."}.get(done or "")
        return render("schedules.html", ctx, page="schedules",
                      view=service.schedules(request.query_params.get("employee") or None), message=message)

    # ── schedule mutations (POST + CSRF; audited in the schedule store) ───
    def hours_from(data: dict) -> tuple | None:
        if one(data, "day_off") == "1":
            return None
        return parse_hours(one(data, "start"), one(data, "end"), one(data, "expected_hours"))

    async def schedule_change(request: Request, change: Callable[[SessionContext, dict], object], done: str):  # noqa: ANN202
        ctx, data, error = await mutation(request)
        if error is not None:
            return error
        try:
            await run_in_threadpool(change, ctx, data)
        except NotFound as exc:
            return render("error.html", ctx, 404, message=str(exc).capitalize() + ".")
        except ValueError as exc:
            view = await run_in_threadpool(service.schedules, one(data, "return_employee") or None)
            return render("schedules.html", ctx, 400, page="schedules", view=view, error=str(exc), message=None)
        employee_id = one(data, "return_employee")
        query = urlencode({"employee": employee_id, "done": done} if employee_id else {"done": done})
        return RedirectResponse(f"/manager/schedules?{query}", status_code=303)

    def _date(data: dict, key: str) -> date | None:
        text = one(data, key)
        try:
            return date.fromisoformat(text) if text else None
        except ValueError:
            raise ValueError(f"{key.replace('_', ' ')} must be a date like 2026-10-08") from None

    @router.post("/schedules/weekly")
    async def add_weekly(request: Request):  # noqa: ANN202
        def change(ctx: SessionContext, data: dict) -> None:
            try:
                weekdays = [int(d) for d in data.get("weekday", [])]
            except ValueError:
                raise ValueError("invalid weekday") from None
            service.add_weekly(ctx.actor, one(data, "employee_id"), weekdays,
                               effective_from=_date(data, "effective_from") or service.selection(
                                   "today", one(data, "employee_id")).ranges[0].start,
                               effective_to=_date(data, "effective_to"), hours=hours_from(data))
        return await schedule_change(request, change, "created")

    @router.post("/schedules/date")
    async def add_date(request: Request):  # noqa: ANN202
        def change(ctx: SessionContext, data: dict) -> None:
            day = _date(data, "schedule_date")
            if day is None:
                raise ValueError("choose a date")
            service.add_date(ctx.actor, one(data, "employee_id"), day, hours_from(data))
        return await schedule_change(request, change, "created")

    @router.post("/schedules/{schedule_id}/update")
    async def update_rule(request: Request, schedule_id: str):  # noqa: ANN202
        def change(ctx: SessionContext, data: dict) -> None:
            service.update_rule(ctx.actor, schedule_id, hours=hours_from(data), from_date=_date(data, "from_date"),
                                schedule_date=_date(data, "schedule_date"), effective_to=_date(data, "effective_to"))
        return await schedule_change(request, change, "updated")

    @router.post("/schedules/{schedule_id}/remove")
    async def remove_rule(request: Request, schedule_id: str):  # noqa: ANN202
        def change(ctx: SessionContext, data: dict) -> None:
            service.remove_rule(ctx.actor, schedule_id, _date(data, "from_date"))
        return await schedule_change(request, change, "removed")

    # ── JSON (same session; for the Overview's auto-refresh) ──────────────
    def api(handler: Callable) -> Callable:
        def wrapped(request: Request):  # noqa: ANN202
            ctx = session(request)
            if ctx is None:
                return JSONResponse({"error": "not authenticated"}, status_code=401)
            try:
                return JSONResponse(handler(request))
            except (ValueError, NotFound) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return wrapped

    def _totals_json(t) -> dict:  # noqa: ANN001
        return {"scheduled_seconds": t.scheduled, "tracked_seconds": t.tracked, "active_seconds": t.active,
                "idle_seconds": t.idle, "unknown_seconds": t.unknown, "locked_seconds": t.locked,
                "overtime_seconds": t.overtime, "attendance_credit_seconds": t.credit,
                "attendance_basis_seconds": t.basis,
                "attendance_percentage": None if t.attendance_fraction is None else round(t.attendance_fraction * 100, 2),
                "late_employees": t.late_employees, "absent_employees": t.absent_employees,
                "data_incomplete_employees": t.incomplete_employees}

    @router.get("/api/current-status")
    @api
    def api_status(request: Request) -> dict:
        now = service.clock()
        names = {e.employee_id: e for e in service.employees()}
        statuses = service.current_status()
        rows = [{"employee_id": eid, "status": st.status.value, "label": st.label,
                 "last_seen": st.last_seen_at.isoformat() if st.last_seen_at else None,
                 "last_seen_text": fmt_ago(st.last_seen_at, now)}
                for eid, st in sorted(statuses.items()) if eid in names]
        active = [statuses[e.employee_id] for e in names.values() if e.is_active]
        return {"generated_at": now.isoformat(), "employees": rows, "counts": service.status_counts(active)}

    @router.get("/api/overview")
    @api
    def api_overview(request: Request) -> dict:
        view = service.overview(selection(request))
        return {"period": view["period"], "totals": _totals_json(view["totals"]),
                "active_employees": view["active_employees"], "status_counts": view["status_counts"]}

    @router.get("/api/attendance")
    @api
    def api_attendance(request: Request) -> dict:
        q = request.query_params
        view = service.attendance(selection(request), q.get("sort") or "date", q.get("dir") or "desc")
        return {"period": view["period"], "totals": _totals_json(view["totals"]), "rows": [
            {"employee_id": r.employee.employee_id, "date": r.summary.local_date.isoformat(),
             "status": r.summary.attendance_status.value, "scheduled_seconds": r.summary.scheduled_seconds,
             "active_seconds": r.summary.active_seconds, "attendance_percentage": r.summary.attendance_percentage,
             "data_quality": r.summary.data_quality.value} for r in view["rows"]]}

    @router.get("/api/applications")
    @api
    def api_applications(request: Request) -> dict:
        view = service.applications(selection(request), request.query_params.get("group") or "app")
        return {"period": view["period"], "group": view["group"], "rows": [
            {"application": g["app"], "employee_id": g["employee"].employee_id if g["employee"] else None,
             "active_seconds": g["active"], "idle_seconds": g["idle"], "unknown_seconds": g["unknown"],
             "usage_days": g["usage_days"]} for g in view["rows"]]}

    return router
