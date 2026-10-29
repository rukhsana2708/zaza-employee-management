"""Tenant-aware employee operations for the trusted SaaS operator (Phase 10G-E-A).

The ONLY supported way to create or change employees of a SaaS organization
from the command line::

    organization slug (operator input)
        -> normalize_slug
        -> control plane: organization id for that slug
        -> TenantResolver.for_job(organization id)   ACTIVE organization,
                                                       READY tenant database
        -> TenantContext (immutable, resolver-issued)
        -> TenantRepositoryManager.repository(context) the tenant's repository,
                                                       current schema, and
                                                       current_database() checked
        -> employee operation inside that tenant database only

The slug only names the organization; it never selects a database. The
tenant database comes from the validated control directory alone. There is
no database, host, URL or organization-id input, and no fallback to the base
database (``ZAZA_DB_NAME``, ``postgres`` in production), the control
database or another tenant.

The legacy ``add-employee`` / ``list-employees`` commands open the single
database of ``database_settings_from_env()`` (``open_repository``) and do
NOT resolve a tenant: they must never be used for a SaaS organization.

Database identity: in production these commands run in the ``zaza-migrate``
job, i.e. as ``zaza_migrator`` (the trusted operator boundary; tenant
objects are owned by it). That is acceptable for bootstrap only. This path
is never exposed over HTTP; customer employee management later goes through
authenticated SaaS authorization as the runtime role ``zaza_app``.

The employee role (EMPLOYEE / MANAGER / ADMIN) is the tenant's own employee
attribute. It is NOT a SaaS membership role and grants no dashboard access.
"""

from __future__ import annotations

from ..repository import EMPLOYEE_ROLES, EmployeeRecord, RepositoryUnavailable
from . import domain
from .connections import TenantRepositoryManager, open_tenant_repository
from .context import TenantContext
from .domain import TenancyError
from .resolver import TenantAccessDenied, TenantResolver

OPERATOR_ACTOR = ("CLI", "saas-operator")  # tenant audit_logs actor of these commands


def operator_repository_factory(settings):  # noqa: ANN001, ANN201
    """The default tenant repository factory (current schema required,
    ``current_database()`` verified), with the operator as the audit actor."""
    return open_tenant_repository(settings, actor_type=OPERATOR_ACTOR[0], actor_id=OPERATOR_ACTOR[1])


class SaasEmployeeOperator:
    """Employee create / list / enable / disable for ONE organization at a
    time, named by slug. ``directory`` is the control repository (it must
    provide ``find_organization_id_by_slug``), ``manager`` a
    :class:`TenantRepositoryManager` built from the server's base settings."""

    def __init__(self, directory, resolver: TenantResolver, manager: TenantRepositoryManager) -> None:  # noqa: ANN001
        self.directory = directory
        self.resolver = resolver
        self.manager = manager

    def context(self, slug: object) -> TenantContext:
        org_id = self.directory.find_organization_id_by_slug(domain.normalize_slug(slug))
        if org_id is None:
            raise TenancyError("no organization with that slug")
        try:
            return self.resolver.for_job(org_id)
        except TenantAccessDenied:
            raise TenancyError("the organization is not ACTIVE or its tenant database is not READY") from None

    def _run(self, context: TenantContext, operation):  # noqa: ANN001, ANN202
        try:
            with self.manager.repository(context) as repo:
                return operation(repo)
        except RepositoryUnavailable as exc:
            # schema out of date, database unreachable, pools exhausted: refuse, never fall back
            reason = str(exc).replace(context.database.database_name, "<tenant database>")
            raise TenancyError(f"the organization's tenant database is not usable: {reason}") from None

    def create(self, slug: object, employee_id: object, name: object, *, timezone: object,
               role: object = "EMPLOYEE") -> EmployeeRecord:
        employee_id = domain.identifier(employee_id, "employee id")
        name = domain.display_name(name, "employee name")
        timezone = domain.timezone_name(timezone)
        if role not in EMPLOYEE_ROLES:
            raise TenancyError(f"employee role must be one of {', '.join(EMPLOYEE_ROLES)} "
                               "(an employee attribute, not a SaaS membership role)")
        context = self.context(slug)
        return self._run(context, lambda repo: repo.add_employee(employee_id, name, role=role, timezone=timezone))

    def list(self, slug: object) -> list[EmployeeRecord]:
        return self._run(self.context(slug), lambda repo: repo.list_employees())

    def set_active(self, slug: object, employee_id: object, active: bool) -> EmployeeRecord:
        """Enable/disable an employee in THIS organization's tenant only. It
        changes no SaaS membership, organization or other tenant. A disabled
        employee gets no new enrollment code and is left out of attendance
        summaries; existing devices and tokens are NOT revoked by this."""
        employee_id = domain.identifier(employee_id, "employee id")
        context = self.context(slug)
        return self._run(context, lambda repo: repo.set_employee_active(employee_id, active))
