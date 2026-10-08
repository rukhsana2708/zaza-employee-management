"""Manager accounts and server-side sessions.

- Accounts: lower-case usernames (case-insensitive), Argon2id password
  hashes, role ADMIN or MANAGER, active flag. No default account exists;
  accounts are created with the ``manager-create`` CLI command.
- Login: the same generic failure for an unknown user, a wrong password and
  a disabled account; an unknown username still costs one Argon2 check.
- Sessions: an opaque random token in an HttpOnly cookie; the database
  stores only SHA-256(token) and SHA-256(CSRF token). A session ends at its
  fixed expiry, at logout, when revoked, or when the account is disabled —
  and is refused immediately afterwards.

Audited (``audit_logs``, entity ``manager_user``): account creation, login,
logout, password reset, disable/enable, session revocation. Page views are
not audited. Passwords, tokens and hashes never appear in audit rows or logs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import DashboardSettings
from .models import ManagerSession, ManagerUser, Role
from .queries import DashboardRepository
from .security import (
    burn_password_check,
    check_password_policy,
    csrf_for,
    hash_password,
    needs_rehash,
    new_token,
    normalize_username,
    same,
    token_hash,
    verify_password,
)

logger = logging.getLogger("zaza_server.dashboard")
UTC = timezone.utc
LOGIN_FAILED = "Invalid username or password."
TOUCH_INTERVAL = timedelta(minutes=5)  # last_seen_at bookkeeping, at most this often


@dataclass(frozen=True)
class SessionContext:
    """An authenticated request: the user, the session and its CSRF token."""

    user: ManagerUser
    session: ManagerSession
    csrf_token: str

    @property
    def actor(self) -> tuple[str, str]:
        return (self.user.role, self.user.username)

    def csrf_ok(self, submitted: str | None) -> bool:
        return bool(submitted) and same(submitted, self.csrf_token) and same(
            token_hash(self.csrf_token), self.session.csrf_token_hash)


class ManagerAuth:
    def __init__(self, repo: DashboardRepository, settings: DashboardSettings | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.repo = repo
        self.settings = settings or DashboardSettings()
        self.clock = clock

    # ── accounts (CLI) ────────────────────────────────────────────────────
    def create_user(self, username: str, display_name: str, password: str, *, role: str = "MANAGER",
                    actor: tuple[str, str] = ("CLI", "server-cli")) -> ManagerUser:
        name = normalize_username(username)
        role = Role(role.upper()).value
        display_name = (display_name or "").strip()
        if not 1 <= len(display_name) <= 200:
            raise ValueError("display name must be 1..200 characters")
        check_password_policy(password, username=name)
        user = self.repo.create_user(name, display_name, hash_password(password), role)
        self.repo.audit(actor, "manager.create", "manager_user", user.manager_user_id, None,
                        {"username": name, "display_name": display_name, "role": role})
        return user

    def _user(self, username: str) -> ManagerUser:
        user = self.repo.user_by_username(normalize_username(username))
        if user is None:
            raise ValueError("no such manager account")
        return user

    def reset_password(self, username: str, password: str, *, actor: tuple[str, str] = ("CLI", "server-cli")) -> int:
        """New password; every existing session of the account is revoked."""
        user = self._user(username)
        check_password_policy(password, username=user.username)
        self.repo.update_user(user.manager_user_id, password_hash=hash_password(password))
        revoked = self.repo.revoke_user_sessions(user.manager_user_id, self.clock())
        self.repo.audit(actor, "manager.password_reset", "manager_user", user.manager_user_id, None,
                        {"sessions_revoked": revoked})
        return revoked

    def set_active(self, username: str, active: bool, *, actor: tuple[str, str] = ("CLI", "server-cli")) -> int:
        user = self._user(username)
        self.repo.update_user(user.manager_user_id, is_active=active)
        revoked = 0 if active else self.repo.revoke_user_sessions(user.manager_user_id, self.clock())
        self.repo.audit(actor, "manager.enable" if active else "manager.disable", "manager_user",
                        user.manager_user_id, {"is_active": user.is_active}, {"is_active": active,
                                                                              "sessions_revoked": revoked})
        return revoked

    def revoke_sessions(self, username: str, *, actor: tuple[str, str] = ("CLI", "server-cli")) -> int:
        user = self._user(username)
        revoked = self.repo.revoke_user_sessions(user.manager_user_id, self.clock())
        self.repo.audit(actor, "manager.revoke_sessions", "manager_user", user.manager_user_id, None,
                        {"sessions_revoked": revoked})
        return revoked

    def users(self) -> list[ManagerUser]:
        return self.repo.users()

    # ── login / sessions ──────────────────────────────────────────────────
    def login(self, username: str, password: str) -> tuple[str, SessionContext] | None:
        """``(session token, context)`` or ``None``. Every failure looks the
        same to the caller (see :data:`LOGIN_FAILED`)."""
        try:
            name = normalize_username(username)
        except ValueError:
            burn_password_check(password or "")
            return None
        user = self.repo.user_by_username(name)
        if user is None:
            burn_password_check(password or "")
            return None
        if not verify_password(user.password_hash, password or "") or not user.is_active:
            return None
        now = self.clock()
        if needs_rehash(user.password_hash):
            self.repo.update_user(user.manager_user_id, password_hash=hash_password(password))
        token = new_token()
        csrf = csrf_for(token)
        session = ManagerSession(str(uuid.uuid4()), user.manager_user_id, token_hash(token), token_hash(csrf), now,
                                 now + timedelta(hours=self.settings.session_hours), now, None)
        self.repo.create_session(session)
        self.repo.update_user(user.manager_user_id, last_login_at=now)
        self.repo.audit((user.role, user.username), "manager.login", "manager_user", user.manager_user_id, None,
                        {"session_expires_at": session.expires_at.isoformat()})
        return token, SessionContext(user, session, csrf)

    def validate(self, token: str | None) -> SessionContext | None:
        if not token or len(token) > 200:
            return None
        found = self.repo.session_by_hash(token_hash(token))
        if found is None:
            return None
        session, user = found
        now = self.clock()
        if session.revoked_at is not None or session.expires_at <= now or not user.is_active:
            return None
        if now - session.last_seen_at >= TOUCH_INTERVAL:
            self.repo.touch_session(session.session_id, now)
        return SessionContext(user, session, csrf_for(token))

    def logout(self, ctx: SessionContext) -> None:
        self.repo.revoke_session(ctx.session.session_id, self.clock())
        self.repo.audit(ctx.actor, "manager.logout", "manager_user", ctx.user.manager_user_id)
