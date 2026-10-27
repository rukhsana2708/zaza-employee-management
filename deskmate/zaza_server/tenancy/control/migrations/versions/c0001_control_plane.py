"""SaaS control plane: organizations, users, memberships, tenant database
directory, control audit log (Phase 10C).

Revision ID: c0001_control_plane
Revises:
Create Date: 2026-10-27

This is the FIRST revision of the separate control-plane chain. It lives in
its own database; the tenant schema (0001-0004) is untouched and is applied
to each tenant database independently.

- ``organizations``: one customer company. ``organization_id`` (UUID) is the
  identity; ``slug`` is a unique, normalized URL handle — never an
  authorization decision on its own.
- ``users``: SaaS login identities (managers/admins of customers). NOT
  monitored employees — those live in each tenant database. No password or
  SSO columns yet (a later authentication phase).
- ``organization_memberships``: user x organization with a SaaS role
  (OWNER/ADMIN/MANAGER/VIEWER). Unrelated to ``employees.role``.
- ``tenant_databases``: organization -> its tenant database. One-to-one.
  Server-generated ``zaza_t_`` names only. No host, user, password,
  connection string or secret is stored here.
- ``control_audit_logs``: append-only record of control-plane changes
  (actor, organization, action, entity, time). No activity payloads.

Every instant is TIMESTAMPTZ. Identifiers are immutable (trigger), audit
rows can't be updated or deleted (trigger).
"""

from __future__ import annotations

from alembic import op

revision = "c0001_control_plane"
down_revision = None
branch_labels = ("control",)
depends_on = None

SLUG = r"'^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$'"
TENANT_DB = r"'^zaza_t_[a-z2-7]{26}$'"
REGION = r"'^[a-z]+-[a-z]+[0-9]{1,2}$'"
REVISION = r"'^[a-z0-9_]{1,64}$'"
EMAIL = r"'^[^@\s]{1,64}@[^@\s]{1,253}$'"
ACTIONS = ("organization.create", "organization.status_change", "organization.suspend", "organization.reactivate",
           "organization.close", "user.create", "user.status_change", "membership.create", "membership.role_change",
           "membership.status_change", "tenant_database.assign", "tenant_database.status_change")
ACTION_LIST = ", ".join(f"'{a}'" for a in ACTIONS)

STATEMENTS = [
    # shared trigger functions
    """
    CREATE FUNCTION zaza_control_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        NEW.updated_at := now();
        RETURN NEW;
    END $$
    """,
    """
    CREATE FUNCTION zaza_control_forbid_identity_change() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        col text;
    BEGIN
        FOREACH col IN ARRAY TG_ARGV LOOP
            IF to_jsonb(NEW) -> col IS DISTINCT FROM to_jsonb(OLD) -> col THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'column ' || TG_TABLE_NAME || '.' || col || ' is immutable';
            END IF;
        END LOOP;
        RETURN NEW;
    END $$
    """,
    """
    CREATE FUNCTION zaza_control_forbid_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'control_audit_logs is append-only' USING ERRCODE = 'check_violation';
    END $$
    """,
    f"""
    CREATE TABLE organizations (
        organization_id   uuid        PRIMARY KEY,
        slug              text        NOT NULL,
        display_name      text        NOT NULL,
        status            text        NOT NULL DEFAULT 'ACTIVE',
        default_timezone  text        NOT NULL DEFAULT 'UTC',
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT organizations_slug_unique UNIQUE (slug),
        CONSTRAINT organizations_slug_format CHECK (slug ~ {SLUG}),
        CONSTRAINT organizations_display_name_length CHECK (char_length(display_name) BETWEEN 1 AND 200),
        CONSTRAINT organizations_status_valid CHECK (status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')),
        CONSTRAINT organizations_timezone_length CHECK (char_length(default_timezone) BETWEEN 1 AND 64)
    )
    """,
    f"""
    CREATE TABLE users (
        user_id           uuid        PRIMARY KEY,
        email_normalized  text        NOT NULL,
        display_name      text        NOT NULL,
        status            text        NOT NULL DEFAULT 'ACTIVE',
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT users_email_unique UNIQUE (email_normalized),
        CONSTRAINT users_email_normalized CHECK (email_normalized = lower(btrim(email_normalized))
                                                 AND email_normalized ~ {EMAIL}),
        CONSTRAINT users_display_name_length CHECK (char_length(display_name) BETWEEN 1 AND 200),
        CONSTRAINT users_status_valid CHECK (status IN ('ACTIVE', 'DISABLED'))
    )
    """,
    """
    CREATE TABLE organization_memberships (
        membership_id     uuid        PRIMARY KEY,
        organization_id   uuid        NOT NULL,
        user_id           uuid        NOT NULL,
        role              text        NOT NULL,
        status            text        NOT NULL DEFAULT 'ACTIVE',
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT organization_memberships_org_fk FOREIGN KEY (organization_id)
            REFERENCES organizations (organization_id) ON DELETE RESTRICT,
        CONSTRAINT organization_memberships_user_fk FOREIGN KEY (user_id)
            REFERENCES users (user_id) ON DELETE RESTRICT,
        CONSTRAINT organization_memberships_unique UNIQUE (organization_id, user_id),
        CONSTRAINT organization_memberships_role_valid CHECK (role IN ('OWNER', 'ADMIN', 'MANAGER', 'VIEWER')),
        CONSTRAINT organization_memberships_status_valid CHECK (status IN ('ACTIVE', 'DISABLED'))
    )
    """,
    f"""
    CREATE TABLE tenant_databases (
        organization_id   uuid        PRIMARY KEY,
        database_name     text        NOT NULL,
        region            text        NOT NULL,
        status            text        NOT NULL DEFAULT 'PROVISIONING',
        schema_revision   text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT tenant_databases_org_fk FOREIGN KEY (organization_id)
            REFERENCES organizations (organization_id) ON DELETE RESTRICT,
        CONSTRAINT tenant_databases_name_unique UNIQUE (database_name),
        CONSTRAINT tenant_databases_name_format CHECK (database_name ~ {TENANT_DB}),
        CONSTRAINT tenant_databases_region_format CHECK (region ~ {REGION}),
        CONSTRAINT tenant_databases_status_valid CHECK (status IN ('PROVISIONING', 'READY', 'SUSPENDED', 'ERROR')),
        CONSTRAINT tenant_databases_revision_format CHECK (schema_revision IS NULL OR schema_revision ~ {REVISION}),
        CONSTRAINT tenant_databases_ready_has_revision CHECK (status <> 'READY' OR schema_revision IS NOT NULL)
    )
    """,
    f"""
    CREATE TABLE control_audit_logs (
        audit_id          uuid        PRIMARY KEY,
        occurred_at       timestamptz NOT NULL DEFAULT now(),
        actor_type        text        NOT NULL,
        actor_id          text,
        organization_id   uuid,
        action            text        NOT NULL,
        entity_type       text        NOT NULL,
        entity_id         text        NOT NULL,
        details           jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
        CONSTRAINT control_audit_logs_org_fk FOREIGN KEY (organization_id)
            REFERENCES organizations (organization_id) ON DELETE RESTRICT,
        CONSTRAINT control_audit_logs_actor_type_valid CHECK (actor_type IN ('USER', 'PLATFORM', 'SYSTEM', 'CLI')),
        CONSTRAINT control_audit_logs_actor_id_length CHECK (actor_id IS NULL OR char_length(actor_id) <= 128),
        CONSTRAINT control_audit_logs_action_valid CHECK (action IN ({ACTION_LIST})),
        CONSTRAINT control_audit_logs_entity_type_valid CHECK (entity_type IN
            ('organization', 'user', 'membership', 'tenant_database')),
        CONSTRAINT control_audit_logs_entity_id_length CHECK (char_length(entity_id) BETWEEN 1 AND 128),
        CONSTRAINT control_audit_logs_details_object CHECK (jsonb_typeof(details) = 'object'
                                                            AND pg_column_size(details) <= 4096)
    )
    """,
    "CREATE INDEX organization_memberships_user_idx ON organization_memberships (user_id)",
    "CREATE INDEX tenant_databases_status_idx ON tenant_databases (status)",
    "CREATE INDEX control_audit_logs_org_time_idx ON control_audit_logs (organization_id, occurred_at)",
    # immutable identities
    "CREATE TRIGGER organizations_identity BEFORE UPDATE ON organizations FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('organization_id', 'created_at')",
    "CREATE TRIGGER users_identity BEFORE UPDATE ON users FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('user_id', 'created_at')",
    "CREATE TRIGGER organization_memberships_identity BEFORE UPDATE ON organization_memberships FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('membership_id', 'organization_id', 'user_id', 'created_at')",
    "CREATE TRIGGER tenant_databases_identity BEFORE UPDATE ON tenant_databases FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('organization_id', 'database_name', 'region', 'created_at')",
    # updated_at maintenance
    *(f"CREATE TRIGGER {t}_touch BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION zaza_control_touch_updated_at()"
      for t in ("organizations", "users", "organization_memberships", "tenant_databases")),
    # append-only audit
    "CREATE TRIGGER control_audit_logs_append_only BEFORE UPDATE OR DELETE ON control_audit_logs "
    "FOR EACH ROW EXECUTE FUNCTION zaza_control_forbid_audit_change()",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table in ("control_audit_logs", "tenant_databases", "organization_memberships", "users", "organizations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    for fn in ("zaza_control_forbid_audit_change", "zaza_control_forbid_identity_change",
               "zaza_control_touch_updated_at"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
