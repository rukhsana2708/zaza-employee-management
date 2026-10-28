"""One-time device enrollment (Phase 10G-A).

An operator issues an enrollment code for ONE intended employee of ONE
organization. The employee's agent submits the code once, with its device
id, and receives its device token exactly once. Nothing the client sends
selects the organization, tenant database or employee:

    code --SHA-256(domain, code)--> device_enrollment_codes (ISSUED, not expired)
         --> organization_id + employee_id      (server-side authority)
         --> TenantResolver: ACTIVE organization, READY tenant -> TenantContext
         --> TenantRepositoryManager: that tenant's repository

Codes: 160 random bits, base32, shown ONCE (``ZZE-XXXX-...``). The control
database stores only a domain-separated hash. Codes expire, are single-use
and can't be reactivated (a database trigger refuses any change out of a
terminal state); operators can revoke unused codes.

THE SAGA (control DB and tenant DB can't commit atomically together)::

  1. CLAIM    control: ISSUED -> CLAIMED, atomically (UPDATE ... WHERE
              status='ISSUED'), recording device_id and a token_id chosen
              NOW. Concurrent requests with one code: exactly one claims.
  2. TENANT   tenant: create the device if new, then issue the token with
              that token_id (existing implementation: zzd_ + 256 bits,
              SHA-256 stored).
  3. COMPLETE control, ONE transaction: CLAIMED -> COMPLETED (route_hash set)
              + INSERT the device credential route + audit.

A credential is ROUTABLE only after step 3, and step 3 is atomic in the
control database. So an interruption anywhere leaves at most an UNROUTABLE
tenant token, which the SaaS sync API can't use (no route). Compensation:
a failure after step 2 revokes that token immediately (by token_id) and
marks the code FAILED. If that compensation itself can't run (crash, or
the tenant is down), the code stays CLAIMED, and ``reconcile()``
(operator job) marks old CLAIMED codes FAILED and revokes their token_id.
The raw token is never persisted for recovery.

Retry: a used code answers the same generic refusal as an unknown one. If
the agent lost the response after step 3, the operator issues a NEW code
(``--reenroll``) and revokes the old token after the switch.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..auth import issue_token
from ..config import ConfigError
from ..repository import RepositoryUnavailable
from . import domain
from .connections import TenantRepositoryManager
from .device_routes import routing_hash
from .domain import TenancyError
from .resolver import TenantAccessDenied, TenantResolver

logger = logging.getLogger("zaza_server.enrollment")

CODE_PREFIX = "ZZE"
_CODE_BYTES = 20  # 160 bits
_CODE_LEN = 32    # base32 characters
_CODE_RE = re.compile(r"^[A-Z2-7]{32}$")
_CODE_DOMAIN = b"zaza-enrollment-code-v1\x00"
_LIMIT_DOMAIN = b"zaza-enrollment-limit-v1\x00"
_GLOBAL_KEY = hashlib.sha256(b"zaza-enrollment-global-v1").hexdigest()

GENERIC_REFUSAL = "invalid, expired or already used enrollment code"
REFUSED = "enrollment refused; ask your administrator for a new enrollment code"
DEVICE_EXISTS = "this device is already enrolled; ask your administrator for a re-enrollment code"


class EnrollmentRefused(Exception):
    def __init__(self, status_code: int, error: str, reason: str = "") -> None:
        super().__init__(error)
        self.status_code, self.error, self.reason = status_code, error, reason


class EnrollmentRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many enrollment attempts")
        self.retry_after = max(1, int(retry_after))


# ─── codes ─────────────────────────────────────────────────────────────────


def generate_code() -> str:
    """``ZZE-XXXX-XXXX-...``: 160 random bits, base32 (no 0/1/8/9 confusion),
    case-insensitive, grouped for typing."""
    raw = base64.b32encode(secrets.token_bytes(_CODE_BYTES)).decode("ascii")
    return CODE_PREFIX + "-" + "-".join(raw[i:i + 4] for i in range(0, _CODE_LEN, 4))


def normalize_code(code: object) -> str | None:
    """The 32 base32 characters of a code (prefix, dashes, spaces and case
    removed), or None if it can't be a code."""
    if not isinstance(code, str) or len(code) > 64:
        return None
    text = re.sub(r"[\s-]", "", code).upper()
    if text.startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):]
    return text if _CODE_RE.match(text) else None


def code_hash(normalized: str) -> str:
    return hashlib.sha256(_CODE_DOMAIN + normalized.encode("ascii")).hexdigest()


def _limit_key(code: object) -> str:
    text = normalize_code(code) or (code[:64] if isinstance(code, str) else "")
    return hashlib.sha256(_LIMIT_DOMAIN + text.encode("utf-8", "replace")).hexdigest()


# ─── settings ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnrollmentSettings:
    default_ttl_hours: int = 72
    max_ttl_hours: int = 720
    code_max_attempts: int = 5        # per code (digest) per window
    code_window_seconds: int = 3600
    global_max_attempts: int = 60     # all enrollment attempts per global window
    global_window_seconds: int = 60
    reconcile_after_seconds: int = 900  # a CLAIMED code older than this is interrupted

    def __post_init__(self) -> None:
        if not 1 <= self.default_ttl_hours <= self.max_ttl_hours <= 720:
            raise ConfigError("enrollment code lifetime must be 1..720 hours")
        if not 1 <= self.code_max_attempts <= 100 or not 5 <= self.global_max_attempts <= 10000:
            raise ConfigError("enrollment attempt limits are out of range")
        if not 60 <= self.reconcile_after_seconds <= 86400:
            raise ConfigError("the enrollment reconcile delay must be 60..86400 seconds")


def enrollment_settings_from_env(env: dict | None = None) -> EnrollmentSettings:
    env = dict(os.environ if env is None else env)
    raw = (env.get("ZAZA_ENROLLMENT_MAX_ATTEMPTS_GLOBAL") or "").strip()
    try:
        return EnrollmentSettings(global_max_attempts=int(raw)) if raw else EnrollmentSettings()
    except ValueError:
        raise ConfigError("ZAZA_ENROLLMENT_MAX_ATTEMPTS_GLOBAL must be an integer") from None


# ─── values ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnrollmentCode:
    """Safe metadata only: never the code or its hash."""

    enrollment_id: uuid.UUID
    organization_id: uuid.UUID
    employee_id: str
    status: str
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    device_id: str | None = None
    allow_existing_device: bool = False
    failure_reason: str | None = None


@dataclass(frozen=True)
class Claim:
    enrollment_id: uuid.UUID
    organization_id: uuid.UUID
    employee_id: str
    device_display_name: str | None
    allow_existing_device: bool
    device_id: str
    token_id: uuid.UUID


@dataclass(frozen=True)
class EnrolledDevice:
    device_id: str
    employee_id: str
    token: str  # returned ONCE to the agent; never stored, never logged


# ─── storage ───────────────────────────────────────────────────────────────


class PostgresEnrollmentStore:
    """SQL for ``device_enrollment_codes`` (and the route insert of the
    completion step), through the control repository's pool, transactions
    and audit writer."""

    def __init__(self, control) -> None:  # noqa: ANN001 — PostgresControlRepository
        self.control = control

    @staticmethod
    def _info(row: dict) -> EnrollmentCode:
        return EnrollmentCode(row["enrollment_id"], row["organization_id"], row["employee_id"], row["status"],
                              row["created_at"], row["expires_at"], row["claimed_at"], row["completed_at"],
                              row["device_id"], row["allow_existing_device"], row["failure_reason"])

    def create_code(self, organization_id: object, employee_id: str, *, ttl_hours: int,
                    device_display_name: str | None = None, allow_existing_device: bool = False
                    ) -> tuple[EnrollmentCode, str]:
        org_id, emp = domain.organization_id(organization_id), domain.identifier(employee_id, "employee id")
        name = domain.display_name(device_display_name, "device name") if device_display_name else None
        code = generate_code()
        eid = domain.new_uuid()
        with self.control.transaction() as cur:
            row = cur.execute(
                "INSERT INTO device_enrollment_codes (enrollment_id, code_hash, organization_id, employee_id, "
                "device_display_name, allow_existing_device, expires_at) VALUES (%s, %s, %s, %s, %s, %s, "
                "now() + make_interval(hours => %s)) RETURNING *",
                (eid, code_hash(normalize_code(code)), org_id, emp, name, allow_existing_device, ttl_hours)).fetchone()
            self.control.write_audit(cur, "enrollment.code_create", "enrollment", eid, org_id,
                                     {"employee_id": emp, "expires_at": row["expires_at"].isoformat(),
                                      "allow_existing_device": allow_existing_device})
        return self._info(row), code

    def list_codes(self, organization_id: object = None) -> list[EnrollmentCode]:
        with self.control.connection() as conn:
            if organization_id is None:
                rows = conn.execute("SELECT * FROM device_enrollment_codes ORDER BY created_at").fetchall()
            else:
                rows = conn.execute("SELECT * FROM device_enrollment_codes WHERE organization_id = %s "
                                    "ORDER BY created_at", (domain.organization_id(organization_id),)).fetchall()
        return [self._info(r) for r in rows]

    def revoke_code(self, enrollment_id: object) -> bool:
        eid = domain.parse_uuid(enrollment_id, "enrollment id")
        with self.control.transaction() as cur:
            row = cur.execute("UPDATE device_enrollment_codes SET status = 'REVOKED', revoked_at = now() "
                              "WHERE enrollment_id = %s AND status = 'ISSUED' RETURNING organization_id",
                              (eid,)).fetchone()
            if row:
                self.control.write_audit(cur, "enrollment.code_revoke", "enrollment", eid, row["organization_id"])
        return row is not None

    def claim(self, digest: str, device_id: str, token_id: uuid.UUID) -> Claim | None:
        """Step 1. One atomic statement: of N concurrent claims of one code,
        exactly one finds status ISSUED (the others wait on the row lock and
        then see CLAIMED)."""
        with self.control.connection() as conn:
            row = conn.execute(
                "UPDATE device_enrollment_codes SET status = 'CLAIMED', claimed_at = now(), device_id = %s, "
                "token_id = %s WHERE code_hash = %s AND status = 'ISSUED' AND expires_at > now() "
                "RETURNING enrollment_id, organization_id, employee_id, device_display_name, allow_existing_device",
                (device_id, token_id, digest)).fetchone()
        if row is None:
            return None
        return Claim(row["enrollment_id"], row["organization_id"], row["employee_id"], row["device_display_name"],
                     row["allow_existing_device"], device_id, token_id)

    def complete(self, claim: Claim, route_hash: str) -> bool:
        """Step 3, ONE control transaction: CLAIMED -> COMPLETED + the route +
        audit. False (nothing written) if the claim is no longer CLAIMED
        (reconciled meanwhile)."""
        with self.control.transaction() as cur:
            row = cur.execute("UPDATE device_enrollment_codes SET status = 'COMPLETED', completed_at = now(), "
                              "route_hash = %s WHERE enrollment_id = %s AND status = 'CLAIMED' "
                              "RETURNING enrollment_id", (route_hash, claim.enrollment_id)).fetchone()
            if row is None:
                return False
            cur.execute("INSERT INTO device_credential_routes (route_hash, organization_id, device_id) "
                        "VALUES (%s, %s, %s)", (route_hash, claim.organization_id, claim.device_id))
            self.control.write_audit(cur, "enrollment.complete", "enrollment", claim.enrollment_id,
                                     claim.organization_id, {"device_id": claim.device_id})
            self.control.write_audit(cur, "device_route.create", "device_route", claim.device_id,
                                     claim.organization_id)
        return True

    def fail(self, claim: Claim, reason: str) -> bool:
        with self.control.transaction() as cur:
            row = cur.execute("UPDATE device_enrollment_codes SET status = 'FAILED', failure_reason = %s "
                              "WHERE enrollment_id = %s AND status = 'CLAIMED' RETURNING enrollment_id",
                              (reason[:64], claim.enrollment_id)).fetchone()
            if row:
                self.control.write_audit(cur, "enrollment.fail", "enrollment", claim.enrollment_id,
                                         claim.organization_id, {"reason": reason[:64]})
        return row is not None

    def take_stale_claims(self, older_than_seconds: int) -> list[Claim]:
        """Reconciliation: atomically mark CLAIMED codes older than the
        cutoff FAILED and return them (a concurrent completion can't win
        afterwards: completion requires status CLAIMED)."""
        with self.control.transaction() as cur:
            rows = cur.execute(
                "UPDATE device_enrollment_codes SET status = 'FAILED', failure_reason = 'interrupted' "
                "WHERE status = 'CLAIMED' AND claimed_at < now() - make_interval(secs => %s) "
                "RETURNING enrollment_id, organization_id, employee_id, device_display_name, allow_existing_device, "
                "device_id, token_id", (older_than_seconds,)).fetchall()
            for r in rows:
                self.control.write_audit(cur, "enrollment.reconcile", "enrollment", r["enrollment_id"],
                                         r["organization_id"], {"device_id": r["device_id"]})
        return [Claim(r["enrollment_id"], r["organization_id"], r["employee_id"], r["device_display_name"],
                      r["allow_existing_device"], r["device_id"], r["token_id"]) for r in rows]

    def expire_codes(self) -> int:
        with self.control.connection() as conn:
            return conn.execute("UPDATE device_enrollment_codes SET status = 'EXPIRED' "
                                "WHERE status = 'ISSUED' AND expires_at <= now()").rowcount


# ─── the service ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileResult:
    expired_codes: int
    interrupted: int
    tokens_revoked: int
    tenant_unreachable: int


class EnrollmentService:
    """``counters`` provides ``count_attempt(bucket, key_hash, window)`` (the
    shared control-DB counters of saas_auth.PostgresAuthStore).
    ``fault`` is a test hook called with each saga step name."""

    def __init__(self, store, resolver: TenantResolver, manager: TenantRepositoryManager, counters,  # noqa: ANN001
                 settings: EnrollmentSettings | None = None, *,
                 fault: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.resolver = resolver
        self.manager = manager
        self.counters = counters
        self.settings = settings or EnrollmentSettings()
        self._fault = fault or (lambda step: None)

    def _rate_limit(self, code: object) -> None:
        s = self.settings
        g, g_wait = self.counters.count_attempt("enrollment_global", _GLOBAL_KEY, s.global_window_seconds)
        c, c_wait = self.counters.count_attempt("enrollment_code", _limit_key(code), s.code_window_seconds)
        waits = [w for count, limit, w in ((g, s.global_max_attempts, g_wait), (c, s.code_max_attempts, c_wait))
                 if count > limit]
        if waits:
            raise EnrollmentRateLimited(max(waits))

    def enroll(self, code: object, device_id: object, device_name: object = None) -> EnrolledDevice:
        self._rate_limit(code)
        normalized = normalize_code(code)
        if normalized is None:
            raise EnrollmentRefused(401, GENERIC_REFUSAL, "malformed_code")
        try:
            device_id = domain.identifier(device_id, "device id")
            client_name = domain.display_name(device_name, "device name") if device_name else None
        except TenancyError as exc:
            raise EnrollmentRefused(422, str(exc), "invalid_device") from None

        # 1. CLAIM (control)
        try:
            claim = self.store.claim(code_hash(normalized), device_id, uuid.uuid4())
        except Exception as exc:  # noqa: BLE001 — control DB unreachable: nothing was claimed
            logger.warning("enrollment claim failed (%s)", type(exc).__name__)
            raise RepositoryUnavailable("enrollment is temporarily unavailable") from None
        if claim is None:
            raise EnrollmentRefused(401, GENERIC_REFUSAL, "unknown_expired_or_used")
        self._fault("claimed")
        try:
            context = self.resolver.for_enrollment(claim.organization_id)
        except TenantAccessDenied:
            self._fail(claim, "tenant_unavailable")
            raise EnrollmentRefused(403, REFUSED, "tenant_unavailable") from None

        # 2. TENANT (device + token in the resolved tenant only)
        issued = None
        try:
            with self.manager.repository(context) as repo:
                employee = repo.get_employee(claim.employee_id)
                if employee is None or not employee.is_active:
                    self._fail(claim, "employee_unavailable")
                    raise EnrollmentRefused(403, REFUSED, "employee_unavailable")
                device = repo.get_device(device_id)
                if device is None:
                    repo.add_device(device_id, claim.employee_id,
                                    display_name=claim.device_display_name or client_name)
                elif not claim.allow_existing_device or device.employee_id != claim.employee_id:
                    self._fail(claim, "device_exists")
                    raise EnrollmentRefused(409, DEVICE_EXISTS, "device_exists")
                elif device.status != "ACTIVE":
                    self._fail(claim, "device_disabled")  # never silently re-enabled
                    raise EnrollmentRefused(403, REFUSED, "device_disabled")
                self._fault("device_ready")
                issued = issue_token(repo, device_id, token_id=str(claim.token_id))
                self._fault("token_issued")
                # 3. COMPLETE (control: state + route + audit in ONE transaction)
                try:
                    completed = self.store.complete(claim, routing_hash(issued.token))
                except Exception as exc:  # noqa: BLE001 — control failed: revoke the unrouted token now
                    self._compensate(repo, claim, issued.token_id, "route_failed")
                    logger.warning("enrollment completion failed (%s); token revoked", type(exc).__name__)
                    raise RepositoryUnavailable("enrollment could not be completed") from None
                if not completed:  # reconciled meanwhile: this credential must not survive
                    self._revoke(repo, issued.token_id)
                    raise EnrollmentRefused(401, GENERIC_REFUSAL, "reconciled")
                self._fault("completed")
        except RepositoryUnavailable:
            if issued is None:
                self._fail(claim, "tenant_unavailable")
            raise
        logger.info("device enrolled (enrollment %s)", claim.enrollment_id)
        return EnrolledDevice(device_id, claim.employee_id, issued.token)

    # ─── compensation / reconciliation ────────────────────────────────────
    def _fail(self, claim: Claim, reason: str) -> None:
        try:
            self.store.fail(claim, reason)
        except Exception as exc:  # noqa: BLE001 — stays CLAIMED: reconcile() finishes it
            logger.warning("could not mark enrollment %s failed (%s)", claim.enrollment_id, type(exc).__name__)

    @staticmethod
    def _revoke(repo, token_id: str) -> bool:  # noqa: ANN001
        try:
            return repo.revoke_token(str(token_id))
        except Exception as exc:  # noqa: BLE001 — the token has no route; reconcile() retries
            logger.warning("could not revoke an unrouted enrollment token (%s)", type(exc).__name__)
            return False

    def _compensate(self, repo, claim: Claim, token_id: str, reason: str) -> None:  # noqa: ANN001
        self._revoke(repo, token_id)
        self._fail(claim, reason)

    def reconcile(self) -> ReconcileResult:
        """Operator job: expire unused codes past their expiry, and finish
        interrupted enrollments (CLAIMED longer than the delay): mark FAILED
        and revoke the token they may have issued. Their tokens never had a
        route, so they were never usable."""
        expired = self.store.expire_codes()
        stale = self.store.take_stale_claims(self.settings.reconcile_after_seconds)
        revoked = unreachable = 0
        for claim in stale:
            try:
                context = self.resolver.for_job(claim.organization_id)
                with self.manager.repository(context) as repo:
                    revoked += int(self._revoke(repo, str(claim.token_id)))
            except (TenantAccessDenied, RepositoryUnavailable):
                unreachable += 1  # not READY/unreachable: the token is unroutable anyway; revoke later
        return ReconcileResult(expired, len(stale), revoked, unreachable)
