"""Tenant provisioning boundary (Phase 10D) — the application-side
orchestration that Phase 10E/10F will run with a PRIVILEGED provisioning
identity. Nothing here is reachable from the web service or an HTTP request.

::

    organization (ACTIVE, in the control plane)
      -> assign a server-generated tenant database name   (status PROVISIONING)
      -> create that PostgreSQL database                   (DatabaseCreator)
      -> apply the tenant migrations                       (TenantMigrationRunner)
      -> verify the schema head
      -> grant the runtime role its table privileges       (DatabaseCreator)
      -> mark the tenant database READY
    any failure -> ERROR (retry by provisioning again)

:class:`PostgresDatabaseCreator` does the database work with plain SQL on a
server-configured admin connection (identifiers quoted, names validated
against the tenant pattern). Whether Phase 10E uses it against Cloud SQL or
replaces it with a Cloud SQL Admin API implementation, the interface stays
the same. It does not call any Google API.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from ..config import DatabaseSettings
from . import domain
from .domain import OrganizationStatus, TenancyError, TenantDatabaseStatus
from .tenant_migrations import TenantMigrationRunner

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_FORBIDDEN_ROLES = {"postgres", "public", "cloudsqlsuperuser", "cloudsqladmin", "pg_database_owner"}


def runtime_role_name(value: object) -> str:
    """A plain lower-case PostgreSQL role name for the application runtime."""
    if not isinstance(value, str) or not _ROLE_RE.match(value) or value in _FORBIDDEN_ROLES \
            or value.startswith("pg_"):
        raise TenancyError("the runtime role must be a plain lower-case role name (not a built-in role)")
    return value


class DatabaseCreator(Protocol):
    def database_exists(self, database_name: str) -> bool: ...
    def create_database(self, database_name: str) -> None: ...
    def grant_runtime_access(self, database_name: str) -> None: ...


class PostgresDatabaseCreator:
    """Creates tenant databases with the provisioning identity in ``admin``
    (server configuration; needs CREATEDB — never the web runtime role).

    ``runtime_role``: the role the web service connects as. Each new tenant
    database is closed to PUBLIC and opened to that role only, with DML on
    the tenant tables — no DDL, no ownership."""

    def __init__(self, admin: DatabaseSettings, *, runtime_role: str | None = None) -> None:
        self.admin = dataclasses.replace(admin, schema=None, application_name="zaza-provision",
                                         pool_min=0, pool_max=1)
        self.runtime_role = runtime_role_name(runtime_role) if runtime_role is not None else None

    def _connect(self, dbname: str | None = None):  # noqa: ANN202
        import psycopg  # noqa: PLC0415

        settings = self.admin if dbname is None else dataclasses.replace(self.admin, dbname=dbname)
        return psycopg.connect(**settings.connect_kwargs(), autocommit=True)

    def database_exists(self, database_name: str) -> bool:
        name = domain.tenant_database_name(database_name)
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None

    def create_database(self, database_name: str) -> None:
        from psycopg import sql  # noqa: PLC0415

        db = sql.Identifier(domain.tenant_database_name(database_name))
        with self._connect() as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(db))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
            if self.runtime_role:
                conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                    db, sql.Identifier(self.runtime_role)))

    def grant_runtime_access(self, database_name: str) -> None:
        """After migrations: DML on the tenant tables for the runtime role,
        also for tables a later migration creates (default privileges)."""
        if not self.runtime_role:
            return
        from psycopg import sql  # noqa: PLC0415

        role = sql.Identifier(self.runtime_role)
        with self._connect(domain.tenant_database_name(database_name)) as conn:
            for statement in (
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                "GRANT USAGE ON SCHEMA public TO {r}",
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {r}",
            ):
                conn.execute(sql.SQL(statement).format(r=role))


@dataclass(frozen=True)
class ProvisioningResult:
    organization_id: uuid.UUID
    database_name: str | None
    ok: bool
    status: TenantDatabaseStatus | None
    schema_revision: str | None = None
    error: str | None = None


class TenantProvisioner:
    """Runs the provisioning workflow for ONE organization. ``control`` is the
    control repository (organization + tenant directory), ``creator`` the
    privileged database creator, ``migrator`` the tenant migration runner
    (same privileged identity)."""

    def __init__(self, control, creator: DatabaseCreator, migrator: TenantMigrationRunner) -> None:  # noqa: ANN001
        self.control = control
        self.creator = creator
        self.migrator = migrator

    def provision(self, organization_id: object, region: str) -> ProvisioningResult:
        org_id, reg = domain.organization_id(organization_id), domain.region(region)
        org = self.control.get_organization(org_id)
        if org is None or org.status is not OrganizationStatus.ACTIVE:
            return ProvisioningResult(org_id, None, False, None, error="organization is not ACTIVE")
        descriptor = self.control.get_tenant_database(org_id) or self.control.assign_tenant_database(org_id, reg)
        name = descriptor.database_name
        if descriptor.status is TenantDatabaseStatus.READY:
            return ProvisioningResult(org_id, name, True, descriptor.status, descriptor.schema_revision)
        if descriptor.status not in (TenantDatabaseStatus.PROVISIONING, TenantDatabaseStatus.ERROR):
            return ProvisioningResult(org_id, name, False, descriptor.status,
                                      error=f"tenant database is {descriptor.status.value}")
        if descriptor.status is TenantDatabaseStatus.ERROR:
            descriptor = self.control.mark_tenant_database(org_id, TenantDatabaseStatus.PROVISIONING.value)
        try:
            if not self.creator.database_exists(name):
                self.creator.create_database(name)
            migrated = self.migrator.migrate(org_id)
            if not migrated.ok:
                raise TenancyError(migrated.error or "tenant migration failed")
            self.creator.grant_runtime_access(name)
            ready = self.control.mark_tenant_database(org_id, TenantDatabaseStatus.READY.value, migrated.after)
        except Exception as exc:  # noqa: BLE001 — every failure leaves the tenant ERROR, never half-READY
            message = f"{type(exc).__name__}: {self.migrator.base.scrub(str(exc)).strip()[:300]}"
            try:
                self.control.mark_tenant_database(org_id, TenantDatabaseStatus.ERROR.value)
            except Exception:  # noqa: BLE001, S110
                pass
            return ProvisioningResult(org_id, name, False, TenantDatabaseStatus.ERROR, error=message)
        return ProvisioningResult(org_id, name, True, ready.status, ready.schema_revision)
