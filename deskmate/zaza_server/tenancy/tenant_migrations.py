"""Controlled tenant schema migrations (Phase 10D) — an operator command,
never run by the web server.

``python -m deskmate.zaza_server tenant-migrate --organization <id>``
``python -m deskmate.zaza_server tenant-migrate --all-ready``

- The tenant database is taken from the CONTROL directory for an
  organization id (a server-generated ``zaza_t_…`` name, validated again
  here); the operator never types a database name.
- Host, user and password come from the server configuration
  (``ZAZA_DB_*``), which for this job is the migration identity — not the
  web service's runtime role (see docs/zaza/CLOUD_SQL_PRODUCTION.md).
- Tenants are migrated one at a time, in a fixed order. A failure marks
  that tenant database ERROR (its requests stop resolving) and, by default,
  stops the run; each result names exactly which tenant succeeded or failed.
- The control-plane database can never be migrated as a tenant: the name
  must match the tenant pattern, must differ from the control database, and
  ``postgres.migrate.upgrade`` refuses a database with control tables.
- Messages are scrubbed of the password.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from ..config import DatabaseSettings
from . import domain
from .context import TenantDatabaseDescriptor
from .domain import TenancyError, TenantDatabaseStatus


class TenantDirectory(Protocol):
    """The control-plane operations the runner needs (PostgresControlRepository)."""

    def get_tenant_database(self, organization_id: uuid.UUID) -> TenantDatabaseDescriptor | None: ...
    def mark_tenant_database(self, organization_id: uuid.UUID, status: str,
                             schema_revision: str | None = None) -> TenantDatabaseDescriptor: ...
    def list_ready_organization_ids(self) -> list[uuid.UUID]: ...


@dataclass(frozen=True)
class TenantMigrationResult:
    organization_id: uuid.UUID
    database_name: str | None
    ok: bool
    before: str | None = None
    after: str | None = None
    error: str | None = None

    def describe(self) -> str:
        where = f"{self.organization_id} ({self.database_name or 'no tenant database'})"
        if self.ok:
            change = "already at head" if self.before == self.after else f"{self.before or '(empty)'} -> {self.after}"
            return f"OK      {where}: {change}"
        return f"FAILED  {where}: {self.error}"


# Statuses a migration may run on. READY: normal upgrades. PROVISIONING: the
# provisioning flow (it marks READY itself). ERROR: an explicit operator retry.
_MIGRATABLE = (TenantDatabaseStatus.READY, TenantDatabaseStatus.PROVISIONING, TenantDatabaseStatus.ERROR)


def _default_upgrade(settings: DatabaseSettings) -> None:
    from ..postgres import migrate  # noqa: PLC0415

    migrate.upgrade(settings)


def _default_status(settings: DatabaseSettings):  # noqa: ANN202 — postgres.migrate.SchemaStatus
    import psycopg  # noqa: PLC0415

    from ..postgres import migrate  # noqa: PLC0415

    with psycopg.connect(**settings.connect_kwargs()) as conn:
        return migrate.schema_status(conn)


class TenantMigrationRunner:
    def __init__(self, directory: TenantDirectory, base: DatabaseSettings, *, control_database_name: str,
                 upgrade: Callable[[DatabaseSettings], None] = _default_upgrade,
                 status: Callable[[DatabaseSettings], object] = _default_status) -> None:
        if not control_database_name:
            raise TenancyError("the control database name is required (it is never migrated as a tenant)")
        self.directory = directory
        self.base = base
        self.control_database_name = control_database_name
        self._upgrade = upgrade
        self._status = status

    def settings_for(self, descriptor: TenantDatabaseDescriptor) -> DatabaseSettings:
        name = domain.tenant_database_name(descriptor.database_name)
        if name in (self.control_database_name, self.base.dbname):
            raise TenancyError("refusing to migrate a non-tenant database")
        return dataclasses.replace(self.base, dbname=name, schema=None, application_name="zaza-tenant-migrate",
                                   pool_min=0, pool_max=1)

    def migrate(self, organization_id: object) -> TenantMigrationResult:
        org_id = domain.organization_id(organization_id)
        descriptor = self.directory.get_tenant_database(org_id)
        if descriptor is None:
            return TenantMigrationResult(org_id, None, False, error="organization has no tenant database")
        name = descriptor.database_name
        if descriptor.status not in _MIGRATABLE:
            return TenantMigrationResult(org_id, name, False,
                                         error=f"tenant database is {descriptor.status.value}; not migrated")
        try:
            settings = self.settings_for(descriptor)
        except TenancyError as exc:
            return TenantMigrationResult(org_id, name, False, error=str(exc))
        before = None
        try:
            before = self._status(settings).current
            self._upgrade(settings)
            status = self._status(settings)
            if not status.up_to_date:
                raise TenancyError(f"schema is at {status.current}, expected {status.head}")
        except Exception as exc:  # noqa: BLE001 — reported per tenant, never silently
            message = f"{type(exc).__name__}: {self.base.scrub(str(exc)).strip()[:300]}"
            try:
                self.directory.mark_tenant_database(org_id, TenantDatabaseStatus.ERROR.value)
            except Exception as mark_exc:  # noqa: BLE001
                message += f" (could not mark ERROR: {type(mark_exc).__name__})"
            return TenantMigrationResult(org_id, name, False, before=before, error=message)
        # READY/ERROR -> READY at the new revision; PROVISIONING stays (the
        # provisioning flow verifies and marks READY itself)
        target = (TenantDatabaseStatus.PROVISIONING if descriptor.status is TenantDatabaseStatus.PROVISIONING
                  else TenantDatabaseStatus.READY)
        self.directory.mark_tenant_database(org_id, target.value, status.current)
        return TenantMigrationResult(org_id, name, True, before=before, after=status.current)

    def migrate_many(self, organization_ids: Iterable[object] | None = None, *,
                     stop_on_failure: bool = True) -> list[TenantMigrationResult]:
        """``organization_ids`` None = every READY tenant. Sequential, in a
        stable order; stops after the first failure unless told otherwise."""
        ids = (self.directory.list_ready_organization_ids() if organization_ids is None
               else [domain.organization_id(o) for o in organization_ids])
        results = []
        for org_id in sorted(dict.fromkeys(ids), key=str):
            result = self.migrate(org_id)
            results.append(result)
            if not result.ok and stop_on_failure:
                break
        return results
