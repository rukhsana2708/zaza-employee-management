"""Control-plane store (Phase 10C): organizations, SaaS users, memberships,
the tenant-database directory and the control audit log.

The control database holds NO customer activity data and NO secrets — no
employees, devices, sessions or periods (those live in each tenant
database), no passwords, no connection strings. Every administrative change
writes a ``control_audit_logs`` row in the same transaction.

:class:`ControlDirectory` is the read interface the tenant resolver needs;
:class:`PostgresControlRepository` implements it on the control database
(migrated with ``control-migrate``; never at web start-up).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..config import DatabaseSettings
from . import domain
from .context import TenantDatabaseDescriptor
from .domain import (
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
    TenancyError,
    TenantDatabaseStatus,
    UserStatus,
)


@dataclass(frozen=True)
class Organization:
    organization_id: uuid.UUID
    slug: str
    display_name: str
    status: OrganizationStatus
    default_timezone: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class User:
    """A SaaS login identity — NOT a monitored employee."""

    user_id: uuid.UUID
    email: str
    display_name: str
    status: UserStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Membership:
    membership_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: MembershipRole
    status: MembershipStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class AuditEntry:
    audit_id: uuid.UUID
    occurred_at: datetime
    actor_type: str
    actor_id: str | None
    organization_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str
    details: dict


class ControlDirectory(Protocol):
    """What tenant resolution reads. Lookups are by immutable id only."""

    def get_organization(self, organization_id: uuid.UUID) -> Organization | None: ...
    def get_user(self, user_id: uuid.UUID) -> User | None: ...
    def get_membership(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None: ...
    def get_tenant_database(self, organization_id: uuid.UUID) -> TenantDatabaseDescriptor | None: ...
    def list_ready_organization_ids(self) -> list[uuid.UUID]: ...


# Constraint name -> safe message (no row data is ever echoed).
_CONSTRAINT_MESSAGES = {
    "organizations_slug_unique": "an organization with this slug already exists",
    "users_email_unique": "a user with this email already exists",
    "organization_memberships_unique": "this user is already a member of the organization",
    "tenant_databases_pkey": "this organization already has a tenant database",
    "tenant_databases_name_unique": "tenant database name collision; retry",
    "organization_memberships_org_fk": "unknown organization",
    "organization_memberships_user_fk": "unknown user",
    "tenant_databases_org_fk": "unknown organization",
    "control_audit_logs_org_fk": "unknown organization",
    # c0002
    "user_password_credentials_user_fk": "unknown user",
    "user_password_credentials_argon2id": "only Argon2id password hashes are stored",
    "user_sessions_user_fk": "unknown user",
    "device_credential_routes_pkey": "this credential is already routed",
    "device_credential_routes_org_fk": "unknown organization",
    "device_credential_routes_device_id_format": "invalid device id",
}


class PostgresControlRepository:
    """Control-plane repository on its own database. Small, lazily opened
    pool (the control plane is not on the hot path of every query)."""

    def __init__(self, settings: DatabaseSettings, *, actor_type: str = "SYSTEM", actor_id: str | None = None,
                 require_current_schema: bool = True, runtime_sync_limit_mode: str | None = None) -> None:
        from psycopg.rows import dict_row  # noqa: PLC0415
        from psycopg_pool import ConnectionPool, PoolTimeout  # noqa: PLC0415

        from ..repository import RepositoryUnavailable  # noqa: PLC0415
        from .control import migrate  # noqa: PLC0415

        self.actor_type, self.actor_id = domain.actor(actor_type, actor_id)
        self.settings = settings
        self._pool = ConnectionPool(
            "", kwargs={**settings.connect_kwargs(), "autocommit": True, "row_factory": dict_row},
            min_size=0, max_size=min(settings.pool_max, 2), timeout=settings.pool_timeout, max_idle=300,
            max_lifetime=3600, name="zaza-control", open=False,
            # a connection dropped meanwhile (database restart, Cloud SQL
            # maintenance, idle Cloud Run instance) is replaced, not handed out
            check=ConnectionPool.check_connection,
        )
        try:
            try:
                self._pool.open(wait=False)
                with self._pool.connection(timeout=settings.connect_timeout + 2) as conn:
                    status = migrate.schema_status(conn)
            except PoolTimeout:
                raise RepositoryUnavailable(f"cannot connect to the control database {settings.display()}") from None
            compatible = (status.up_to_date if runtime_sync_limit_mode is None else
                          migrate.runtime_compatible(status, sync_limit_mode=runtime_sync_limit_mode))
        except BaseException:
            self._pool.close()
            raise
        if require_current_schema and not compatible:
            self._pool.close()
            raise RepositoryUnavailable(
                f"control schema is at {status.current or 'nothing (empty database)'}, this server needs "
                f"{status.head}; "
                + (f"incompatible with ZAZA_SYNC_LIMIT_MODE={runtime_sync_limit_mode}; "
                   "c0003 requires off; shadow/enforce require c0004. " if runtime_sync_limit_mode is not None else "")
                + "run `python -m deskmate.zaza_server control-migrate` only in the approved rollout order")

    def close(self) -> None:
        self._pool.close()

    # ─── plumbing ─────────────────────────────────────────────────────────
    @contextmanager
    def _tx(self) -> Iterator:
        from psycopg import errors  # noqa: PLC0415

        with self._pool.connection() as conn:
            try:
                with conn.transaction():
                    yield conn.cursor()
            except (errors.UniqueViolation, errors.ForeignKeyViolation, errors.CheckViolation) as exc:
                name = getattr(exc.diag, "constraint_name", None)
                raise TenancyError(_CONSTRAINT_MESSAGES.get(name or "", "the change violates a control-plane "
                                                                        "rule")) from None

    # Public hooks for the other control-plane stores of this package
    # (saas_auth, device_routes): same pool, same constraint mapping, same
    # audit writer, one transaction for a change and its audit row.
    def transaction(self):  # noqa: ANN201 — context manager yielding a cursor
        return self._tx()

    def connection(self):  # noqa: ANN201 — a pooled autocommit connection
        return self._pool.connection()

    def write_audit(self, cur, action: str, entity_type: str, entity_id: object,  # noqa: ANN001
                    organization_id: uuid.UUID | None, details: dict | None = None) -> None:
        self._audit(cur, action, entity_type, entity_id, organization_id, details)

    def _audit(self, cur, action: str, entity_type: str, entity_id: object,  # noqa: ANN001
               organization_id: uuid.UUID | None, details: dict | None = None) -> None:
        cur.execute(
            "INSERT INTO control_audit_logs (audit_id, actor_type, actor_id, organization_id, action, entity_type, "
            "entity_id, details) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (domain.new_uuid(), self.actor_type, self.actor_id, organization_id, domain.audit_action(action),
             entity_type, str(entity_id), json.dumps(details or {}, sort_keys=True)))

    @staticmethod
    def _org(row: dict) -> Organization:
        return Organization(row["organization_id"], row["slug"], row["display_name"],
                            OrganizationStatus(row["status"]), row["default_timezone"], row["created_at"],
                            row["updated_at"])

    @staticmethod
    def _user(row: dict) -> User:
        return User(row["user_id"], row["email_normalized"], row["display_name"], UserStatus(row["status"]),
                    row["created_at"], row["updated_at"])

    @staticmethod
    def _membership(row: dict) -> Membership:
        return Membership(row["membership_id"], row["organization_id"], row["user_id"], MembershipRole(row["role"]),
                          MembershipStatus(row["status"]), row["created_at"], row["updated_at"])

    @staticmethod
    def _tdb(row: dict) -> TenantDatabaseDescriptor:
        return TenantDatabaseDescriptor(row["organization_id"], row["database_name"], row["region"],
                                        TenantDatabaseStatus(row["status"]), row["schema_revision"])

    def _one(self, sql: str, params: tuple) -> dict | None:
        with self._pool.connection() as conn:
            return conn.execute(sql, params).fetchone()

    # ─── organizations ────────────────────────────────────────────────────
    def create_organization(self, slug: str, display_name: str, *, default_timezone: str = "UTC") -> Organization:
        org_id = domain.new_uuid()
        slug = domain.normalize_slug(slug)
        name = domain.display_name(display_name, "organization name")
        tz = domain.timezone_name(default_timezone)
        with self._tx() as cur:
            row = cur.execute(
                "INSERT INTO organizations (organization_id, slug, display_name, default_timezone) "
                "VALUES (%s, %s, %s, %s) RETURNING *", (org_id, slug, name, tz)).fetchone()
            self._audit(cur, "organization.create", "organization", org_id, org_id, {"slug": slug})
        return self._org(row)

    def get_organization(self, organization_id: uuid.UUID) -> Organization | None:
        row = self._one("SELECT * FROM organizations WHERE organization_id = %s",
                        (domain.organization_id(organization_id),))
        return self._org(row) if row else None

    def find_organization_id_by_slug(self, slug: str) -> uuid.UUID | None:
        """Slug -> id, for URLs/display. Grants nothing: callers must still
        resolve a TenantContext through an authenticated membership."""
        row = self._one("SELECT organization_id FROM organizations WHERE slug = %s", (domain.normalize_slug(slug),))
        return row["organization_id"] if row else None

    def set_organization_status(self, organization_id: uuid.UUID, status: str) -> Organization:
        org_id, new = domain.organization_id(organization_id), domain.organization_status(status)
        with self._tx() as cur:
            old = cur.execute("SELECT status FROM organizations WHERE organization_id = %s FOR UPDATE",
                              (org_id,)).fetchone()
            if old is None:
                raise TenancyError("unknown organization")
            row = cur.execute("UPDATE organizations SET status = %s WHERE organization_id = %s RETURNING *",
                              (new.value, org_id)).fetchone()
            action = {OrganizationStatus.SUSPENDED: "organization.suspend", OrganizationStatus.CLOSED:
                      "organization.close", OrganizationStatus.ACTIVE: "organization.reactivate"}[new]
            self._audit(cur, action, "organization", org_id, org_id, {"from": old["status"], "to": new.value})
        return self._org(row)

    # ─── users (SaaS identities, not employees) ───────────────────────────
    def create_user(self, email: str, display_name: str) -> User:
        uid = domain.new_uuid()
        with self._tx() as cur:
            row = cur.execute(
                "INSERT INTO users (user_id, email_normalized, display_name) VALUES (%s, %s, %s) RETURNING *",
                (uid, domain.normalize_email(email), domain.display_name(display_name))).fetchone()
            self._audit(cur, "user.create", "user", uid, None)
        return self._user(row)

    def get_user(self, user_id: uuid.UUID) -> User | None:
        row = self._one("SELECT * FROM users WHERE user_id = %s", (domain.user_id(user_id),))
        return self._user(row) if row else None

    def find_user_by_email(self, email: str) -> User | None:
        row = self._one("SELECT * FROM users WHERE email_normalized = %s", (domain.normalize_email(email),))
        return self._user(row) if row else None

    def set_user_status(self, user_id: uuid.UUID, status: str) -> User:
        uid, new = domain.user_id(user_id), domain.user_status(status)
        with self._tx() as cur:
            row = cur.execute("UPDATE users SET status = %s WHERE user_id = %s RETURNING *",
                              (new.value, uid)).fetchone()
            if row is None:
                raise TenancyError("unknown user")
            self._audit(cur, "user.status_change", "user", uid, None, {"to": new.value})
        return self._user(row)

    # ─── memberships (SaaS RBAC) ──────────────────────────────────────────
    def add_membership(self, organization_id: uuid.UUID, user_id: uuid.UUID, role: str) -> Membership:
        org_id, uid, r = domain.organization_id(organization_id), domain.user_id(user_id), domain.membership_role(role)
        mid = domain.new_uuid()
        with self._tx() as cur:
            row = cur.execute(
                "INSERT INTO organization_memberships (membership_id, organization_id, user_id, role) "
                "VALUES (%s, %s, %s, %s) RETURNING *", (mid, org_id, uid, r.value)).fetchone()
            self._audit(cur, "membership.create", "membership", mid, org_id, {"user_id": str(uid), "role": r.value})
        return self._membership(row)

    def change_membership_role(self, organization_id: uuid.UUID, user_id: uuid.UUID, role: str) -> Membership:
        org_id, uid, r = domain.organization_id(organization_id), domain.user_id(user_id), domain.membership_role(role)
        with self._tx() as cur:
            old = cur.execute("SELECT * FROM organization_memberships WHERE organization_id = %s AND user_id = %s "
                              "FOR UPDATE", (org_id, uid)).fetchone()
            if old is None:
                raise TenancyError("no such membership")
            row = cur.execute("UPDATE organization_memberships SET role = %s WHERE membership_id = %s RETURNING *",
                              (r.value, old["membership_id"])).fetchone()
            self._audit(cur, "membership.role_change", "membership", old["membership_id"], org_id,
                        {"user_id": str(uid), "from": old["role"], "to": r.value})
        return self._membership(row)

    def set_membership_status(self, organization_id: uuid.UUID, user_id: uuid.UUID, status: str) -> Membership:
        org_id, uid, s = domain.organization_id(organization_id), domain.user_id(user_id), domain.membership_status(status)
        with self._tx() as cur:
            row = cur.execute("UPDATE organization_memberships SET status = %s WHERE organization_id = %s "
                              "AND user_id = %s RETURNING *", (s.value, org_id, uid)).fetchone()
            if row is None:
                raise TenancyError("no such membership")
            self._audit(cur, "membership.status_change", "membership", row["membership_id"], org_id,
                        {"user_id": str(uid), "to": s.value})
        return self._membership(row)

    def get_membership(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
        row = self._one("SELECT * FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                        (domain.organization_id(organization_id), domain.user_id(user_id)))
        return self._membership(row) if row else None

    def list_memberships_for_user(self, user_id: uuid.UUID) -> list[Membership]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM organization_memberships WHERE user_id = %s ORDER BY created_at",
                                (domain.user_id(user_id),)).fetchall()
        return [self._membership(r) for r in rows]

    # ─── tenant database directory ────────────────────────────────────────
    def assign_tenant_database(self, organization_id: uuid.UUID, region: str) -> TenantDatabaseDescriptor:
        """Record the organization's (future) tenant database. The name is
        generated HERE; callers cannot choose it. Status PROVISIONING until
        the database exists and is migrated (:meth:`mark_tenant_database`)."""
        org_id, reg = domain.organization_id(organization_id), domain.region(region)
        name = domain.generate_tenant_database_name()
        with self._tx() as cur:
            row = cur.execute("INSERT INTO tenant_databases (organization_id, database_name, region) "
                              "VALUES (%s, %s, %s) RETURNING *", (org_id, name, reg)).fetchone()
            self._audit(cur, "tenant_database.assign", "tenant_database", org_id, org_id,
                        {"database_name": name, "region": reg})
        return self._tdb(row)

    def mark_tenant_database(self, organization_id: uuid.UUID, status: str,
                             schema_revision: str | None = None) -> TenantDatabaseDescriptor:
        org_id, s = domain.organization_id(organization_id), domain.tenant_database_status(status)
        rev = domain.schema_revision(schema_revision)
        with self._tx() as cur:
            row = cur.execute("UPDATE tenant_databases SET status = %s, schema_revision = COALESCE(%s, schema_revision)"
                              " WHERE organization_id = %s RETURNING *", (s.value, rev, org_id)).fetchone()
            if row is None:
                raise TenancyError("organization has no tenant database")
            self._audit(cur, "tenant_database.status_change", "tenant_database", org_id, org_id,
                        {"to": s.value, "schema_revision": row["schema_revision"]})
        return self._tdb(row)

    def get_tenant_database(self, organization_id: uuid.UUID) -> TenantDatabaseDescriptor | None:
        row = self._one("SELECT * FROM tenant_databases WHERE organization_id = %s",
                        (domain.organization_id(organization_id),))
        return self._tdb(row) if row else None

    def list_ready_organization_ids(self) -> list[uuid.UUID]:
        """ACTIVE organizations whose tenant database is READY — the input of
        per-tenant jobs. Returns ids only; each job resolves one context."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT o.organization_id FROM organizations o JOIN tenant_databases t USING (organization_id) "
                "WHERE o.status = 'ACTIVE' AND t.status = 'READY' ORDER BY o.organization_id").fetchall()
        return [r["organization_id"] for r in rows]

    # ─── audit ────────────────────────────────────────────────────────────
    def list_audit(self, organization_id: uuid.UUID | None = None, *, limit: int = 100) -> list[AuditEntry]:
        with self._pool.connection() as conn:
            if organization_id is None:
                rows = conn.execute("SELECT * FROM control_audit_logs ORDER BY occurred_at, audit_id LIMIT %s",
                                    (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM control_audit_logs WHERE organization_id = %s "
                                    "ORDER BY occurred_at, audit_id LIMIT %s",
                                    (domain.organization_id(organization_id), limit)).fetchall()
        return [AuditEntry(r["audit_id"], r["occurred_at"], r["actor_type"], r["actor_id"], r["organization_id"],
                           r["action"], r["entity_type"], r["entity_id"], r["details"]) for r in rows]
