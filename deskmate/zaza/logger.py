"""Tiny standalone console logger for the ZaZa agent.

Deliberately independent of ``deskmate.logger`` (which writes into the
upstream ``~/.deskmate`` data directory) so the ZaZa module has zero
runtime coupling to DeskMate's data layout, per the "clearly separated
path/module" requirement. Phase 1 only needs console output for manual
verification.
"""

from __future__ import annotations

import logging

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("zaza")
    root.setLevel(logging.INFO)
    import sys  # noqa: PLC0415

    if not root.handlers and sys.stderr is not None:  # a windowed (no-console) build has no stderr
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"zaza.{name}")
