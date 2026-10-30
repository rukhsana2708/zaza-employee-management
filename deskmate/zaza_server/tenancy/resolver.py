"""Trusted tenant resolution (Phase 10C) — the ONLY issuer of TenantContext.

Every path starts from something the server has authenticated, never from an
organization id, slug or database name supplied by a browser or agent:

MANAGER (dashboard)   authenticated user_id
                      -> ACTIVE user -> ACTIVE membership in that organization
                      -> ACTIVE organization -> READY tenant database
                      -> TenantContext(kind=MEMBER, role=membership role)

AGENT (device sync)   bearer credential
                      -> server-side credential router -> organization_id
                      -> ACTIVE organization -> READY tenant database
                      -> TenantContext(kind=DEVICE)
                      -> the token is then verified INSIDE that tenant
                         database (existing ``auth.authenticate_request``)

JOB (attendance,      control plane lists READY tenants; each job iteration
reports)              resolves exactly one TenantContext(kind=JOB)

An organization id the client *claims* is at most compared against the
resolved one; a mismatch is refused, it is never used to choose a database.

Failures raise :class:`TenantAccessDenied` with a generic message, so a
caller can't probe which organizations exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, TypeVar

from . import domain
from .context import ContextKind, TenantContext, issue_context
from .control_repository import ControlDirectory
from .domain import MembershipStatus, OrganizationStatus, TenantDatabaseStatus, UserStatus

T = TypeVar("T")


class TenantAccessDenied(Exception):
    """No usable tenant for this request. The message is deliberately generic."""

    def __init__(self, reason: str = "no access to this organization") -> None:
        super().__init__(reason)


class DeviceCredentialRouter(Protocol):
    """Server-side lookup: which organization must verify this credential?

    Keyed by ``device_routes.routing_hash(token)`` (a domain-separated
    SHA-256, not the tenant's token hash); see ``device_routes.py``
    (Phase 10F-B). Returns ``None`` for an unknown or revoked route. It never
    takes the organization from the request, and a route proves nothing:
    the tenant database still authenticates the credential."""

    def organization_for_credential(self, route_hash: str) -> uuid.UUID | None: ...


@dataclass(frozen=True)
class DeviceTenant:
    context: TenantContext
    device: object  # the tenant's DeviceRecord, authenticated inside the tenant database


class TenantResolver:
    def __init__(self, directory: ControlDirectory) -> None:
        self.directory = directory

    def _context(self, organization_id: object, kind: ContextKind, *, role=None, user_id=None) -> TenantContext:  # noqa: ANN001
        try:
            org_id = domain.organization_id(organization_id)
        except domain.TenancyError:
            raise TenantAccessDenied() from None
        org = self.directory.get_organization(org_id)
        if org is None or org.status is not OrganizationStatus.ACTIVE:
            raise TenantAccessDenied()
        database = self.directory.get_tenant_database(org_id)
        if database is None or database.organization_id != org_id or database.status is not TenantDatabaseStatus.READY:
            raise TenantAccessDenied()
        return issue_context(organization_id=org_id, slug=org.slug, database=database, kind=kind, role=role,
                             user_id=user_id)

    # ─── manager / dashboard ──────────────────────────────────────────────
    def for_member(self, user_id: object, organization_id: object) -> TenantContext:
        """``user_id`` comes from the authenticated SaaS session (server-side),
        ``organization_id`` is the organization the user asks to work in; the
        membership decides. A membership in organization A never authorizes B."""
        try:
            uid, org_id = domain.user_id(user_id), domain.organization_id(organization_id)
        except domain.TenancyError:
            raise TenantAccessDenied() from None
        user = self.directory.get_user(uid)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise TenantAccessDenied()
        membership = self.directory.get_membership(org_id, uid)
        if (membership is None or membership.status is not MembershipStatus.ACTIVE
                or membership.organization_id != org_id or membership.user_id != uid):
            raise TenantAccessDenied()
        return self._context(org_id, ContextKind.MEMBER, role=domain.membership_role(membership.role), user_id=uid)

    # ─── agent / device sync ──────────────────────────────────────────────
    def for_device(self, router: DeviceCredentialRouter, token: str, device_id: str,
                   authenticate_in_tenant: Callable[[TenantContext, str, str], object], *,
                   claimed_organization_id: object = None) -> DeviceTenant:
        """Resolve the organization FROM THE CREDENTIAL, then verify the
        credential inside that organization's database.

        ``authenticate_in_tenant(context, token, device_id)`` opens the
        context's tenant database and runs the existing device
        authentication there (token hash + X-ZaZa-Device-Id + status); it
        raises on failure. A client-claimed organization that differs from
        the credential's is refused — never followed."""
        if not isinstance(device_id, str) or not device_id:
            raise TenantAccessDenied("invalid credentials")
        context = self.for_device_route(router, token, claimed_organization_id=claimed_organization_id)
        device = authenticate_in_tenant(context, token, device_id)
        if device is None:
            raise TenantAccessDenied("invalid credentials")
        return DeviceTenant(context=context, device=device)

    def for_device_route(self, router: DeviceCredentialRouter, token: str, *,
                         claimed_organization_id: object = None) -> TenantContext:
        """The DEVICE context the credential ROUTES to — NOT an
        authentication: the caller must still verify the credential inside
        that tenant database (``SaasDeviceAuthenticator`` does, while holding
        that tenant's repository). A claimed organization is only compared."""
        from .device_routes import routing_hash  # noqa: PLC0415

        if not isinstance(token, str) or not token.strip():
            raise TenantAccessDenied("invalid credentials")
        routed = router.organization_for_credential(routing_hash(token))
        if routed is None:
            raise TenantAccessDenied("invalid credentials")
        if claimed_organization_id is not None:
            try:
                claimed = domain.organization_id(claimed_organization_id)
            except domain.TenancyError:
                raise TenantAccessDenied("invalid credentials") from None
            if claimed != routed:
                raise TenantAccessDenied("invalid credentials")
        return self._context(routed, ContextKind.DEVICE)

    def for_device_route_info(self, router: DeviceCredentialRouter, token: str) -> tuple[TenantContext, str | None]:
        """:meth:`for_device_route`, plus the device id of the route when the router can supply it (one control read).
        The device id identifies the device for rate limiting only; it authenticates nothing."""
        from .device_routes import routing_hash  # noqa: PLC0415

        if not isinstance(token, str) or not token.strip():
            raise TenantAccessDenied("invalid credentials")
        digest = routing_hash(token)
        lookup = getattr(router, "route_for_credential", None)
        if lookup is not None:
            found = lookup(digest)
            if found is None:
                raise TenantAccessDenied("invalid credentials")
            routed, device_id = found
        else:
            routed, device_id = router.organization_for_credential(digest), None
            if routed is None:
                raise TenantAccessDenied("invalid credentials")
        return self._context(routed, ContextKind.DEVICE), device_id

    def for_enrollment(self, organization_id: object) -> TenantContext:
        """The DEVICE context for a device enrollment. ``organization_id``
        comes from the server-side enrollment record (claimed by its code),
        never from the request. ACTIVE organization, READY tenant."""
        return self._context(organization_id, ContextKind.DEVICE)

    # ─── per-tenant jobs ──────────────────────────────────────────────────
    def for_job(self, organization_id: object) -> TenantContext:
        return self._context(organization_id, ContextKind.JOB)

    def ready_job_contexts(self) -> Iterator[TenantContext]:
        """One context per READY tenant, resolved one at a time (a tenant
        that became unavailable meanwhile is skipped, not merged)."""
        for org_id in self.directory.list_ready_organization_ids():
            try:
                yield self.for_job(org_id)
            except TenantAccessDenied:
                continue


@dataclass(frozen=True)
class JobOutcome:
    organization_id: uuid.UUID
    ok: bool
    result: object = None
    error: str | None = None


def run_per_tenant(resolver: TenantResolver, job: Callable[[TenantContext], T]) -> list[JobOutcome]:
    """Run ``job`` once per READY tenant, each with its OWN context. The job
    opens (and must close) only that tenant's database; an exception in one
    tenant is recorded and the next tenant starts with a fresh context —
    nothing of tenant A's work can continue against tenant B."""
    outcomes = []
    for context in resolver.ready_job_contexts():
        try:
            outcomes.append(JobOutcome(context.organization_id, True, job(context)))
        except Exception as exc:  # noqa: BLE001 — isolate tenants from each other's failures
            outcomes.append(JobOutcome(context.organization_id, False, error=type(exc).__name__))
    return outcomes
