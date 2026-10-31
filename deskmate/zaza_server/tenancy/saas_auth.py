"""SaaS authentication (Phase 10F-B): control-plane users, Argon2id
passwords, server-side sessions and a SHARED login rate limiter.

- **Identity:** the control-plane ``users`` row (one identity system). A SaaS
  user is never a monitored employee, and nothing here reads or writes
  tenant databases.
- **Passwords:** Argon2id PHC hashes only (``user_password_credentials``),
  via the maintained ``argon2-cffi`` helpers in ``dashboard/security.py``.
  No plaintext, no reversible storage, no hints. An unknown or invalid email
  still costs one Argon2 verification (no timing shortcut).
- **Sessions:** a 256-bit random token in a Secure, HttpOnly,
  SameSite=Strict cookie. The control database stores SHA-256(token) only.
  A session is refused once it has expired, been revoked, or its user is not
  ACTIVE. A password change revokes every session of the user.
- **CSRF:** HMAC(session token) per session (``csrf_for``), plus the
  pre-session double-submit login token and
  ``http_security.same_origin`` in the routes.
- **Rate limiting:** :class:`SharedLoginRateLimiter` counts attempts in
  ``login_attempt_counters`` (one atomic upsert per bucket). It is shared
  by every Cloud Run instance and keyed by a digest of the login identity
  plus one global bucket, never by a client IP (which Cloud Run mode
  doesn't trust).
- **Authorization:** a session identifies a USER. It authorizes no
  organization by itself: every organization request goes through
  ``TenantResolver.for_member`` (ACTIVE user, ACTIVE membership, ACTIVE
  organization, READY tenant). Permissions come from :data:`ROLE_PERMISSIONS`.

SQL lives in :class:`PostgresAuthStore`; routes call :class:`SaasAuthService`.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

from ..config import ConfigError
from ..dashboard.security import (
    burn_password_check,
    check_password_policy,
    csrf_for,
    hash_password,
    needs_rehash,
    new_token,
    same,
    token_hash,
    verify_password,
)
from . import domain
from .context import ContextKind, TenantContext
from .control_repository import User
from .domain import MembershipRole, TenancyError, UserStatus

UTC = timezone.utc
SESSION_COOKIE = "zaza_session"
LOGIN_COOKIE = "zaza_login"  # pre-session double-submit token
SESSION_PATH = "/app"
LOGIN_PATH = "/app/login"
TOUCH_INTERVAL = timedelta(minutes=5)
_IDENTITY_DOMAIN = b"zaza-login-identity-v1\x00"
_GLOBAL_KEY = hashlib.sha256(b"zaza-login-global-v1").hexdigest()


class LoginRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many sign-in attempts")
        self.retry_after = max(1, int(retry_after))


# ─── settings ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SaasAuthSettings:
    session_hours: int = 12
    identity_max_attempts: int = 10   # per login identity per window
    identity_window_seconds: int = 900
    global_max_attempts: int = 300    # all identities together per global window
    global_window_seconds: int = 60
    cookie_secure: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.session_hours <= 168:
            raise ConfigError("ZAZA_SAAS_SESSION_HOURS must be 1..168")
        if not 3 <= self.identity_max_attempts <= 100:
            raise ConfigError("ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_PER_IDENTITY must be 3..100")
        if not 60 <= self.identity_window_seconds <= 86400:
            raise ConfigError("ZAZA_SAAS_LOGIN_WINDOW_SECONDS must be 60..86400")
        if not 10 <= self.global_max_attempts <= 100000:
            raise ConfigError("ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_GLOBAL must be 10..100000")
        if not 10 <= self.global_window_seconds <= 3600:
            raise ConfigError("the global login window must be 10..3600 seconds")


def saas_auth_settings_from_env(env: dict | None = None) -> SaasAuthSettings:
    env = dict(os.environ if env is None else env)

    def num(name: str, default: int) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer") from None

    secure = (env.get("ZAZA_SAAS_COOKIE_SECURE") or "true").strip().lower()
    if secure not in ("true", "false"):
        raise ConfigError("ZAZA_SAAS_COOKIE_SECURE must be true or false")
    return SaasAuthSettings(
        session_hours=num("ZAZA_SAAS_SESSION_HOURS", 12),
        identity_max_attempts=num("ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_PER_IDENTITY", 10),
        identity_window_seconds=num("ZAZA_SAAS_LOGIN_WINDOW_SECONDS", 900),
        global_max_attempts=num("ZAZA_SAAS_LOGIN_MAX_ATTEMPTS_GLOBAL", 300),
        cookie_secure=secure == "true",
    )


# ─── values ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SaasSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class SaasSessionContext:
    """An authenticated SaaS request: the USER (not an organization)."""

    user: User
    session: SaasSession
    csrf_token: str

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.user_id

    def csrf_ok(self, submitted: str | None) -> bool:
        return bool(submitted) and same(submitted, self.csrf_token)


def login_identity_key(email: str) -> str:
    """Digest of the (normalized, if valid) login identity: the rate-limit
    key. The email itself is never stored in the counter table."""
    text = (email or "").strip().lower()[:254]
    return hashlib.sha256(_IDENTITY_DOMAIN + text.encode("utf-8")).hexdigest()


# ─── storage boundary ──────────────────────────────────────────────────────


class AuthStore(Protocol):
    def find_user_by_email(self, email: str) -> User | None: ...
    def get_user(self, user_id: uuid.UUID) -> User | None: ...
    def get_password_hash(self, user_id: uuid.UUID) -> str | None: ...
    def set_password_hash(self, user_id: uuid.UUID, password_hash: str, now: datetime) -> tuple[int, int]: ...
    def rehash_password(self, user_id: uuid.UUID, password_hash: str) -> None: ...
    def create_session(self, session: SaasSession, token_digest: str) -> None: ...
    def session_by_hash(self, token_digest: str) -> tuple[SaasSession, User] | None: ...
    def touch_session(self, session_id: uuid.UUID, now: datetime) -> None: ...
    def revoke_session(self, session_id: uuid.UUID, now: datetime, reason: str) -> None: ...
    def revoke_user_sessions(self, user_id: uuid.UUID, now: datetime, reason: str, *, audit: bool) -> int: ...
    def count_attempt(self, bucket: str, key_hash: str, window_seconds: int) -> tuple[int, int]: ...
    def clear_attempts(self, bucket: str, key_hash: str) -> None: ...
    def cleanup(self, session_retention: timedelta, counter_retention: timedelta) -> tuple[int, int]: ...


class PostgresAuthStore:
    """:class:`AuthStore` on the control database (c0002 tables), through the
    control repository's pool, transactions and audit writer."""

    def __init__(self, control) -> None:  # noqa: ANN001 — PostgresControlRepository
        self.control = control

    def find_user_by_email(self, email: str) -> User | None:
        return self.control.find_user_by_email(email)

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self.control.get_user(user_id)

    def get_password_hash(self, user_id: uuid.UUID) -> str | None:
        with self.control.connection() as conn:
            row = conn.execute("SELECT password_hash FROM user_password_credentials WHERE user_id = %s",
                               (domain.user_id(user_id),)).fetchone()
        return row["password_hash"] if row else None

    def set_password_hash(self, user_id: uuid.UUID, password_hash: str, now: datetime) -> tuple[int, int]:
        """Create or replace the credential, revoke every session of the user,
        audit — one transaction. Returns (credential version, sessions revoked)."""
        uid = domain.user_id(user_id)
        with self.control.transaction() as cur:
            row = cur.execute(
                "INSERT INTO user_password_credentials (user_id, password_hash) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET password_hash = EXCLUDED.password_hash, "
                "credential_version = user_password_credentials.credential_version + 1, password_changed_at = now() "
                "RETURNING credential_version", (uid, password_hash)).fetchone()
            revoked = cur.execute(
                "UPDATE user_sessions SET revoked_at = %s, revoke_reason = 'password_change' "
                "WHERE user_id = %s AND revoked_at IS NULL", (now, uid)).rowcount
            self.control.write_audit(cur, "user.password_set", "user", uid, None,
                                     {"credential_version": row["credential_version"], "sessions_revoked": revoked})
        return row["credential_version"], revoked

    def rehash_password(self, user_id: uuid.UUID, password_hash: str) -> None:
        """Parameter upgrade of the SAME password (library defaults changed):
        no version bump, no session revocation, no audit."""
        with self.control.transaction() as cur:
            cur.execute("UPDATE user_password_credentials SET password_hash = %s WHERE user_id = %s",
                        (password_hash, domain.user_id(user_id)))

    @staticmethod
    def _session(row: dict) -> SaasSession:
        return SaasSession(row["session_id"], row["user_id"], row["created_at"], row["expires_at"],
                           row["last_seen_at"], row["revoked_at"])

    def create_session(self, session: SaasSession, token_digest: str) -> None:
        with self.control.transaction() as cur:
            cur.execute("INSERT INTO user_sessions (session_id, user_id, token_hash, created_at, expires_at, "
                        "last_seen_at) VALUES (%s, %s, %s, %s, %s, %s)",
                        (session.session_id, session.user_id, token_digest, session.created_at, session.expires_at,
                         session.last_seen_at))

    def session_by_hash(self, token_digest: str) -> tuple[SaasSession, User] | None:
        with self.control.connection() as conn:
            row = conn.execute("SELECT * FROM user_sessions WHERE token_hash = %s", (token_digest,)).fetchone()
        if row is None:
            return None
        user = self.control.get_user(row["user_id"])
        return (self._session(row), user) if user else None

    def touch_session(self, session_id: uuid.UUID, now: datetime) -> None:
        with self.control.connection() as conn:
            conn.execute("UPDATE user_sessions SET last_seen_at = %s WHERE session_id = %s AND revoked_at IS NULL",
                         (now, session_id))

    def revoke_session(self, session_id: uuid.UUID, now: datetime, reason: str) -> None:
        with self.control.connection() as conn:
            conn.execute("UPDATE user_sessions SET revoked_at = %s, revoke_reason = %s "
                         "WHERE session_id = %s AND revoked_at IS NULL", (now, reason, session_id))

    def revoke_user_sessions(self, user_id: uuid.UUID, now: datetime, reason: str, *, audit: bool) -> int:
        uid = domain.user_id(user_id)
        with self.control.transaction() as cur:
            revoked = cur.execute("UPDATE user_sessions SET revoked_at = %s, revoke_reason = %s "
                                  "WHERE user_id = %s AND revoked_at IS NULL", (now, reason, uid)).rowcount
            if audit:
                self.control.write_audit(cur, "user.sessions_revoked", "user", uid, None,
                                         {"reason": reason, "sessions_revoked": revoked})
        return revoked

    def cleanup_expired_counters(self, bucket: str = "sync_device", max_age_seconds: int = 3600,
                                 limit: int = 500) -> int:
        """Delete a bounded batch of long-expired counter rows of ONE bucket (database time). Called opportunistically
        by the sync rate limiter, so no scheduler or extra service is needed; never touches another bucket."""
        with self.control.connection() as conn:
            return conn.execute(
                "DELETE FROM login_attempt_counters WHERE ctid IN (SELECT ctid FROM login_attempt_counters "
                "WHERE bucket = %s AND window_start < now() - make_interval(secs => %s) LIMIT %s)",
                (bucket, max_age_seconds, limit)).rowcount

    def count_attempt(self, bucket: str, key_hash: str, window_seconds: int) -> tuple[int, int]:
        """Atomically count one attempt in the CURRENT window (database time,
        so all instances agree). Returns (attempts so far, seconds left)."""
        with self.control.connection() as conn:
            row = conn.execute(
                "WITH w AS (SELECT to_timestamp(floor(extract(epoch FROM clock_timestamp()) / %(win)s) * %(win)s) "
                "AS ws) "
                "INSERT INTO login_attempt_counters (bucket, key_hash, window_start, attempts) "
                "SELECT %(bucket)s, %(key)s, ws, 1 FROM w "
                "ON CONFLICT (bucket, key_hash, window_start) DO UPDATE "
                "SET attempts = login_attempt_counters.attempts + 1 "
                "RETURNING attempts, GREATEST(1, ceil(extract(epoch FROM window_start + make_interval(secs => "
                "%(win)s) - clock_timestamp())))::int AS retry_after",
                {"win": window_seconds, "bucket": bucket, "key": key_hash}).fetchone()
        return row["attempts"], row["retry_after"]

    def clear_attempts(self, bucket: str, key_hash: str) -> None:
        with self.control.connection() as conn:
            conn.execute("DELETE FROM login_attempt_counters WHERE bucket = %s AND key_hash = %s", (bucket, key_hash))

    def cleanup(self, session_retention: timedelta, counter_retention: timedelta) -> tuple[int, int]:
        """Delete sessions that ended (expired or revoked) longer ago than the
        retention, and counter windows older than theirs. Bounded storage."""
        with self.control.connection() as conn:
            sessions = conn.execute(
                "DELETE FROM user_sessions WHERE COALESCE(revoked_at, expires_at) < clock_timestamp() - %s",
                (session_retention,)).rowcount
            counters = conn.execute("DELETE FROM login_attempt_counters WHERE window_start < clock_timestamp() - %s",
                                    (counter_retention,)).rowcount
        return sessions, counters


# ─── shared login rate limiter ─────────────────────────────────────────────


class SharedLoginRateLimiter:
    """Shared across instances (state in the control database). Each attempt
    is counted atomically BEFORE the password check, in two buckets: the
    login identity (normalized email digest) and one global bucket against
    bursts. Over either limit: refused, with the seconds until its window
    ends. A successful sign-in clears the identity bucket. No client address
    is used (unverifiable on Cloud Run)."""

    def __init__(self, store: AuthStore, settings: SaasAuthSettings) -> None:
        self.store = store
        self.settings = settings

    def acquire(self, identity_key: str) -> int | None:
        """Seconds to wait (refused) or None (allowed)."""
        s = self.settings
        g_count, g_wait = self.store.count_attempt("global", _GLOBAL_KEY, s.global_window_seconds)
        i_count, i_wait = self.store.count_attempt("identity", identity_key, s.identity_window_seconds)
        waits = [w for count, limit, w in ((g_count, s.global_max_attempts, g_wait),
                                            (i_count, s.identity_max_attempts, i_wait)) if count > limit]
        return max(waits) if waits else None

    def success(self, identity_key: str) -> None:
        self.store.clear_attempts("identity", identity_key)


# ─── the service ───────────────────────────────────────────────────────────


class SaasAuthService:
    def __init__(self, store: AuthStore, settings: SaasAuthSettings | None = None, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.settings = settings or SaasAuthSettings()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.limiter = SharedLoginRateLimiter(store, self.settings)

    # ─── sign-in ──────────────────────────────────────────────────────────
    def login(self, email: str, password: str) -> tuple[str, SaasSessionContext] | None:
        """``(session token, context)``, or None for EVERY failure (unknown
        email, wrong password, no credential, disabled user). Raises
        :class:`LoginRateLimited` before any password check when over a
        limit. Unknown identities still cost one Argon2 verification."""
        password = password if isinstance(password, str) else ""
        key = login_identity_key(email if isinstance(email, str) else "")
        wait = self.limiter.acquire(key)
        if wait is not None:
            raise LoginRateLimited(wait)
        try:
            user = self.store.find_user_by_email(email)
        except TenancyError:
            user = None
        stored = self.store.get_password_hash(user.user_id) if user is not None else None
        if stored is None:
            burn_password_check(password)
            return None
        if not verify_password(stored, password) or user.status is not UserStatus.ACTIVE:
            return None
        self.limiter.success(key)
        if needs_rehash(stored):
            self.store.rehash_password(user.user_id, hash_password(password))
        now = self.clock()
        token = new_token()
        session = SaasSession(uuid.uuid4(), user.user_id, now, now + timedelta(hours=self.settings.session_hours), now)
        self.store.create_session(session, token_hash(token))
        return token, SaasSessionContext(user, session, csrf_for(token))

    def validate_session(self, token: str | None) -> SaasSessionContext | None:
        if not token or len(token) > 200:
            return None
        found = self.store.session_by_hash(token_hash(token))
        if found is None:
            return None
        session, user = found
        now = self.clock()
        if session.revoked_at is not None or session.expires_at <= now or user.status is not UserStatus.ACTIVE:
            return None
        if now - session.last_seen_at >= TOUCH_INTERVAL:
            self.store.touch_session(session.session_id, now)
        return SaasSessionContext(user, session, csrf_for(token))

    def logout(self, ctx: SaasSessionContext) -> None:
        self.store.revoke_session(ctx.session.session_id, self.clock(), "logout")

    # ─── trusted administrative path (operator CLI) ───────────────────────
    def set_password(self, user_id: uuid.UUID, password: str) -> tuple[int, int]:
        user = self.store.get_user(user_id)
        if user is None:
            raise TenancyError("unknown user")
        check_password_policy(password, username=user.email.split("@")[0])
        return self.store.set_password_hash(user.user_id, hash_password(password), self.clock())

    def revoke_user_sessions(self, user_id: uuid.UUID, *, reason: str = "admin") -> int:
        return self.store.revoke_user_sessions(domain.user_id(user_id), self.clock(), reason, audit=True)

    def cleanup(self, *, session_retention_days: int = 30) -> tuple[int, int]:
        counter_retention = timedelta(seconds=2 * max(self.settings.identity_window_seconds,
                                                      self.settings.global_window_seconds))
        return self.store.cleanup(timedelta(days=session_retention_days), counter_retention)


# ─── membership authorization ──────────────────────────────────────────────


class Permission(str, Enum):
    ORG_VIEW = "org.view"                          # see the organization exists, its name, own role
    EMPLOYEES_VIEW_ALL = "employees.view_all"      # every employee of the organization
    EMPLOYEES_VIEW_SCOPED = "employees.view_scoped"  # only assigned teams (team scopes: future phase)
    DEVICES_MANAGE = "devices.manage"              # enroll / disable devices (future phase)
    MEMBERS_MANAGE = "members.manage"              # invite / change SaaS members (future phase)
    ORG_SETTINGS_MANAGE = "org.settings.manage"
    EMPLOYEES_VIEW_SELF = "employees.view_self"
    EMPLOYEES_MANAGE = "employees.manage"


# Explicit sets, not a numeric hierarchy. MANAGER deliberately does NOT get
# company-wide employee access: it gets the scoped permission, which grants
# direct-report access only through workforce subject-scope validation.
ROLE_PERMISSIONS: dict[MembershipRole, frozenset[Permission]] = {
    MembershipRole.OWNER: frozenset({Permission.ORG_VIEW, Permission.EMPLOYEES_VIEW_ALL,
        Permission.EMPLOYEES_VIEW_SCOPED, Permission.DEVICES_MANAGE, Permission.MEMBERS_MANAGE,
        Permission.ORG_SETTINGS_MANAGE, Permission.EMPLOYEES_VIEW_SELF, Permission.EMPLOYEES_MANAGE}),
    MembershipRole.ADMIN: frozenset({Permission.ORG_VIEW, Permission.EMPLOYEES_VIEW_ALL, Permission.DEVICES_MANAGE,
                                     Permission.MEMBERS_MANAGE, Permission.EMPLOYEES_VIEW_SELF,
                                     Permission.EMPLOYEES_MANAGE}),
    MembershipRole.MANAGER: frozenset({Permission.ORG_VIEW, Permission.EMPLOYEES_VIEW_SCOPED,
                                      Permission.EMPLOYEES_VIEW_SELF}),
    MembershipRole.EMPLOYEE: frozenset({Permission.ORG_VIEW, Permission.EMPLOYEES_VIEW_SELF}),
    MembershipRole.VIEWER: frozenset({Permission.ORG_VIEW}),
}


class PermissionDenied(Exception):
    pass


def permissions(context: TenantContext) -> frozenset[Permission]:
    """Permissions of a MEMBER context (a resolved membership). Device and
    job contexts carry no SaaS permissions."""
    if not isinstance(context, TenantContext) or context.kind is not ContextKind.MEMBER or context.role is None:
        return frozenset()
    return ROLE_PERMISSIONS[context.role]


def require(context: TenantContext, permission: Permission) -> None:
    if permission not in permissions(context):
        raise PermissionDenied(permission.value)
