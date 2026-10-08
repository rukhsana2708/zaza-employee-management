"""Attendance summaries (Phase 5).

Revision ID: 0002_attendance_summaries
Revises: 0001_initial
Create Date: 2026-10-08

daily_summaries, weekly_summaries, monthly_summaries: the authoritative
attendance / work-time figures (calculated by deskmate.zaza_server.attendance).
Later phases (Sheets, dashboard, charts) read these; they never recalculate.

Forward-only, additive: creates three tables, touches nothing existing.
"""

from __future__ import annotations

from alembic import op

revision = "0002_attendance_summaries"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

HASH_PATTERN = "'^[0-9a-f]{64}$'"
DAILY_STATUSES = ("'PRESENT', 'LATE', 'EARLY_LEAVE', 'LATE_AND_EARLY', 'ABSENT', 'DAY_OFF', 'WORKED_DAY_OFF', "
                  "'DATA_INCOMPLETE', 'NO_SCHEDULE', 'PENDING'")
QUALITY = "'COMPLETE', 'PARTIAL', 'INSUFFICIENT'"


def _rollup_columns(t: str) -> str:
    """Columns shared by weekly_summaries and monthly_summaries."""
    return f"""
        timezone                      text        NOT NULL,
        working_days                  integer     NOT NULL,
        days_worked                   integer     NOT NULL,
        scheduled_seconds             bigint      NOT NULL,
        measurable_scheduled_seconds  bigint      NOT NULL,
        tracked_seconds               bigint      NOT NULL,
        active_seconds                bigint      NOT NULL,
        idle_seconds                  bigint      NOT NULL,
        unknown_seconds               bigint      NOT NULL,
        locked_seconds                bigint      NOT NULL,
        active_in_shift_seconds       bigint      NOT NULL,
        detected_break_seconds        bigint      NOT NULL,
        late_seconds                  bigint      NOT NULL,
        early_leave_seconds           bigint      NOT NULL,
        overtime_seconds              bigint      NOT NULL,
        attendance_credit_seconds     bigint      NOT NULL,
        attendance_basis_seconds      bigint      NOT NULL,
        attendance_percentage         numeric(5, 2),
        average_active_seconds_per_worked_day bigint,
        absent_days                   integer     NOT NULL,
        late_days                     integer     NOT NULL,
        early_leave_days              integer     NOT NULL,
        incomplete_days               integer     NOT NULL,
        worked_day_off_days           integer     NOT NULL,
        pending_days                  integer     NOT NULL,
        data_quality                  text        NOT NULL,
        is_provisional                boolean     NOT NULL,
        calculation_version           integer     NOT NULL,
        summary_hash                  text        NOT NULL,
        created_at                    timestamptz NOT NULL DEFAULT now(),
        updated_at                    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT {t}_employee_fk FOREIGN KEY (employee_id) REFERENCES employees (employee_id) ON DELETE RESTRICT,
        CONSTRAINT {t}_counts_valid CHECK (
            working_days >= 0 AND days_worked >= 0 AND absent_days >= 0 AND late_days >= 0
            AND early_leave_days >= 0 AND incomplete_days >= 0 AND worked_day_off_days >= 0 AND pending_days >= 0
            AND absent_days + incomplete_days + pending_days <= working_days
        ),
        CONSTRAINT {t}_seconds_nonneg CHECK (
            scheduled_seconds >= 0 AND measurable_scheduled_seconds >= 0 AND tracked_seconds >= 0
            AND active_seconds >= 0 AND idle_seconds >= 0 AND unknown_seconds >= 0 AND locked_seconds >= 0
            AND active_in_shift_seconds >= 0 AND detected_break_seconds >= 0 AND late_seconds >= 0
            AND early_leave_seconds >= 0 AND overtime_seconds >= 0 AND attendance_credit_seconds >= 0
            AND attendance_basis_seconds >= 0
        ),
        CONSTRAINT {t}_tracked_is_sum CHECK (
            tracked_seconds = active_seconds + idle_seconds + unknown_seconds + locked_seconds
        ),
        CONSTRAINT {t}_attendance_valid CHECK (
            attendance_credit_seconds <= attendance_basis_seconds
            AND (attendance_percentage IS NULL OR attendance_percentage BETWEEN 0 AND 100)
            AND ((attendance_basis_seconds > 0) = (attendance_percentage IS NOT NULL))
        ),
        CONSTRAINT {t}_quality_valid CHECK (data_quality IN ({QUALITY})),
        CONSTRAINT {t}_calculation_version_valid CHECK (calculation_version >= 1),
        CONSTRAINT {t}_hash_format CHECK (summary_hash ~ {HASH_PATTERN})"""


STATEMENTS = [
    f"""
    CREATE TABLE daily_summaries (
        employee_id                   text        NOT NULL,
        local_date                    date        NOT NULL,
        timezone                      text        NOT NULL,
        schedule_id                   uuid,
        schedule_kind                 text        NOT NULL,
        is_working_day                boolean     NOT NULL,
        scheduled_start               timestamptz,
        scheduled_end                 timestamptz,
        shift_span_seconds            integer     NOT NULL,
        scheduled_seconds             integer     NOT NULL,
        measurable_scheduled_seconds  integer     NOT NULL,
        window_start                  timestamptz NOT NULL,
        window_end                    timestamptz NOT NULL,
        tracked_seconds               integer     NOT NULL,
        active_seconds                integer     NOT NULL,
        idle_seconds                  integer     NOT NULL,
        unknown_seconds               integer     NOT NULL,
        locked_seconds                integer     NOT NULL,
        active_in_shift_seconds       integer     NOT NULL,
        tracked_in_shift_seconds      integer     NOT NULL,
        unknown_in_shift_seconds      integer     NOT NULL,
        pre_shift_active_seconds      integer     NOT NULL,
        post_shift_active_seconds     integer     NOT NULL,
        detected_break_seconds        integer     NOT NULL,
        first_activity_at             timestamptz,
        last_activity_at              timestamptz,
        first_tracked_at              timestamptz,
        last_tracked_at               timestamptz,
        late_seconds                  integer     NOT NULL,
        early_leave_seconds           integer     NOT NULL,
        overtime_seconds              integer     NOT NULL,
        attendance_status             text        NOT NULL,
        attendance_credit_seconds     integer     NOT NULL,
        attendance_basis_seconds      integer     NOT NULL,
        attendance_percentage         numeric(5, 2),
        worked_day                    boolean     NOT NULL,
        data_quality                  text        NOT NULL,
        quality_flags                 text[]      NOT NULL DEFAULT '{{}}',
        is_provisional                boolean     NOT NULL,
        session_count                 integer     NOT NULL,
        device_count                  integer     NOT NULL,
        calculation_version           integer     NOT NULL,
        policy                        jsonb       NOT NULL,
        summary_hash                  text        NOT NULL,
        created_at                    timestamptz NOT NULL DEFAULT now(),
        updated_at                    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT daily_summaries_pk PRIMARY KEY (employee_id, local_date),
        CONSTRAINT daily_summaries_employee_fk FOREIGN KEY (employee_id)
            REFERENCES employees (employee_id) ON DELETE RESTRICT,
        CONSTRAINT daily_summaries_schedule_fk FOREIGN KEY (schedule_id)
            REFERENCES work_schedules (schedule_id) ON DELETE SET NULL,
        CONSTRAINT daily_summaries_status_valid CHECK (attendance_status IN ({DAILY_STATUSES})),
        CONSTRAINT daily_summaries_quality_valid CHECK (data_quality IN ({QUALITY})),
        CONSTRAINT daily_summaries_kind_valid CHECK (schedule_kind IN ('DATE', 'WEEKLY', 'NONE')),
        CONSTRAINT daily_summaries_seconds_nonneg CHECK (
            shift_span_seconds >= 0 AND scheduled_seconds >= 0 AND measurable_scheduled_seconds >= 0
            AND tracked_seconds >= 0 AND active_seconds >= 0 AND idle_seconds >= 0 AND unknown_seconds >= 0
            AND locked_seconds >= 0 AND active_in_shift_seconds >= 0 AND tracked_in_shift_seconds >= 0
            AND unknown_in_shift_seconds >= 0 AND pre_shift_active_seconds >= 0
            AND post_shift_active_seconds >= 0 AND detected_break_seconds >= 0 AND late_seconds >= 0
            AND early_leave_seconds >= 0 AND overtime_seconds >= 0 AND attendance_credit_seconds >= 0
            AND attendance_basis_seconds >= 0 AND session_count >= 0 AND device_count >= 0
        ),
        -- tracked time is exactly the four monitored states; nothing is counted twice
        CONSTRAINT daily_summaries_tracked_is_sum CHECK (
            tracked_seconds = active_seconds + idle_seconds + unknown_seconds + locked_seconds
        ),
        CONSTRAINT daily_summaries_parts_within_totals CHECK (
            active_in_shift_seconds <= active_seconds AND pre_shift_active_seconds <= active_seconds
            AND post_shift_active_seconds <= active_seconds AND tracked_in_shift_seconds <= tracked_seconds
            AND unknown_in_shift_seconds <= unknown_seconds
            AND measurable_scheduled_seconds <= scheduled_seconds
            AND attendance_credit_seconds <= attendance_basis_seconds
            AND attendance_basis_seconds <= measurable_scheduled_seconds
        ),
        CONSTRAINT daily_summaries_worked_day_consistent CHECK (worked_day = (active_seconds > 0)),
        CONSTRAINT daily_summaries_shift_consistent CHECK (
            (scheduled_start IS NULL) = (scheduled_end IS NULL)
            AND (scheduled_start IS NULL OR scheduled_end > scheduled_start)
            AND (is_working_day OR (scheduled_seconds = 0 AND scheduled_start IS NULL))
        ),
        CONSTRAINT daily_summaries_window_valid CHECK (window_end > window_start),
        -- lateness / early leave only exist on days that say so
        CONSTRAINT daily_summaries_punctuality_consistent CHECK (
            (late_seconds = 0 OR attendance_status IN ('LATE', 'LATE_AND_EARLY'))
            AND (early_leave_seconds = 0 OR attendance_status IN ('EARLY_LEAVE', 'LATE_AND_EARLY'))
        ),
        CONSTRAINT daily_summaries_status_matches_day CHECK (
            (attendance_status IN ('DAY_OFF', 'WORKED_DAY_OFF', 'NO_SCHEDULE')) = (NOT is_working_day)
        ),
        CONSTRAINT daily_summaries_attendance_valid CHECK (
            (attendance_percentage IS NULL OR attendance_percentage BETWEEN 0 AND 100)
            AND ((attendance_basis_seconds > 0) = (attendance_percentage IS NOT NULL))
        ),
        CONSTRAINT daily_summaries_calculation_version_valid CHECK (calculation_version >= 1),
        CONSTRAINT daily_summaries_hash_format CHECK (summary_hash ~ {HASH_PATTERN}),
        CONSTRAINT daily_summaries_policy_object CHECK (jsonb_typeof(policy) = 'object')
    )
    """,
    f"""
    CREATE TABLE weekly_summaries (
        employee_id  text NOT NULL,
        week_start   date NOT NULL,
        week_end     date NOT NULL,
        {_rollup_columns("weekly_summaries")},
        CONSTRAINT weekly_summaries_pk PRIMARY KEY (employee_id, week_start),
        -- ISO week: Monday .. Sunday
        CONSTRAINT weekly_summaries_iso_week CHECK (
            EXTRACT(ISODOW FROM week_start) = 1 AND week_end = week_start + 6
        )
    )
    """,
    f"""
    CREATE TABLE monthly_summaries (
        employee_id  text NOT NULL,
        month        date NOT NULL,
        month_end    date NOT NULL,
        {_rollup_columns("monthly_summaries")},
        CONSTRAINT monthly_summaries_pk PRIMARY KEY (employee_id, month),
        CONSTRAINT monthly_summaries_calendar_month CHECK (
            EXTRACT(DAY FROM month) = 1
            AND month_end = (month + interval '1 month' - interval '1 day')::date
        )
    )
    """,
    # The primary keys serve "employee + date/week/month"; these serve
    # "everyone on a date / in a week / in a month" (team views).
    "CREATE INDEX daily_summaries_date_idx ON daily_summaries (local_date)",
    "CREATE INDEX weekly_summaries_week_idx ON weekly_summaries (week_start)",
    "CREATE INDEX monthly_summaries_month_idx ON monthly_summaries (month)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Summaries are derived data (recalculable), but downgrade is still only
    # reachable through an explicit Alembic command, never the server CLI.
    for table in ("monthly_summaries", "weekly_summaries", "daily_summaries"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
