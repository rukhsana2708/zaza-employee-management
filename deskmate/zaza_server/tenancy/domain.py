"""Central validation for the SaaS control plane (Phase 10C).

Every identifier, status and role used by the control plane is checked
here, once — HTTP handlers, the CLI and the repository call these functions
instead of validating ad hoc.

Two role vocabularies exist and must never be mixed:

- ``employees.role`` (tenant database: EMPLOYEE / MANAGER / ADMIN) is a
  business classification of a *monitored employee* inside one customer.
  It grants nothing in the SaaS.
- :class:`MembershipRole` (control plane: OWNER / ADMIN / MANAGER / VIEWER)
  is a *SaaS user's* permission to administer or view one organization.

There is no conversion between them. ``PLATFORM_SUPERADMIN`` is not a
membership role (see docs/zaza/SAAS_TENANCY.md).
"""

from __future__ import annotations

import base64
import re
import secrets
import unicodedata
import uuid
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TenancyError(ValueError):
    """Invalid control-plane input. Messages never echo secrets."""


class OrganizationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class MembershipStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class MembershipRole(str, Enum):
    """SaaS organization roles, strongest first. NOT ``employees.role``."""

    OWNER = "OWNER"      # highest customer-level authority
    ADMIN = "ADMIN"      # organization configuration, employees and managers
    MANAGER = "MANAGER"  # operational dashboard access (future: team scope)
    VIEWER = "VIEWER"    # read-only reporting


class TenantDatabaseStatus(str, Enum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    SUSPENDED = "SUSPENDED"
    ERROR = "ERROR"


def _enum(kind: type[Enum], value: object, what: str):  # noqa: ANN202
    if isinstance(value, kind):
        return value
    if isinstance(value, str):
        try:
            return kind(value)
        except ValueError:
            pass
    raise TenancyError(f"{what} must be one of {', '.join(m.value for m in kind)}")


def organization_status(value: object) -> OrganizationStatus:
    return _enum(OrganizationStatus, value, "organization status")


def user_status(value: object) -> UserStatus:
    return _enum(UserStatus, value, "user status")


def membership_status(value: object) -> MembershipStatus:
    return _enum(MembershipStatus, value, "membership status")


def membership_role(value: object) -> MembershipRole:
    """Only OWNER / ADMIN / MANAGER / VIEWER. ``EMPLOYEE`` (an employee
    classification) and ``PLATFORM_SUPERADMIN`` are refused."""
    return _enum(MembershipRole, value, "membership role")


def tenant_database_status(value: object) -> TenantDatabaseStatus:
    return _enum(TenantDatabaseStatus, value, "tenant database status")


# ─── identifiers ──────────────────────────────────────────────────────────


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def parse_uuid(value: object, what: str = "identifier") -> uuid.UUID:
    """A canonical UUID (organization, user, membership, audit ids). Names,
    slugs and other strings are not identifiers."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                                               r"[0-9a-fA-F]{12}", value):
        return uuid.UUID(value)
    raise TenancyError(f"{what} must be a UUID")


def organization_id(value: object) -> uuid.UUID:
    return parse_uuid(value, "organization_id")


def user_id(value: object) -> uuid.UUID:
    return parse_uuid(value, "user_id")


# ─── slugs ────────────────────────────────────────────────────────────────

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
RESERVED_SLUGS = frozenset({
    "admin", "api", "app", "auth", "control", "dashboard", "health", "help", "login", "logout", "manager",
    "platform", "static", "status", "support", "www", "zaza",
})


def normalize_slug(value: object) -> str:
    """Lower-case, URL-safe, 3..63 characters: ``a-z``, ``0-9`` and single
    inner hyphens. Spaces/underscores become hyphens; nothing else is
    silently rewritten (accents etc. are refused, not transliterated).

    A slug is a *display/URL handle*: it is never an authorization decision
    on its own — access always goes through the immutable organization_id
    and an authenticated membership."""
    if not isinstance(value, str):
        raise TenancyError("slug must be text")
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    if not SLUG_RE.match(text):
        raise TenancyError("slug must be 3-63 characters: a-z, 0-9 and inner hyphens")
    if text in RESERVED_SLUGS:
        raise TenancyError("slug is reserved")
    return text


# ─── people / names ───────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                       r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def normalize_email(value: object) -> str:
    """Trimmed, lower-cased, plain ``local@domain`` (ASCII). The normalized
    form is the unique login identity of a control-plane user."""
    if not isinstance(value, str):
        raise TenancyError("email must be text")
    text = value.strip().lower()
    if len(text) > 254 or not _EMAIL_RE.match(text):
        raise TenancyError("email is not a valid address")
    return text


def display_name(value: object, what: str = "display name") -> str:
    if not isinstance(value, str):
        raise TenancyError(f"{what} must be text")
    text = " ".join(value.split())
    if not 1 <= len(text) <= 200 or any(unicodedata.category(c).startswith("C") for c in text):
        raise TenancyError(f"{what} must be 1-200 printable characters")
    return text


def timezone_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise TenancyError("timezone must be an IANA name, e.g. Asia/Dhaka")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise TenancyError("timezone must be an IANA name, e.g. Asia/Dhaka") from None
    return value


# ─── tenant databases ─────────────────────────────────────────────────────

TENANT_DB_PREFIX = "zaza_t_"
TENANT_DB_RE = re.compile(r"^zaza_t_[a-z2-7]{26}$")
REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]{1,2}$")
_REVISION_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def generate_tenant_database_name() -> str:
    """``zaza_t_`` + 128 random bits (base32, lower-case). Generated by the
    server only — never derived from a customer name, slug or request."""
    return TENANT_DB_PREFIX + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def tenant_database_name(value: object) -> str:
    """Accept only the exact generated form. Rejects everything else —
    ``postgres``, ``template1``, the control database, connection strings,
    quoting, path separators, whitespace, upper case, other lengths."""
    if not isinstance(value, str) or not TENANT_DB_RE.match(value):
        raise TenancyError("tenant database name must be a server-generated zaza_t_ identifier")
    return value


def region(value: object) -> str:
    """A Google Cloud region name such as ``asia-south1``."""
    if not isinstance(value, str) or not REGION_RE.match(value):
        raise TenancyError("region must look like asia-south1")
    return value


def schema_revision(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _REVISION_RE.match(value):
        raise TenancyError("schema revision must be a migration revision id")
    return value


# ─── audit ────────────────────────────────────────────────────────────────

C0001_AUDIT_ACTIONS = frozenset({
    "organization.create", "organization.status_change", "organization.suspend", "organization.reactivate",
    "organization.close", "user.create", "user.status_change", "membership.create", "membership.role_change",
    "membership.status_change", "tenant_database.assign", "tenant_database.status_change",
})
# Phase 10F-B (c0002): security-sensitive administrative events. Never the
# password, a hash, a session token or a device token in the details.
C0002_AUDIT_ACTIONS = frozenset({
    "user.password_set", "user.sessions_revoked", "device_route.create", "device_route.revoke",
})
AUDIT_ACTIONS = C0001_AUDIT_ACTIONS | C0002_AUDIT_ACTIONS
AUDIT_ENTITY_TYPES = frozenset({"organization", "user", "membership", "tenant_database", "device_route"})
ACTOR_TYPES = frozenset({"USER", "PLATFORM", "SYSTEM", "CLI"})
_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")


def audit_action(value: object) -> str:
    if value not in AUDIT_ACTIONS:
        raise TenancyError("unknown audit action")
    return value  # type: ignore[return-value]


def actor(actor_type: object, actor_id: object) -> tuple[str, str | None]:
    if actor_type not in ACTOR_TYPES:
        raise TenancyError(f"actor type must be one of {', '.join(sorted(ACTOR_TYPES))}")
    if actor_id is not None and (not isinstance(actor_id, str) or not _ACTOR_ID_RE.match(actor_id)):
        raise TenancyError("actor id must be a short identifier")
    return actor_type, actor_id  # type: ignore[return-value]
