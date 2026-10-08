"""Privacy-safe, size-bounded file logging for the installed agent.

- ``<data dir>/logs/agent.log``, rotated at 1 MB, 5 old files kept (≈ 6 MB).
- :class:`PrivacyLogFilter` drops the developer-console lines that would put
  window titles, application names or per-tick activity into a second,
  uncontrolled copy (the activity database already holds the approved
  metadata), and masks anything that looks like a bearer token.
- Tokens are never passed to a logger anywhere; the mask is defence in depth.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 1_000_000
BACKUPS = 5
_DROP_PREFIXES = ("APP_CHANGE", "ACTIVITY ", "IDLE ", "CONTINUE")
_TOKEN_RE = re.compile(r"(?i)(bearer\s+|zzd_)[A-Za-z0-9._~+/=-]+")


class PrivacyLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        if msg.startswith(_DROP_PREFIXES) or "title=" in msg or msg == "%s":
            return False  # window titles / per-tick activity stay out of logs
        rendered = record.getMessage()
        if _TOKEN_RE.search(rendered):
            record.msg, record.args = _TOKEN_RE.sub(r"\1***", rendered), ()
        return True


def configure(log_dir: Path, *, debug: bool = False, console: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "agent.log"
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    handler.addFilter(PrivacyLogFilter())
    root = logging.getLogger("zaza")
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.addFilter(PrivacyLogFilter())
        root.addHandler(stream)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False
    from . import logger as zaza_logger  # noqa: PLC0415

    zaza_logger._CONFIGURED = True  # keep the console logger from adding a stderr handler
    return path
