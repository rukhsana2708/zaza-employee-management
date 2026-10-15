"""Server logging: bounded log files and secret redaction (Phase 10).

``serve`` logs to stderr and, when ``ZAZA_LOG_FILE`` is set, to a
size-rotated file (``logging.handlers.RotatingFileHandler``): at most
``ZAZA_LOG_MAX_BYTES`` × (1 + ``ZAZA_LOG_BACKUP_COUNT``) bytes on disk.

Every line passes through :class:`RedactingFormatter`, after formatting and
including tracebacks, which masks bearer tokens, Authorization and Cookie
values, ``password=``/``token=``-style fields, credentials in database
URLs, PEM private keys and Google access tokens, plus any literal secret
registered at start-up (the database password). The application already
avoids logging these; this is the safety net.

Environment:

``ZAZA_LOG_FILE``           path of the log file (unset: stderr only)
``ZAZA_LOG_LEVEL``          DEBUG, INFO (default), WARNING or ERROR
``ZAZA_LOG_MAX_BYTES``      rotate at this size, 1 MiB..100 MiB (default 10 MiB)
``ZAZA_LOG_BACKUP_COUNT``   rotated files kept, 1..50 (default 5)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError

MiB = 1024 * 1024
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MASK = "***"

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL), MASK),
    (re.compile(r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)[^\r\n]+"), rf"\1\2{MASK}"),
    (re.compile(r"(?i)\b(cookie|set-cookie)(\s*[:=]\s*)[^\r\n]+"), rf"\1\2{MASK}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"), f"Bearer {MASK}"),
    (re.compile(r"(?i)\b(zaza_manager_(?:session|login))=[^;\s]+"), rf"\1={MASK}"),
    (re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s@]+:)[^@\s]+@"), rf"\1{MASK}@"),
    (re.compile(r"""(?i)(["']?\b[\w-]*(?:password|passwd|pwd|secret|token|private_key|api_key)["']?)(\s*[:=]\s*["']?)"""
                r"""[^\s"',;&}]+"""), rf"\1\2{MASK}"),
    (re.compile(r"ya29\.[\w.\-]+"), MASK),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str = FORMAT, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


@dataclass(frozen=True)
class LogSettings:
    file: Path | None = None
    level: str = "INFO"
    max_bytes: int = 10 * MiB
    backup_count: int = 5

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ConfigError(f"ZAZA_LOG_LEVEL must be one of {', '.join(LEVELS)}")
        if not MiB <= self.max_bytes <= 100 * MiB:
            raise ConfigError("ZAZA_LOG_MAX_BYTES must be 1048576..104857600 (1..100 MiB)")
        if not 1 <= self.backup_count <= 50:
            raise ConfigError("ZAZA_LOG_BACKUP_COUNT must be 1..50")

    @property
    def max_disk_bytes(self) -> int | None:
        return self.max_bytes * (1 + self.backup_count) if self.file else None


def _int(env: dict, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None


def log_settings_from_env(env: dict | None = None) -> LogSettings:
    env = dict(os.environ if env is None else env)
    file = (env.get("ZAZA_LOG_FILE") or "").strip()
    return LogSettings(
        file=Path(file) if file else None,
        level=(env.get("ZAZA_LOG_LEVEL") or "INFO").strip().upper(),
        max_bytes=_int(env, "ZAZA_LOG_MAX_BYTES", 10 * MiB),
        backup_count=_int(env, "ZAZA_LOG_BACKUP_COUNT", 5),
    )


def configure_logging(settings: LogSettings | None = None, *, secrets: Iterable[str] = ()) -> LogSettings:
    """Install redacting stderr (+ rotating file) handlers on the root
    logger, replacing ones installed by an earlier call (handlers added by
    others are left alone). uvicorn's loggers propagate here
    (``log_config=None``)."""
    settings = settings or log_settings_from_env()
    formatter = RedactingFormatter(secrets=secrets)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.file is not None:
        settings.file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            settings.file, maxBytes=settings.max_bytes, backupCount=settings.backup_count, encoding="utf-8"))
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "zaza_handler", False)]:
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.zaza_handler = True
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    return settings
