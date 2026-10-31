"""SaaS read-only reporting orchestration; no HTTP or SQL policy duplication."""

from datetime import date, datetime, timezone

from ..dashboard.analysis import Analysis, ChartData, Series, hours
from ..dashboard.charts import render as render_chart
from ..dashboard.service import DashboardService
from ..workforce.service import WorkforceService
from .queries import ScopedDashboardRepository


class BrowserDashboardService:
    def __init__(self, runtime):
        self.clock = getattr(runtime, "browser_clock", lambda: datetime.now(timezone.utc))
        self.workforce = WorkforceService(
            runtime.control, runtime.resolver, runtime.manager, clock=self.clock
        )

    def view(self, context, page, query, employee_id=None):
        with self.workforce.dashboard(context, employee_id) as (conn, scope):
            repo = ScopedDashboardRepository(conn, scope, now=self.clock())
            service = DashboardService(repo, clock=self.clock)
            selected = employee_id or query.get("employee") or None
            start = date.fromisoformat(query["from"]) if query.get("from") else None
            end = date.fromisoformat(query["to"]) if query.get("to") else None
            sel = service.selection(query.get("period") or "today", selected, start, end)
            profiles = repo.profiles([selected] if selected else None)
            statuses = service.current_status()
            view = {
                "profiles": profiles,
                "statuses": statuses,
                "employees": service.employees(),
                "selection": sel,
                "period": service.describe(sel),
                "now": self.clock(),
                "limited": any(
                    (s.start is not None or s.end is not None)
                    and s.employee_id in {p["employee_id"] for p in profiles}
                    for s in scope.subjects
                ),
                "limits": [
                    {
                        "name": p["display_name"],
                        "start": s.start,
                        "end": s.end,
                        "timezone": p["timezone"],
                    }
                    for s in scope.subjects
                    for p in profiles
                    if p["employee_id"] == s.employee_id and s.start
                ],
            }
            if employee_id:
                view["detail"] = service.employee_detail(employee_id, sel)
                view["profile"] = profiles[0]
            elif page == "attendance":
                view["report"] = service.attendance(
                    sel, query.get("sort", "date"), query.get("direction", "desc")
                )
            elif page == "applications":
                view["report"] = service.applications(sel, query.get("group", "app"))
            elif page == "analytics":
                analysis = Analysis(service, sel)
                charts = analysis.charts(
                    ["active_by_employee", "status_hours", "applications", "attendance"]
                )
                # Do not let legacy long-range weekly condensation invent zero boundary buckets.
                totals = {}
                for row in analysis.rows:
                    day = row.summary.local_date
                    totals[day] = totals.get(day, 0) + row.summary.active_seconds
                days = sorted(totals)[-62:]
                charts.insert(
                    2,
                    ChartData(
                        "daily_trend",
                        "Daily Active Computer Time",
                        "Complete authorized daily summaries only; omitted dates are not zero.",
                        "h",
                        "columns",
                        (Series("active", "Active Hours"),),
                        [d.isoformat() for d in days],
                        [[hours(totals[d])] for d in days],
                        note="Latest 62 available days at most. Other charts cover the selected eligible data.",
                    ),
                )
                view["charts"] = [render_chart(c) for c in charts]

            else:
                view["report"] = service.overview(sel)
            return view
