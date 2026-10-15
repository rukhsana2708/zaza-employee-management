"""Generate the ZaZa PostgreSQL role passwords without ever showing them (Phase 10).

For each role this writes two files into the secrets directory (which only
Administrators and SYSTEM can read):

- ``db-<role>.password``  the password, used by the ZaZa programs to log in
- ``db-<role>.scram``     its SCRAM-SHA-256 verifier, which the database
                          setup script gives to ``CREATE ROLE ... PASSWORD``

so the plaintext password never appears in SQL text, a command line, the
PostgreSQL log or the terminal. Existing files are never overwritten. The
``.scram`` files are only needed for role creation and are deleted after it.

Standard library only; run with the server's Python 3.14:

    "C:\\Program Files\\Python314\\python.exe" -I zaza_db_credentials.py --secrets-dir <dir> owner app report
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import string
import sys
from pathlib import Path

ROLES = ("owner", "app", "report")
ALPHABET = string.ascii_letters + string.digits  # ASCII only: SASLprep leaves it unchanged
PASSWORD_LENGTH = 40  # ~238 bits
ITERATIONS = 4096     # PostgreSQL's default scram_iterations


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def scram_keys(password: str, salt: bytes, iterations: int = ITERATIONS) -> tuple[bytes, bytes]:
    """``(StoredKey, ServerKey)`` per RFC 5802 / RFC 7677 (SCRAM-SHA-256)."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return stored_key, server_key


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    """The string PostgreSQL stores in ``pg_authid.rolpassword``; accepted
    as-is by ``CREATE ROLE ... PASSWORD``."""
    if any(c not in ALPHABET for c in password):
        raise ValueError("only ASCII letters and digits are supported (no SASLprep needed)")
    salt = salt if salt is not None else os.urandom(16)
    stored_key, server_key = scram_keys(password, salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` exclusively (fails if it exists); no trailing newline."""
    with open(path, "x", encoding="ascii", newline="") as handle:
        handle.write(text)


def create(secrets_dir: Path, roles: list[str]) -> list[str]:
    created = []
    for role in roles:
        password_file = secrets_dir / f"db-{role}.password"
        verifier_file = secrets_dir / f"db-{role}.scram"
        if password_file.exists() or verifier_file.exists():
            raise FileExistsError(f"{password_file.name} or {verifier_file.name} already exists; not overwritten")
    for role in roles:
        password = generate_password()
        _write_new(secrets_dir / f"db-{role}.password", password)
        _write_new(secrets_dir / f"db-{role}.scram", scram_verifier(password))
        created.append(role)
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("roles", nargs="+", choices=ROLES)
    args = parser.parse_args(argv)
    if not args.secrets_dir.is_dir():
        print(f"not a directory: {args.secrets_dir}", file=sys.stderr)
        return 2
    try:
        created = create(args.secrets_dir, args.roles)
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Created password and verifier files for: {', '.join(created)} (contents not shown).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
