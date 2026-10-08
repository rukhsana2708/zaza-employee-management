"""Passwords, tokens, usernames and HTTP security headers.

- Passwords: Argon2id via ``argon2-cffi`` (its maintained defaults). Only the
  PHC hash string is stored; the database refuses anything else.
- Session tokens: 32 random bytes from :mod:`secrets`, URL-safe. The browser
  gets the token; the database gets SHA-256(token) only.
- CSRF tokens: HMAC-SHA256(session token, "zaza-csrf"). Tied to the session,
  computable on every request from the cookie, never stored in plaintext
  (the session row keeps SHA-256 of it), and useless without the session.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

COOKIE_NAME = "zaza_manager_session"
LOGIN_COOKIE_NAME = "zaza_manager_login"  # double-submit token for the login form (pre-session CSRF)
COOKIE_PATH = "/manager"

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
MIN_PASSWORD = 12
MAX_PASSWORD = 256

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
    "Cross-Origin-Opener-Policy": "same-origin",
}


class PasswordPolicyError(ValueError):
    pass


def _hasher():  # noqa: ANN202
    from argon2 import PasswordHasher  # noqa: PLC0415

    return PasswordHasher()  # Argon2id, library defaults (RFC 9106 low-memory profile)


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    from argon2.exceptions import (  # noqa: PLC0415
        InvalidHashError,
        VerificationError,
        VerifyMismatchError,
    )

    try:
        return _hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher().check_needs_rehash(password_hash)


# A real Argon2id hash of a random secret: verifying against it when a
# username doesn't exist keeps the response time the same as a wrong password.
_DUMMY_HASH: str | None = None


def burn_password_check(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(_DUMMY_HASH, password)


def check_password_policy(password: str, *, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD} characters")
    if password.strip() != password:
        raise PasswordPolicyError("password must not start or end with spaces")
    if username and username.lower() in password.lower():
        raise PasswordPolicyError("password must not contain the username")
    if len(set(password)) < 6:
        raise PasswordPolicyError("password is too repetitive")


def normalize_username(username: str) -> str:
    """Usernames are case-insensitive: stored and compared lower-case."""
    name = (username or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError("username must be 3-64 characters: letters, digits, '.', '_' or '-', "
                         "starting with a letter or digit")
    return name


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_for(session_token: str) -> str:
    return hmac.new(session_token.encode("utf-8"), b"zaza-csrf", hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
