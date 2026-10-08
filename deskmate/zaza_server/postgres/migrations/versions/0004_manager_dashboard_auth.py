"""Manager dashboard authentication (Phase 7).

Revision ID: 0004_manager_dashboard_auth
Revises: 0003_schedule_timezone
Create Date: 2026-10-09

- ``manager_users``: dashboard accounts. Usernames are stored lower-case and
  unique case-insensitively; passwords only as Argon2id hashes (a CHECK
  refuses anything else, so a plaintext password can't be stored even by
  mistake).
- ``manager_sessions``: server-side sessions. Only SHA-256 hashes of the
  session token and of the session's CSRF token are stored; the browser
  holds the opaque random token.
- ``activity_periods (device_id, ended_at)`` index for the dashboard's
  current-status lookup (latest period per device).
- ``audit_logs.entity_type`` additionally allows ``manager_user`` (login,
  logout, password reset, disable/enable). The constraint is replaced under
  the same name; no audit row is touched.

Forward-only and additive. 0001-0003 are unchanged.
"""

from __future__ import annotations

from alembic import op

revision = "0004_manager_dashboard_auth"
down_revision = "0003_schedule_timezone"
branch_labels = None
depends_on = None

HASH_PATTERN = "'^[0-9a-f]{64}$'"

STATEMENTS = [
    """
    CREATE TABLE manager_users (
        manager_user_id  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        username         text        NOT NULL,
        display_name     text        NOT NULL,
        password_hash    text        NOT NULL,
        role             text        NOT NULL DEFAULT 'MANAGER',
        is_active        boolean     NOT NULL DEFAULT true,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        last_login_at    timestamptz,
        CONSTRAINT manager_users_username_format CHECK (
            username = lower(username) AND username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'
        ),
        CONSTRAINT manager_users_name_valid CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
        -- Only an Argon2id PHC string fits: plaintext can't be stored.
        CONSTRAINT manager_users_password_hash_argon2id CHECK (
            password_hash LIKE '$argon2id$%' AND length(password_hash) BETWEEN 50 AND 512
        ),
        CONSTRAINT manager_users_role_valid CHECK (role IN ('ADMIN', 'MANAGER'))
    )
    """,
    "CREATE UNIQUE INDEX manager_users_username_key ON manager_users (lower(username))",
    "CREATE TRIGGER manager_users_touch BEFORE UPDATE ON manager_users "
    "FOR EACH ROW EXECUTE FUNCTION zaza_touch_updated_at()",
    f"""
    CREATE TABLE manager_sessions (
        session_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        manager_user_id     uuid        NOT NULL,
        session_token_hash  text        NOT NULL,
        csrf_token_hash     text        NOT NULL,
        created_at          timestamptz NOT NULL DEFAULT now(),
        expires_at          timestamptz NOT NULL,
        last_seen_at        timestamptz NOT NULL DEFAULT now(),
        revoked_at          timestamptz,
        CONSTRAINT manager_sessions_user_fk FOREIGN KEY (manager_user_id)
            REFERENCES manager_users (manager_user_id) ON DELETE RESTRICT,
        CONSTRAINT manager_sessions_token_hash_key UNIQUE (session_token_hash),
        CONSTRAINT manager_sessions_token_hash_format CHECK (session_token_hash ~ {HASH_PATTERN}),
        CONSTRAINT manager_sessions_csrf_hash_format CHECK (csrf_token_hash ~ {HASH_PATTERN}),
        CONSTRAINT manager_sessions_expiry_valid CHECK (expires_at > created_at),
        CONSTRAINT manager_sessions_revoked_valid CHECK (revoked_at IS NULL OR revoked_at >= created_at)
    )
    """,
    "CREATE INDEX manager_sessions_user_expiry_idx ON manager_sessions (manager_user_id, expires_at)",
    # Current status: the latest activity period of each device.
    "CREATE INDEX activity_periods_device_ended_idx ON activity_periods (device_id, ended_at)",
    "ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_entity_type_valid",
    """
    ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_entity_type_valid CHECK (
        entity_type IN ('employee', 'device', 'device_token', 'work_schedule', 'manager_user')
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_entity_type_valid")
    op.execute("ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_entity_type_valid CHECK ("
               "entity_type IN ('employee', 'device', 'device_token', 'work_schedule')) NOT VALID")
    op.execute("DROP INDEX IF EXISTS activity_periods_device_ended_idx")
    op.execute("DROP TABLE IF EXISTS manager_sessions")
    op.execute("DROP TABLE IF EXISTS manager_users")
