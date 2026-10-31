"""Read-only dashboard adapter. EVERY workforce query joins the authorized subject CTE.

Interval observations are clipped in SQL. Stored aggregates require complete
calendar AND attribution coverage; attendance is never prorated.
"""

from datetime import datetime, timezone

from ..attendance.models import ScheduleRule
from ..attendance.postgres_store import PostgresAttendanceStore
from ..dashboard.models import AppUsage, Device, Employee, Period
from ..workforce.service import AuthorizedWorkforceScope


class ScopedDashboardRepository:
    def __init__(self, conn, scope: AuthorizedWorkforceScope, *, now=None):
        if not isinstance(scope, AuthorizedWorkforceScope):
            raise TypeError("authorized workforce scope required")
        self.conn, self.scope = conn, scope
        self.now = now or datetime.now(timezone.utc)
        values = ",".join("(%s::text,%s::timestamptz,%s::timestamptz)" for _ in scope.subjects)
        self.prefix = (
            "WITH authorized(employee_id,scope_start,scope_end) AS (VALUES " + values + ") "
            if values
            else "WITH authorized(employee_id,scope_start,scope_end) AS "
            "(SELECT NULL::text,NULL::timestamptz,NULL::timestamptz WHERE false) "
        )
        self.params = tuple(v for s in scope.subjects for v in (s.employee_id, s.start, s.end))

    def _rows(self, query, params=()):
        return self.conn.execute(self.prefix + query, (*self.params, *params)).fetchall()

    def employees(self):
        return [
            Employee(**r)
            for r in self._rows(
                "SELECT e.employee_id,e.display_name,e.timezone,e.is_active FROM employees e "
                "JOIN authorized a USING(employee_id) ORDER BY e.employee_id"
            )
        ]

    def profiles(self, employee_ids=None):
        return self._rows(
            """SELECT e.employee_id,e.display_name,e.timezone,e.is_active,
            p.employment_status,p.monitoring_enabled,d.display_name AS designation,
            dep.display_name AS department,
            ARRAY(SELECT t.display_name FROM employee_team_memberships tm JOIN teams t ON t.id=tm.team_id
                  WHERE tm.employee_id=e.employee_id AND tm.ended_at IS NULL ORDER BY t.display_name) AS teams,
            (SELECT m.display_name FROM employee_reporting_relationships r
             JOIN employees m ON m.employee_id=r.manager_employee_id WHERE r.employee_id=e.employee_id
             AND r.effective_from<=%s AND (r.effective_to IS NULL OR r.effective_to>%s)) AS reporting_manager
            FROM employees e JOIN authorized a USING(employee_id) JOIN employee_profiles p USING(employee_id)
            LEFT JOIN designations d ON d.id=p.designation_id LEFT JOIN departments dep ON dep.id=p.department_id
            WHERE (%s::text[] IS NULL OR e.employee_id=ANY(%s))
            ORDER BY lower(e.display_name),e.employee_id""",
            (self.now, self.now, employee_ids, employee_ids),
        )

    def devices(self):
        return [
            Device(**r)
            for r in self._rows("""SELECT d.device_id,d.employee_id,d.status,
            CASE WHEN (a.scope_start IS NULL OR d.last_seen_at>=a.scope_start)
                  AND (a.scope_end IS NULL OR d.last_seen_at<=a.scope_end) THEN d.last_seen_at END AS last_seen_at
            FROM devices d JOIN authorized a USING(employee_id)""")
        ]

    def _periods(self, condition, params, *, latest=False, start=None, end=None, limit=None):
        # requested bounds are also applied in SQL, never only in presentation.
        lo, hi = start, end
        projection = """p.period_id::text AS period_id,p.employee_id,p.device_id,
            greatest(p.started_at,a.scope_start,%s::timestamptz) AS started_at,
            least(p.ended_at,a.scope_end,%s::timestamptz) AS ended_at,
            extract(epoch FROM least(p.ended_at,a.scope_end,%s::timestamptz)-
                greatest(p.started_at,a.scope_start,%s::timestamptz))::float8 AS duration_seconds,
            p.status,p.is_open,p.app_name,p.window_title,p.domain,p.privacy_excluded"""
        select = "SELECT " + ("DISTINCT ON (p.device_id) " if latest else "") + projection
        query = (
            select
            + """ FROM activity_periods p JOIN authorized a USING(employee_id)
            WHERE (a.scope_start IS NULL OR p.ended_at>a.scope_start)
              AND (a.scope_end IS NULL OR p.started_at<a.scope_end) AND """
            + condition
        )
        query += " ORDER BY " + (
            "p.device_id,p.ended_at DESC,p.started_at DESC,p.period_id"
            if latest
            else "p.started_at DESC,p.period_id"
        )
        arguments = (lo, hi, hi, lo, *params)
        if limit is not None:
            query += " LIMIT %s"
            arguments += (limit,)
        return [Period(**r) for r in self._rows(query, arguments)]

    def latest_periods(self, since):
        return self._periods("p.ended_at>=%s", (since,), latest=True)

    def activity(self, employee_id, start, end, limit):
        return self._periods(
            "p.employee_id=%s AND p.ended_at>%s AND p.started_at<%s",
            (employee_id, start, end),
            start=start,
            end=end,
            limit=limit,
        )

    @staticmethod
    def _daily_guard():
        return """(a.scope_start IS NULL OR (s.window_start>=a.scope_start
            AND s.local_date::timestamp AT TIME ZONE s.timezone>=a.scope_start
            AND (s.scheduled_start IS NULL OR s.scheduled_start>=a.scope_start)))
            AND (a.scope_end IS NULL OR (s.window_end<=a.scope_end
            AND (s.local_date+1)::timestamp AT TIME ZONE s.timezone<=a.scope_end
            AND (s.scheduled_end IS NULL OR s.scheduled_end<=a.scope_end)))"""

    def daily(self, employee_ids, start, end):
        rows = self._rows(
            "SELECT s.* FROM daily_summaries s JOIN authorized a USING(employee_id) "
            "WHERE s.employee_id=ANY(%s) AND s.local_date BETWEEN %s AND %s AND "
            + self._daily_guard()
            + " ORDER BY s.local_date,s.employee_id",
            (employee_ids, start, end),
        )
        return [PostgresAttendanceStore._daily(r) for r in rows]

    def summaries_updated_at(self):
        return self._rows(
            "SELECT max(s.updated_at) AS t FROM daily_summaries s "
            "JOIN authorized a USING(employee_id) WHERE " + self._daily_guard()
        )[0]["t"]

    def app_usage(self, employee_ids, start, end):
        rows = self._rows(
            """SELECT u.employee_id,u.usage_date,u.app_name,
            sum(u.active_seconds) AS active_seconds,sum(u.idle_seconds) AS idle_seconds,
            sum(u.unknown_seconds) AS unknown_seconds
            FROM application_usage_daily u JOIN authorized a USING(employee_id)
            WHERE u.employee_id=ANY(%s) AND u.usage_date BETWEEN %s AND %s
            AND (a.scope_start IS NULL OR u.day_start_utc>=a.scope_start)
            AND (a.scope_end IS NULL OR u.day_end_utc<=a.scope_end)
            GROUP BY u.employee_id,u.usage_date,u.app_name ORDER BY u.employee_id,u.usage_date,u.app_name""",
            (employee_ids, start, end),
        )
        return [AppUsage(**r) for r in rows]

    def schedules(self, employee_ids):
        # Current schedule configuration is allowed; no schedule-history editor is exposed.
        rows = self._rows(
            """SELECT s.schedule_id::text AS schedule_id,s.employee_id,s.is_working_day,s.timezone,
            s.day_of_week,s.effective_from,s.effective_to,s.schedule_date,s.start_time,s.end_time,s.expected_work_seconds
            FROM work_schedules s JOIN authorized a USING(employee_id) WHERE s.employee_id=ANY(%s)
            AND (a.scope_start IS NULL OR COALESCE(s.schedule_date,s.effective_to,'infinity'::date)>=
                 (a.scope_start AT TIME ZONE s.timezone)::date)
            AND (a.scope_end IS NULL OR COALESCE(s.schedule_date,s.effective_from,'-infinity'::date)<=
                 (a.scope_end AT TIME ZONE s.timezone)::date)""",
            (employee_ids,),
        )
        return [ScheduleRule(**r) for r in rows]
