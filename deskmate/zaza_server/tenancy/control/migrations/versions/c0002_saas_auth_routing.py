"""SaaS authentication and device credential routing (Phase 10F-B).

Revision ID: c0002_saas_auth_routing  (VARCHAR(32) version column)
Revises: c0001_control_plane
Create Date: 2026-10-28

Additive. c0001 is unchanged. Control/auth metadata only — no employee
activity, no tenant business data.

- ``user_password_credentials``: one Argon2id PHC hash per SaaS user
  (``users`` stays the single identity). Never plaintext, never reversible.
- ``user_sessions``: server-side SaaS sessions. The browser holds the random
  token; this table holds SHA-256(token) only. Revocation is a timestamp +
  reason; expiry is enforced by the server on every request.
- ``login_attempt_counters``: shared, concurrency-safe login rate limiting
  across every Cloud Run instance (fixed windows, upsert + counter). Keys
  are SHA-256 digests of the normalized login identity, never the email.
  Rows expire with their window and are deleted by cleanup.
- ``device_credential_routes``: which ORGANIZATION must verify a device
  credential — keyed by a domain-separated SHA-256 of the token (NOT the
  hash the tenant database stores, and never the raw token). A route is
  not proof of anything: the tenant database still authenticates.

Also extends the audit CHECK constraints with the c0002 actions and the
``device_route`` entity type (the c0001 rows remain valid).
"""

from __future__ import annotations

from alembic import op

revision = "c0002_saas_auth_routing"
down_revision = "c0001_control_plane"
branch_labels = None
depends_on = None

HEX64 = r"'^[0-9a-f]{64}$'"
DEVICE_ID = r"'^[A-Za-z0-9._:-]{1,128}$'"
C0001_ACTIONS = ("organization.create", "organization.status_change", "organization.suspend",
                 "organization.reactivate", "organization.close", "user.create", "user.status_change",
                 "membership.create", "membership.role_change", "membership.status_change", "tenant_database.assign",
                 "tenant_database.status_change")
NEW_ACTIONS = ("user.password_set", "user.sessions_revoked", "device_route.create", "device_route.revoke")
ACTION_LIST = ", ".join(f"'{a}'" for a in (*C0001_ACTIONS, *NEW_ACTIONS))
ENTITY_TYPES = ("organization", "user", "membership", "tenant_database", "device_route")
ENTITY_LIST = ", ".join(f"'{e}'" for e in ENTITY_TYPES)
REVOKE_REASONS = ("logout", "admin", "password_change", "user_disabled")

STATEMENTS = [
    """
    CREATE TABLE user_password_credentials (
        user_id              uuid        PRIMARY KEY,
        password_hash        text        NOT NULL,
        credential_version   integer     NOT NULL DEFAULT 1,
        created_at           timestamptz NOT NULL DEFAULT now(),
        password_changed_at  timestamptz NOT NULL DEFAULT now(),
        updated_at           timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT user_password_credentials_user_fk FOREIGN KEY (user_id) REFERENCES users (user_id)
            ON DELETE RESTRICT,
        CONSTRAINT user_password_credentials_argon2id CHECK (left(password_hash, 10) = '$argon2id$'
                                                             AND char_length(password_hash) <= 512),
        CONSTRAINT user_password_credentials_version_positive CHECK (credential_version >= 1)
    )
    """,
    f"""
    CREATE TABLE user_sessions (
        session_id      uuid        PRIMARY KEY,
        user_id         uuid        NOT NULL,
        token_hash      text        NOT NULL,
        created_at      timestamptz NOT NULL DEFAULT now(),
        expires_at      timestamptz NOT NULL,
        last_seen_at    timestamptz NOT NULL DEFAULT now(),
        revoked_at      timestamptz,
        revoke_reason   text,
        CONSTRAINT user_sessions_user_fk FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE RESTRICT,
        CONSTRAINT user_sessions_token_hash_unique UNIQUE (token_hash),
        CONSTRAINT user_sessions_token_hash_format CHECK (token_hash ~ {HEX64}),
        CONSTRAINT user_sessions_expiry_after_creation CHECK (expires_at > created_at),
        CONSTRAINT user_sessions_revocation_consistent CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL)),
        CONSTRAINT user_sessions_revoke_reason_valid CHECK (revoke_reason IS NULL OR revoke_reason IN
            ({", ".join(f"'{r}'" for r in REVOKE_REASONS)}))
    )
    """,
    "CREATE INDEX user_sessions_user_idx ON user_sessions (user_id)",
    "CREATE INDEX user_sessions_expires_idx ON user_sessions (expires_at)",
    f"""
    CREATE TABLE login_attempt_counters (
        bucket          text        NOT NULL,
        key_hash        text        NOT NULL,
        window_start    timestamptz NOT NULL,
        attempts        integer     NOT NULL DEFAULT 1,
        CONSTRAINT login_attempt_counters_pkey PRIMARY KEY (bucket, key_hash, window_start),
        CONSTRAINT login_attempt_counters_bucket_valid CHECK (bucket IN ('identity', 'global')),
        CONSTRAINT login_attempt_counters_key_format CHECK (key_hash ~ {HEX64}),
        CONSTRAINT login_attempt_counters_attempts_positive CHECK (attempts >= 1)
    )
    """,
    "CREATE INDEX login_attempt_counters_window_idx ON login_attempt_counters (window_start)",
    f"""
    CREATE TABLE device_credential_routes (
        route_hash       text        PRIMARY KEY,
        organization_id  uuid        NOT NULL,
        device_id        text        NOT NULL,
        status           text        NOT NULL DEFAULT 'ACTIVE',
        created_at       timestamptz NOT NULL DEFAULT now(),
        revoked_at       timestamptz,
        CONSTRAINT device_credential_routes_org_fk FOREIGN KEY (organization_id)
            REFERENCES organizations (organization_id) ON DELETE RESTRICT,
        CONSTRAINT device_credential_routes_hash_format CHECK (route_hash ~ {HEX64}),
        CONSTRAINT device_credential_routes_device_id_format CHECK (device_id ~ {DEVICE_ID}),
        CONSTRAINT device_credential_routes_status_valid CHECK (status IN ('ACTIVE', 'REVOKED')),
        CONSTRAINT device_credential_routes_revocation_consistent CHECK ((status = 'REVOKED') = (revoked_at IS NOT NULL))
    )
    """,
    "CREATE INDEX device_credential_routes_device_idx ON device_credential_routes (organization_id, device_id)",
    # immutable identities (reuses the c0001 trigger function)
    "CREATE TRIGGER user_password_credentials_identity BEFORE UPDATE ON user_password_credentials FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('user_id', 'created_at')",
    "CREATE TRIGGER user_sessions_identity BEFORE UPDATE ON user_sessions FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('session_id', 'user_id', 'token_hash', 'created_at', "
    "'expires_at')",
    "CREATE TRIGGER device_credential_routes_identity BEFORE UPDATE ON device_credential_routes FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('route_hash', 'organization_id', 'device_id', "
    "'created_at')",
    "CREATE TRIGGER user_password_credentials_touch BEFORE UPDATE ON user_password_credentials FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_touch_updated_at()",
    # audit vocabulary
    "ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_action_valid",
    f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_action_valid CHECK (action IN ({ACTION_LIST}))",
    "ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_entity_type_valid",
    f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_entity_type_valid CHECK (entity_type IN "
    f"({ENTITY_LIST}))",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    old_actions = ", ".join(f"'{a}'" for a in C0001_ACTIONS)
    op.execute("ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_action_valid")
    op.execute(f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_action_valid CHECK (action IN "
               f"({old_actions}))")
    op.execute("ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_entity_type_valid")
    op.execute("ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_entity_type_valid CHECK "
               "(entity_type IN ('organization', 'user', 'membership', 'tenant_database'))")
    for table in ("device_credential_routes", "login_attempt_counters", "user_sessions", "user_password_credentials"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
