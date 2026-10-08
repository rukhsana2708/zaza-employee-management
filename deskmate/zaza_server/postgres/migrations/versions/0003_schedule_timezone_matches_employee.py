"""Schedule timezone must equal the employee's timezone (Phase 5 rule).

Revision ID: 0003_schedule_timezone
Revises: 0002_attendance_summaries
Create Date: 2026-10-08

The attendance calculation uses one calendar per employee: the employee's
reporting timezone defines local dates and date attribution, and the
schedule's timezone defines the shift's start/end instants. Phase 5 requires
both to be the same zone (no per-schedule "travel" timezones), and this
enforces it in the database so direct SQL can't create an inconsistent rule:

- ``employees (employee_id, timezone)`` gets a UNIQUE key (trivially unique,
  employee_id is the primary key) so it can be referenced;
- ``work_schedules (employee_id, timezone)`` references it. ON UPDATE NO
  ACTION: an employee's timezone can't be changed while schedules in the old
  zone exist (changing it is a deliberate, audited migration, not a side
  effect).

Forward-only. Existing mismatched rows abort the upgrade with a clear
message rather than being silently rewritten.
"""

from __future__ import annotations

from alembic import op

revision = "0003_schedule_timezone"
down_revision = "0002_attendance_summaries"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    DO $$
    DECLARE n integer;
    BEGIN
        SELECT count(*) INTO n FROM work_schedules s JOIN employees e USING (employee_id)
         WHERE s.timezone <> e.timezone;
        IF n > 0 THEN
            RAISE EXCEPTION '% work_schedules row(s) use a timezone different from their employee''s; '
                            'fix them before applying 0003_schedule_timezone', n;
        END IF;
    END $$
    """,
    "ALTER TABLE employees ADD CONSTRAINT employees_id_timezone_key UNIQUE (employee_id, timezone)",
    """
    ALTER TABLE work_schedules ADD CONSTRAINT work_schedules_timezone_matches_employee
        FOREIGN KEY (employee_id, timezone) REFERENCES employees (employee_id, timezone)
        ON UPDATE NO ACTION ON DELETE RESTRICT
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE work_schedules DROP CONSTRAINT IF EXISTS work_schedules_timezone_matches_employee")
    op.execute("ALTER TABLE employees DROP CONSTRAINT IF EXISTS employees_id_timezone_key")
