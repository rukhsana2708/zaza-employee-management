"""Tenant routing values (Phase 10C).

:class:`TenantDatabaseDescriptor` — which tenant database belongs to which
organization: a validated, server-generated name plus region and status.
It holds no host, user or password: those come from server configuration
(see :func:`tenant_database_settings`), never from a request or the
control database.

:class:`TenantContext` — the resolved, authorized "we are working for
organization X, in database Y" value. Only :mod:`.resolver` can issue one
(it needs the module-private issuer key); it cannot be built from a slug,
a query parameter or a database name, and it is immutable.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from enum import Enum

from ..config import DatabaseSettings
from . import domain
from .domain import MembershipRole, TenancyError, TenantDatabaseStatus


@dataclass(frozen=True)
class TenantDatabaseDescriptor:
    organization_id: uuid.UUID
    database_name: str
    region: str
    status: TenantDatabaseStatus
    schema_revision: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "organization_id", domain.organization_id(self.organization_id))
        domain.tenant_database_name(self.database_name)
        domain.region(self.region)
        object.__setattr__(self, "status", domain.tenant_database_status(self.status))
        domain.schema_revision(self.schema_revision)


class ContextKind(str, Enum):
    MEMBER = "MEMBER"  # an authenticated SaaS user with an active membership
    DEVICE = "DEVICE"  # an authenticated employee device (agent sync)
    JOB = "JOB"        # a server-side job for exactly one tenant (attendance, reports)


_ISSUER = object()  # only resolver.py passes this; see issue_context()


class TenantContext:
    """Authorized routing to ONE tenant database. Immutable; not copyable
    with a changed organization (there is no setter, no ``replace``)."""

    __slots__ = ("organization_id", "slug", "database", "kind", "role", "user_id")

    def __init__(self, *, organization_id: uuid.UUID, slug: str, database: TenantDatabaseDescriptor,
                 kind: ContextKind, role: MembershipRole | None, user_id: uuid.UUID | None, _issuer: object) -> None:
        if _issuer is not _ISSUER:
            raise TenancyError("a TenantContext is issued only by the tenant resolver")
        if not isinstance(database, TenantDatabaseDescriptor) or database.organization_id != organization_id:
            raise TenancyError("tenant database does not belong to this organization")
        if database.status is not TenantDatabaseStatus.READY:
            raise TenancyError("tenant database is not READY")
        if (kind is ContextKind.MEMBER) != (role is not None and user_id is not None):
            raise TenancyError("only member contexts carry a role and a user")
        for name, value in (("organization_id", organization_id), ("slug", slug), ("database", database),
                            ("kind", kind), ("role", role), ("user_id", user_id)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("TenantContext is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("TenantContext is immutable")

    def __copy__(self):  # noqa: ANN204
        return self

    def __deepcopy__(self, memo):  # noqa: ANN001, ANN204
        return self

    def __reduce__(self):  # noqa: ANN204 — no pickling round-trip that bypasses the resolver
        raise TypeError("TenantContext cannot be serialized")

    def __repr__(self) -> str:
        return (f"TenantContext(organization_id={self.organization_id}, slug={self.slug!r}, kind={self.kind.value}, "
                f"role={self.role.value if self.role else None}, database={self.database.database_name})")


def issue_context(**kwargs) -> TenantContext:  # noqa: ANN003 — used by resolver.py only
    return TenantContext(**kwargs, _issuer=_ISSUER)


def tenant_database_settings(base: DatabaseSettings, context: TenantContext) -> DatabaseSettings:
    """Connection settings for the context's tenant database: host, port,
    user, password, TLS, pool and timeouts all come from the SERVER's
    configured ``base``; only the database name changes, and it is the
    validated name from the resolved context. No other input is accepted —
    there is no way to pass a host, URL or connection string here."""
    if not isinstance(context, TenantContext):
        raise TenancyError("tenant database settings need a resolved TenantContext")
    name = domain.tenant_database_name(context.database.database_name)
    return dataclasses.replace(base, dbname=name, schema=None, application_name="zaza-tenant")
