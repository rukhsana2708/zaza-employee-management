"""One-time device enrollment (Phase 10G-A).

Revision ID: c0003_device_enrollment
Revises: c0002_saas_auth_routing
Create Date: 2026-10-28

Additive. c0001 and c0002 are unchanged. Control/auth metadata only.

- ``device_enrollment_codes``: one row per operator-issued enrollment code.
  Stores a domain-separated SHA-256 of the code (NEVER the code), the
  organization and the intended employee (server-side authority), expiry,
  and the saga state of the enrollment:
    ISSUED -> CLAIMED -> COMPLETED            (success)
    ISSUED -> REVOKED | EXPIRED               (operator / clock)
    CLAIMED -> FAILED                         (refused, compensated or reconciled)
  ``token_id`` (the tenant token's non-secret id, chosen BEFORE the tenant
  write) and ``route_hash`` let an operator/reconciler find and revoke
  exactly what an interrupted enrollment created. No raw token is stored.
- ``login_attempt_counters`` also counts enrollment attempts (two new
  buckets) — shared across instances, keyed by digests, never IPs.
- Audit vocabulary: the ``enrollment.*`` actions and the ``enrollment``
  entity type.
"""

from __future__ import annotations

from alembic import op

revision = "c0003_device_enrollment"
down_revision = "c0002_saas_auth_routing"
branch_labels = None
depends_on = None

HEX64 = r"'^[0-9a-f]{64}$'"
IDENT = r"'^[A-Za-z0-9._:-]{1,128}$'"
PREVIOUS_ACTIONS = ("organization.create", "organization.status_change", "organization.suspend",
                    "organization.reactivate", "organization.close", "user.create", "user.status_change",
                    "membership.create", "membership.role_change", "membership.status_change",
                    "tenant_database.assign", "tenant_database.status_change", "user.password_set",
                    "user.sessions_revoked", "device_route.create", "device_route.revoke")
NEW_ACTIONS = ("enrollment.code_create", "enrollment.code_revoke", "enrollment.complete", "enrollment.fail",
               "enrollment.reconcile")
PREVIOUS_ENTITIES = ("organization", "user", "membership", "tenant_database", "device_route")
STATUSES = ("ISSUED", "CLAIMED", "COMPLETED", "REVOKED", "EXPIRED", "FAILED")


def _list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


STATEMENTS = [
    f"""
    CREATE TABLE device_enrollment_codes (
        enrollment_id          uuid        PRIMARY KEY,
        code_hash              text        NOT NULL,
        organization_id        uuid        NOT NULL,
        employee_id            text        NOT NULL,
        device_display_name    text,
        allow_existing_device  boolean     NOT NULL DEFAULT false,
        status                 text        NOT NULL DEFAULT 'ISSUED',
        created_at             timestamptz NOT NULL DEFAULT now(),
        expires_at             timestamptz NOT NULL,
        claimed_at             timestamptz,
        completed_at           timestamptz,
        revoked_at             timestamptz,
        device_id              text,
        token_id               uuid,
        route_hash             text,
        failure_reason         text,
        CONSTRAINT device_enrollment_codes_hash_unique UNIQUE (code_hash),
        CONSTRAINT device_enrollment_codes_org_fk FOREIGN KEY (organization_id)
            REFERENCES organizations (organization_id) ON DELETE RESTRICT,
        CONSTRAINT device_enrollment_codes_hash_format CHECK (code_hash ~ {HEX64}),
        CONSTRAINT device_enrollment_codes_employee_format CHECK (employee_id ~ {IDENT}),
        CONSTRAINT device_enrollment_codes_device_format CHECK (device_id IS NULL OR device_id ~ {IDENT}),
        CONSTRAINT device_enrollment_codes_display_name_length CHECK (device_display_name IS NULL
            OR char_length(device_display_name) BETWEEN 1 AND 200),
        CONSTRAINT device_enrollment_codes_route_format CHECK (route_hash IS NULL OR route_hash ~ {HEX64}),
        CONSTRAINT device_enrollment_codes_status_valid CHECK (status IN ({_list(STATUSES)})),
        CONSTRAINT device_enrollment_codes_expiry_after_creation CHECK (expires_at > created_at),
        CONSTRAINT device_enrollment_codes_failure_reason_length CHECK (failure_reason IS NULL
            OR char_length(failure_reason) BETWEEN 1 AND 64),
        CONSTRAINT device_enrollment_codes_claim_consistent CHECK (
            (status IN ('ISSUED', 'REVOKED', 'EXPIRED')) = (claimed_at IS NULL)),
        CONSTRAINT device_enrollment_codes_claim_identifiers CHECK (
            claimed_at IS NULL OR (device_id IS NOT NULL AND token_id IS NOT NULL)),
        CONSTRAINT device_enrollment_codes_completion_consistent CHECK (
            (status = 'COMPLETED') = (completed_at IS NOT NULL AND route_hash IS NOT NULL)),
        CONSTRAINT device_enrollment_codes_revocation_consistent CHECK ((status = 'REVOKED') = (revoked_at IS NOT NULL))
    )
    """,
    "CREATE INDEX device_enrollment_codes_org_idx ON device_enrollment_codes (organization_id, status)",
    "CREATE INDEX device_enrollment_codes_claimed_idx ON device_enrollment_codes (status, claimed_at)",
    "CREATE TRIGGER device_enrollment_codes_identity BEFORE UPDATE ON device_enrollment_codes FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_forbid_identity_change('enrollment_id', 'code_hash', 'organization_id', "
    "'employee_id', 'created_at', 'expires_at', 'allow_existing_device')",
    # a terminal state never changes again (no reactivation of a used/revoked/failed code)
    """
    CREATE FUNCTION zaza_control_enrollment_terminal() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status IN ('COMPLETED', 'REVOKED', 'EXPIRED', 'FAILED') THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = 'enrollment code is in a terminal state: ' || OLD.status;
        END IF;
        IF OLD.status = 'CLAIMED' AND NEW.status NOT IN ('CLAIMED', 'COMPLETED', 'FAILED') THEN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = 'a claimed enrollment can only complete or fail';
        END IF;
        RETURN NEW;
    END $$
    """,
    "CREATE TRIGGER device_enrollment_codes_terminal BEFORE UPDATE ON device_enrollment_codes FOR EACH ROW "
    "EXECUTE FUNCTION zaza_control_enrollment_terminal()",
    # shared counters: enrollment buckets
    "ALTER TABLE login_attempt_counters DROP CONSTRAINT login_attempt_counters_bucket_valid",
    "ALTER TABLE login_attempt_counters ADD CONSTRAINT login_attempt_counters_bucket_valid CHECK (bucket IN "
    "('identity', 'global', 'enrollment_code', 'enrollment_global'))",
    # audit vocabulary
    "ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_action_valid",
    f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_action_valid CHECK (action IN "
    f"({_list(PREVIOUS_ACTIONS + NEW_ACTIONS)}))",
    "ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_entity_type_valid",
    f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_entity_type_valid CHECK (entity_type IN "
    f"({_list(PREVIOUS_ENTITIES + ('enrollment',))}))",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_entity_type_valid")
    op.execute(f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_entity_type_valid CHECK "
               f"(entity_type IN ({_list(PREVIOUS_ENTITIES)}))")
    op.execute("ALTER TABLE control_audit_logs DROP CONSTRAINT control_audit_logs_action_valid")
    op.execute(f"ALTER TABLE control_audit_logs ADD CONSTRAINT control_audit_logs_action_valid CHECK (action IN "
               f"({_list(PREVIOUS_ACTIONS)}))")
    op.execute("ALTER TABLE login_attempt_counters DROP CONSTRAINT login_attempt_counters_bucket_valid")
    op.execute("ALTER TABLE login_attempt_counters ADD CONSTRAINT login_attempt_counters_bucket_valid CHECK "
               "(bucket IN ('identity', 'global'))")
    op.execute("DROP TABLE IF EXISTS device_enrollment_codes")
    op.execute("DROP FUNCTION IF EXISTS zaza_control_enrollment_terminal()")
