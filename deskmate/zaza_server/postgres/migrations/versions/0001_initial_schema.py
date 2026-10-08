"""Initial ZaZa central schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-08

Tables: employees, devices, device_tokens, work_schedules, work_sessions,
activity_periods, idle_periods, application_usage_daily, audit_logs.

Conventions
- Every timestamp is TIMESTAMPTZ (an absolute instant). Reporting time zones
  live in separate ``timezone`` columns; nothing stores ambiguous local time.
- Constraints are named so the repository can map a violation to a short,
  safe error message without echoing row data.
- Synced tables keep typed, first-class columns for everything reporting
  needs, plus ``payload`` (the validated wire record as received) for
  audit and forward compatibility. Reports must use the typed columns.
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

ID_PATTERN = "'^[A-Za-z0-9._:-]{1,128}$'"
HASH_PATTERN = "'^[0-9a-f]{64}$'"
MAX_SECONDS = "316224000"  # 10 years, mirrors the wire protocol bound
DAY_SECONDS = "93600"  # 26 h: the longest possible local day (DST) per app


def _sync_columns(table: str) -> str:
    """Bookkeeping every synchronized table shares (idempotency + ownership)."""
    t = table
    return f"""
        device_id           text        NOT NULL,
        employee_id         text        NOT NULL,
        record_version      integer     NOT NULL,
        local_seq           bigint      NOT NULL,
        content_hash        text        NOT NULL,
        device_created_at   timestamptz NOT NULL,
        device_updated_at   timestamptz NOT NULL,
        first_received_at   timestamptz NOT NULL DEFAULT now(),
        last_received_at    timestamptz NOT NULL DEFAULT now(),
        last_batch_id       uuid        NOT NULL,
        payload             jsonb       NOT NULL,
        CONSTRAINT {t}_device_fk FOREIGN KEY (device_id) REFERENCES devices (device_id) ON DELETE RESTRICT,
        CONSTRAINT {t}_employee_fk FOREIGN KEY (employee_id) REFERENCES employees (employee_id) ON DELETE RESTRICT,
        CONSTRAINT {t}_version_positive CHECK (record_version >= 1),
        CONSTRAINT {t}_local_seq_nonneg CHECK (local_seq >= 0),
        CONSTRAINT {t}_hash_format CHECK (content_hash ~ {HASH_PATTERN}),
        CONSTRAINT {t}_received_order CHECK (last_received_at >= first_received_at),
        CONSTRAINT {t}_payload_object CHECK (jsonb_typeof(payload) = 'object')"""


def _seconds_check(table: str, columns: list[str], upper: str = MAX_SECONDS) -> str:
    # BETWEEN also rejects NaN/Infinity (NaN sorts above every number in PostgreSQL).
    cond = " AND ".join(f"{c} BETWEEN 0 AND {upper}" for c in columns)
    return f"CONSTRAINT {table}_durations_valid CHECK ({cond})"


STATEMENTS = [
    # ─── helper functions ────────────────────────────────────────────────
    """
    CREATE FUNCTION zaza_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        -- updated_at tracks real changes; a last_seen_at heartbeat alone
        -- (device connectivity) does not count as an edit.
        IF (to_jsonb(NEW) - 'updated_at' - 'last_seen_at') IS DISTINCT FROM
           (to_jsonb(OLD) - 'updated_at' - 'last_seen_at') THEN
            NEW.updated_at := now();
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE FUNCTION zaza_check_timezone() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        -- Raises "time zone ... not recognized" for an invalid IANA name.
        PERFORM now() AT TIME ZONE NEW.timezone;
        RETURN NEW;
    END $$
    """,
    """
    CREATE FUNCTION zaza_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs is append-only' USING ERRCODE = 'restrict_violation';
    END $$
    """,
    # ─── people & devices ────────────────────────────────────────────────
    f"""
    CREATE TABLE employees (
        employee_id   text        PRIMARY KEY,
        display_name  text        NOT NULL,
        role          text        NOT NULL DEFAULT 'EMPLOYEE',
        is_active     boolean     NOT NULL DEFAULT true,
        timezone      text        NOT NULL DEFAULT 'UTC',
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT employees_id_format CHECK (employee_id ~ {ID_PATTERN}),
        CONSTRAINT employees_name_valid CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
        CONSTRAINT employees_role_valid CHECK (role IN ('EMPLOYEE', 'MANAGER', 'ADMIN')),
        CONSTRAINT employees_timezone_valid CHECK (length(timezone) BETWEEN 1 AND 64)
    )
    """,
    f"""
    CREATE TABLE devices (
        device_id     text        PRIMARY KEY,
        employee_id   text        NOT NULL,
        display_name  text,
        status        text        NOT NULL DEFAULT 'ACTIVE',
        disabled_at   timestamptz,
        last_seen_at  timestamptz,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT devices_employee_fk FOREIGN KEY (employee_id) REFERENCES employees (employee_id) ON DELETE RESTRICT,
        CONSTRAINT devices_id_format CHECK (device_id ~ {ID_PATTERN}),
        CONSTRAINT devices_name_valid CHECK (display_name IS NULL OR length(btrim(display_name)) BETWEEN 1 AND 200),
        CONSTRAINT devices_status_valid CHECK (status IN ('ACTIVE', 'DISABLED')),
        CONSTRAINT devices_disabled_consistent CHECK ((status = 'DISABLED') = (disabled_at IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE device_tokens (
        token_id      uuid        PRIMARY KEY,
        device_id     text        NOT NULL,
        token_hash    text        NOT NULL,
        status        text        NOT NULL DEFAULT 'ACTIVE',
        created_at    timestamptz NOT NULL DEFAULT now(),
        last_used_at  timestamptz,
        revoked_at    timestamptz,
        CONSTRAINT device_tokens_device_fk FOREIGN KEY (device_id) REFERENCES devices (device_id) ON DELETE RESTRICT,
        CONSTRAINT device_tokens_hash_unique UNIQUE (token_hash),
        -- Only a SHA-256 hex digest fits here: a raw "zzd_..." token can't be stored.
        CONSTRAINT device_tokens_hash_format CHECK (token_hash ~ {HASH_PATTERN}),
        CONSTRAINT device_tokens_status_valid CHECK (status IN ('ACTIVE', 'REVOKED')),
        CONSTRAINT device_tokens_revoked_consistent CHECK ((status = 'REVOKED') = (revoked_at IS NOT NULL))
    )
    """,
    """
    CREATE TABLE work_schedules (
        schedule_id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        employee_id            text        NOT NULL,
        -- Either a weekly rule (ISO day_of_week 1=Mon..7=Sun, valid from
        -- effective_from to effective_to) or a one-off rule for schedule_date.
        day_of_week            smallint,
        effective_from         date,
        effective_to           date,
        schedule_date          date,
        is_working_day         boolean     NOT NULL,
        start_time             time,
        end_time               time,
        expected_work_seconds  integer,
        timezone               text        NOT NULL,
        created_at             timestamptz NOT NULL DEFAULT now(),
        updated_at             timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT work_schedules_employee_fk FOREIGN KEY (employee_id) REFERENCES employees (employee_id) ON DELETE RESTRICT,
        CONSTRAINT work_schedules_kind_valid CHECK (
            (day_of_week IS NOT NULL AND effective_from IS NOT NULL AND schedule_date IS NULL)
            OR (day_of_week IS NULL AND effective_from IS NULL AND effective_to IS NULL AND schedule_date IS NOT NULL)
        ),
        CONSTRAINT work_schedules_day_of_week_valid CHECK (day_of_week IS NULL OR day_of_week BETWEEN 1 AND 7),
        CONSTRAINT work_schedules_effective_range CHECK (effective_to IS NULL OR effective_to >= effective_from),
        -- A shift belongs to the local day it STARTS on (day_of_week /
        -- schedule_date). end_time < start_time means it ends the next local
        -- day (20:00 -> 04:00 spans 8 h). start_time = end_time is rejected:
        -- it must not silently mean a 24-hour shift. 24:00 is not allowed
        -- either (00:00 -> 24:00 would be a 24-hour shift in disguise).
        CONSTRAINT work_schedules_hours_valid CHECK (
            (is_working_day AND start_time IS NOT NULL AND end_time IS NOT NULL
             AND start_time < '24:00' AND end_time < '24:00' AND end_time <> start_time
             AND expected_work_seconds IS NOT NULL AND expected_work_seconds > 0
             AND expected_work_seconds <= CASE
                 WHEN end_time > start_time THEN EXTRACT(EPOCH FROM (end_time - start_time))
                 ELSE EXTRACT(EPOCH FROM (end_time - start_time)) + 86400  -- crosses midnight
             END)
            OR (NOT is_working_day AND start_time IS NULL AND end_time IS NULL
                AND COALESCE(expected_work_seconds, 0) = 0)
        ),
        CONSTRAINT work_schedules_timezone_valid CHECK (length(timezone) BETWEEN 1 AND 64)
    )
    """,
    # ─── synchronized activity data ──────────────────────────────────────
    f"""
    CREATE TABLE work_sessions (
        session_id          uuid        PRIMARY KEY,
        {_sync_columns("work_sessions")},
        started_at          timestamptz NOT NULL,
        ended_at            timestamptz,
        last_heartbeat_at   timestamptz NOT NULL,
        status              text        NOT NULL,
        start_reason        text        NOT NULL,
        end_reason          text,
        previous_session_id uuid,
        tracked_seconds     double precision NOT NULL,
        active_seconds      double precision NOT NULL,
        idle_seconds        double precision NOT NULL,
        unknown_seconds     double precision NOT NULL,
        locked_seconds      double precision NOT NULL,
        CONSTRAINT work_sessions_id_device_unique UNIQUE (session_id, device_id),
        CONSTRAINT work_sessions_status_valid CHECK (status IN ('OPEN', 'CLOSED', 'INTERRUPTED')),
        CONSTRAINT work_sessions_open_has_no_end CHECK ((status = 'OPEN') = (ended_at IS NULL)),
        CONSTRAINT work_sessions_end_after_start CHECK (ended_at IS NULL OR ended_at >= started_at),
        CONSTRAINT work_sessions_reasons_valid CHECK (
            length(start_reason) BETWEEN 1 AND 64 AND (end_reason IS NULL OR length(end_reason) BETWEEN 1 AND 64)
        ),
        {_seconds_check("work_sessions", ["tracked_seconds", "active_seconds", "idle_seconds",
                                          "unknown_seconds", "locked_seconds"])}
    )
    """,
    f"""
    CREATE TABLE activity_periods (
        period_id        uuid        PRIMARY KEY,
        {_sync_columns("activity_periods")},
        session_id       uuid        NOT NULL,
        started_at       timestamptz NOT NULL,
        ended_at         timestamptz NOT NULL,
        duration_seconds double precision NOT NULL,
        is_open          boolean     NOT NULL,
        status           text        NOT NULL,
        status_detail    text,
        app_name         text,
        window_title     text,
        domain           text,
        privacy_excluded boolean     NOT NULL,
        start_reason     text        NOT NULL,
        end_reason       text,
        -- A period belongs to a session of the SAME device.
        CONSTRAINT activity_periods_session_fk FOREIGN KEY (session_id, device_id)
            REFERENCES work_sessions (session_id, device_id) ON DELETE RESTRICT,
        CONSTRAINT activity_periods_status_valid CHECK (status IN ('ACTIVE', 'IDLE', 'UNKNOWN', 'LOCKED')),
        CONSTRAINT activity_periods_end_after_start CHECK (ended_at >= started_at),
        CONSTRAINT activity_periods_text_lengths CHECK (
            (status_detail IS NULL OR length(status_detail) BETWEEN 1 AND 64)
            AND (app_name IS NULL OR length(app_name) <= 260)
            AND (window_title IS NULL OR length(window_title) <= 256)
            AND (domain IS NULL OR length(domain) <= 253)
            AND length(start_reason) BETWEEN 1 AND 64
            AND (end_reason IS NULL OR length(end_reason) BETWEEN 1 AND 64)
        ),
        -- Privacy: an excluded period can only carry the fixed placeholders.
        CONSTRAINT activity_periods_privacy_excluded_redacted CHECK (
            NOT privacy_excluded OR (
                (window_title IS NULL OR window_title = 'Excluded / Private')
                AND (domain IS NULL OR domain = 'Excluded / Private Site')
            )
        ),
        {_seconds_check("activity_periods", ["duration_seconds"])}
    )
    """,
    f"""
    CREATE TABLE idle_periods (
        idle_id          uuid        PRIMARY KEY,
        {_sync_columns("idle_periods")},
        session_id       uuid        NOT NULL,
        started_at       timestamptz NOT NULL,
        ended_at         timestamptz NOT NULL,
        duration_seconds double precision NOT NULL,
        is_open          boolean     NOT NULL,
        end_reason       text,
        CONSTRAINT idle_periods_session_fk FOREIGN KEY (session_id, device_id)
            REFERENCES work_sessions (session_id, device_id) ON DELETE RESTRICT,
        CONSTRAINT idle_periods_end_after_start CHECK (ended_at >= started_at),
        CONSTRAINT idle_periods_reason_valid CHECK (end_reason IS NULL OR length(end_reason) BETWEEN 1 AND 64),
        {_seconds_check("idle_periods", ["duration_seconds"])}
    )
    """,
    f"""
    CREATE TABLE application_usage_daily (
        usage_id         uuid        PRIMARY KEY,
        {_sync_columns("application_usage_daily")},
        usage_date       date        NOT NULL,
        day_start_utc    timestamptz NOT NULL,
        day_end_utc      timestamptz NOT NULL,
        app_name         text        NOT NULL,
        active_seconds   double precision NOT NULL,
        idle_seconds     double precision NOT NULL,
        unknown_seconds  double precision NOT NULL,
        period_count     integer     NOT NULL,
        CONSTRAINT application_usage_daily_device_day_app_unique UNIQUE (device_id, usage_date, app_name),
        CONSTRAINT application_usage_daily_day_bounds CHECK (
            day_end_utc > day_start_utc AND day_end_utc - day_start_utc <= interval '26 hours'
        ),
        CONSTRAINT application_usage_daily_app_name_valid CHECK (length(app_name) BETWEEN 1 AND 260),
        CONSTRAINT application_usage_daily_period_count_nonneg CHECK (period_count >= 0),
        {_seconds_check("application_usage_daily", ["active_seconds", "idle_seconds", "unknown_seconds"],
                        DAY_SECONDS)}
    )
    """,
    # ─── audit ───────────────────────────────────────────────────────────
    """
    CREATE TABLE audit_logs (
        audit_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        occurred_at  timestamptz NOT NULL DEFAULT now(),
        actor_type   text        NOT NULL,
        actor_id     text,
        action       text        NOT NULL,
        entity_type  text        NOT NULL,
        entity_id    text        NOT NULL,
        old_values   jsonb,
        new_values   jsonb,
        request_id   text,
        CONSTRAINT audit_logs_actor_type_valid CHECK (actor_type IN ('SYSTEM', 'CLI', 'ADMIN', 'MANAGER')),
        CONSTRAINT audit_logs_actor_id_valid CHECK (actor_id IS NULL OR length(actor_id) BETWEEN 1 AND 128),
        CONSTRAINT audit_logs_action_format CHECK (action ~ '^[a-z_]+[.][a-z_]+$'),
        CONSTRAINT audit_logs_entity_type_valid CHECK (
            entity_type IN ('employee', 'device', 'device_token', 'work_schedule')
        ),
        CONSTRAINT audit_logs_entity_id_valid CHECK (length(entity_id) BETWEEN 1 AND 128),
        CONSTRAINT audit_logs_values_objects CHECK (
            (old_values IS NULL OR jsonb_typeof(old_values) = 'object')
            AND (new_values IS NULL OR jsonb_typeof(new_values) = 'object')
        )
    )
    """,
    # ─── triggers ────────────────────────────────────────────────────────
    "CREATE TRIGGER employees_touch BEFORE UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION zaza_touch_updated_at()",
    "CREATE TRIGGER devices_touch BEFORE UPDATE ON devices FOR EACH ROW EXECUTE FUNCTION zaza_touch_updated_at()",
    "CREATE TRIGGER work_schedules_touch BEFORE UPDATE ON work_schedules "
    "FOR EACH ROW EXECUTE FUNCTION zaza_touch_updated_at()",
    "CREATE TRIGGER employees_timezone BEFORE INSERT OR UPDATE OF timezone ON employees "
    "FOR EACH ROW EXECUTE FUNCTION zaza_check_timezone()",
    "CREATE TRIGGER work_schedules_timezone BEFORE INSERT OR UPDATE OF timezone ON work_schedules "
    "FOR EACH ROW EXECUTE FUNCTION zaza_check_timezone()",
    "CREATE TRIGGER audit_logs_no_update BEFORE UPDATE OR DELETE ON audit_logs "
    "FOR EACH ROW EXECUTE FUNCTION zaza_audit_append_only()",
    "CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs "
    "FOR EACH STATEMENT EXECUTE FUNCTION zaza_audit_append_only()",
    # ─── indexes (for the reporting queries we know are coming) ──────────
    "CREATE INDEX devices_employee_idx ON devices (employee_id)",
    "CREATE INDEX device_tokens_device_idx ON device_tokens (device_id)",
    "CREATE UNIQUE INDEX work_schedules_weekly_unique ON work_schedules (employee_id, day_of_week, effective_from) "
    "WHERE day_of_week IS NOT NULL",
    "CREATE UNIQUE INDEX work_schedules_date_unique ON work_schedules (employee_id, schedule_date) "
    "WHERE schedule_date IS NOT NULL",
    # employee + date range / start time
    "CREATE INDEX work_sessions_employee_started_idx ON work_sessions (employee_id, started_at)",
    "CREATE INDEX activity_periods_employee_started_idx ON activity_periods (employee_id, started_at)",
    "CREATE INDEX idle_periods_employee_started_idx ON idle_periods (employee_id, started_at)",
    # session -> children (also serves the composite foreign keys)
    "CREATE INDEX activity_periods_session_idx ON activity_periods (session_id, device_id)",
    "CREATE INDEX idle_periods_session_idx ON idle_periods (session_id, device_id)",
    # device + sync order
    "CREATE INDEX work_sessions_device_seq_idx ON work_sessions (device_id, local_seq)",
    "CREATE INDEX activity_periods_device_seq_idx ON activity_periods (device_id, local_seq)",
    "CREATE INDEX idle_periods_device_seq_idx ON idle_periods (device_id, local_seq)",
    "CREATE INDEX application_usage_daily_device_seq_idx ON application_usage_daily (device_id, local_seq)",
    # daily summaries / app usage by employee + day
    "CREATE INDEX application_usage_daily_employee_day_idx ON application_usage_daily (employee_id, usage_date)",
    # currently open / recent sessions
    "CREATE INDEX work_sessions_open_idx ON work_sessions (employee_id, last_heartbeat_at) WHERE status = 'OPEN'",
    # audit lookups
    "CREATE INDEX audit_logs_entity_idx ON audit_logs (entity_type, entity_id, occurred_at)",
    "CREATE INDEX audit_logs_occurred_idx ON audit_logs (occurred_at)",
]

DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS audit_logs",
    "DROP TABLE IF EXISTS application_usage_daily",
    "DROP TABLE IF EXISTS idle_periods",
    "DROP TABLE IF EXISTS activity_periods",
    "DROP TABLE IF EXISTS work_sessions",
    "DROP TABLE IF EXISTS work_schedules",
    "DROP TABLE IF EXISTS device_tokens",
    "DROP TABLE IF EXISTS devices",
    "DROP TABLE IF EXISTS employees",
    "DROP FUNCTION IF EXISTS zaza_audit_append_only()",
    "DROP FUNCTION IF EXISTS zaza_check_timezone()",
    "DROP FUNCTION IF EXISTS zaza_touch_updated_at()",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Destroys all central data. Never run by the server; only reachable via
    # an explicit Alembic command by an operator.
    for statement in DROP_STATEMENTS:
        op.execute(statement)
