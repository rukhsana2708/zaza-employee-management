"""Phase A member authorization and administrative workforce operations.

No caller-supplied organization/database routing; every operation refreshes the
resolver-issued membership and uses the tenant manager. No monitoring auth changes.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from ..tenancy.context import ContextKind, TenantContext
from ..tenancy.domain import MembershipStatus, TenancyError, UserStatus, display_name, parse_uuid
from ..tenancy.saas_auth import Permission, PermissionDenied, permissions, require
from .repository import WorkforceRepository


@dataclass(frozen=True)
class SubjectScope:
    employee_id: str
    kind: str
    start: datetime | None = None
    end: datetime | None = None

    def clip(self, start, end):
        """Historical queries MUST use these half-open bounds, including aggregates."""
        _instant(start)
        _instant(end)
        lo = max(start, self.start) if self.start else start
        hi = min(end, self.end) if self.end else end
        if lo >= hi:
            raise PermissionDenied("outside authorized historical window")
        return lo, hi


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TenancyError("an aware timestamp is required")
    return value


class WorkforceService:
    def __init__(self, directory, resolver, manager, *, clock=lambda: datetime.now(timezone.utc)):
        self.directory, self.resolver, self.manager, self.clock = (
            directory,
            resolver,
            manager,
            clock,
        )

    @contextmanager
    def _access(self, context, capability=None):
        if not isinstance(context, TenantContext) or context.kind is not ContextKind.MEMBER:
            raise PermissionDenied("a resolved member context is required")
        fresh = self.resolver.for_member(context.user_id, context.organization_id)
        if capability:
            require(fresh, capability)
        # Workforce operations require BOTH exact new schemas. Runtime predecessor
        # compatibility exists only to keep pre-existing monitoring operational.
        from ..tenancy.control import migrate  # noqa: PLC0415

        with self.directory.connection() as conn:
            if not migrate.schema_status(conn).up_to_date:
                raise TenancyError("control workforce migration is pending")
        membership = self.directory.get_membership(fresh.organization_id, fresh.user_id)
        with self.manager.repository(fresh) as repo:
            with WorkforceRepository.transaction(repo, fresh.user_id) as store:
                # Revalidate after waiting on an administrative transaction lock.
                fresh = self.resolver.for_member(fresh.user_id, fresh.organization_id)
                if capability:
                    require(fresh, capability)
                yield fresh, membership, store

    def create_catalog(self, context, kind, name):
        name = display_name(name, "catalog name")
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (_, _, store):
            return store.create_catalog(kind, name)

    def update_catalog(self, context, kind, identity, *, version, name, active):
        if type(active) is not bool:
            raise TenancyError("active must be boolean")
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (_, _, store):
            return store.update_catalog(
                kind, parse_uuid(identity), version, display_name(name), active
            )

    def update_profile(self, context, employee_id, *, version, **changes):
        if "monitoring_enabled" in changes and type(changes["monitoring_enabled"]) is not bool:
            raise TenancyError("monitoring policy must be boolean")
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (_, _, store):
            for field, kind in [("designation_id", "designation"), ("department_id", "department")]:
                if changes.get(field) is not None:
                    changes[field] = parse_uuid(changes[field])
                    if not store.catalog(kind, changes[field])["is_active"]:
                        raise TenancyError("catalog entry is archived")
            return store.update_profile(employee_id, version, changes)

    def link_account(self, context, employee_id, user_id, membership_id, *, replace=False):
        user_id, membership_id = parse_uuid(user_id), parse_uuid(membership_id)
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (fresh, _, store):
            require(fresh, Permission.MEMBERS_MANAGE)
            user = self.directory.get_user(user_id)
            member = self.directory.get_membership(fresh.organization_id, user_id)
            if (
                user is None
                or user.status is not UserStatus.ACTIVE
                or member is None
                or member.membership_id != membership_id
                or member.user_id != user_id
                or member.organization_id != fresh.organization_id
                or member.status is not MembershipStatus.ACTIVE
            ):
                raise PermissionDenied("an active membership in this organization is required")
            return store.link(employee_id, user_id, membership_id, replace)

    def revoke_link(self, context, employee_id):
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (fresh, _, store):
            require(fresh, Permission.MEMBERS_MANAGE)
            return store.revoke_links(employee_id)

    def set_team(self, context, employee_id, team_id, *, active=True):
        if type(active) is not bool:
            raise TenancyError("active must be boolean")
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (_, _, store):
            return store.team_membership(employee_id, parse_uuid(team_id), active)

    def add_reporting(self, context, employee_id, manager_id, start, end=None):
        _instant(start)
        if end is not None:
            _instant(end)
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (_, _, store):
            return store.reporting(employee_id, manager_id, start, end)

    def end_reporting(self, context, identity, end):
        _instant(end)
        with self._access(context, Permission.EMPLOYEES_MANAGE) as (_, _, store):
            return store.end_reporting(parse_uuid(identity), end)

    def _scope(self, fresh, membership, store, employee_id, *, employee_work=False):
        target = store.profile(employee_id)
        perms = permissions(fresh)
        link = store.link_for_user(fresh.user_id, membership.membership_id)
        if employee_work:
            # Future self-service mutations must call this boundary, never device auth.
            if link is None or link["employee_id"] != employee_id:
                raise PermissionDenied("a valid self linkage is required")
            if not target["is_active"] or target["employment_status"] != "ACTIVE":
                raise PermissionDenied("employee work is disabled")
        if Permission.EMPLOYEES_VIEW_ALL in perms:
            return SubjectScope(employee_id, "organization")
        if link is None:
            raise PermissionDenied("a valid employee linkage is required")
        actor = store.profile(link["employee_id"])
        if not actor["is_active"] or actor["employment_status"] not in {"ACTIVE", "LEAVE"}:
            raise PermissionDenied("employee access is disabled")
        if link["employee_id"] == employee_id and Permission.EMPLOYEES_VIEW_SELF in perms:
            return SubjectScope(employee_id, "self")
        if Permission.EMPLOYEES_VIEW_SCOPED in perms and actor["employment_status"] == "ACTIVE":
            now = _instant(self.clock())
            relation = store.direct_relationship(employee_id, link["employee_id"], now)
            if relation:
                return SubjectScope(employee_id, "direct_report", relation["effective_from"], now)
        raise PermissionDenied("employee is outside authorized scope")

    def subject_scope(self, context, employee_id, *, employee_work=False):
        with self._access(context) as (fresh, membership, store):
            return self._scope(fresh, membership, store, employee_id, employee_work=employee_work)

    def profile(self, context, employee_id):
        with self._access(context) as (fresh, membership, store):
            self._scope(fresh, membership, store, employee_id)
            row = store.profile(employee_id)
            row["team_ids"] = store.team_ids(employee_id)
            return row
