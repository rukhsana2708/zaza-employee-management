"""Control-plane schema migrations (Phase 10C), run only by an explicit
operator command: ``python -m deskmate.zaza_server control-migrate``.

Separate from the tenant migrations in ``postgres/migrate.py``: its own
script directory, revision namespace (``c0001_...``) and version table
(``zaza_control_version``). Nothing runs these at web-server start-up.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("DISABLE_SQLALCHEMY_CEXT_RUNTIME", "1")  # same reason as postgres/migrate.py

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from ...config import DatabaseSettings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
VERSION_TABLE = "zaza_control_version"


@dataclass(frozen=True)
class SchemaStatus:
    current: str | None
    head: str

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


def runtime_compatible(status: SchemaStatus, *, sync_limit_mode: str) -> bool:
    """Accept only the exact head or a configuration allowed by THAT head's declaration.

    No ordering, ancestry inference or transitive compatibility. Operators and
    tenant migrations keep using SchemaStatus.up_to_date (exact equality).
    """
    from ...ratelimit import SyncLimitSettings  # noqa: PLC0415

    SyncLimitSettings(mode=sync_limit_mode)  # invalid modes must never become a bypass
    if status.up_to_date:
        return True
    scripts = ScriptDirectory.from_config(alembic_config())
    if status.head != scripts.get_current_head():
        return False
    declaration = getattr(scripts.get_revision(status.head).module, "compatible_previous_revisions", {})
    return sync_limit_mode in declaration.get(status.current, ())


def alembic_config(settings: DatabaseSettings | None = None, *, connection=None, output=None) -> Config:  # noqa: ANN001
    cfg = Config(output_buffer=output) if output is not None else Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["zaza_settings"] = settings
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def current_revision(conn) -> str | None:  # noqa: ANN001 — a psycopg connection
    from psycopg.rows import tuple_row  # noqa: PLC0415

    cur = conn.cursor(row_factory=tuple_row)
    if cur.execute("SELECT to_regclass(%s)::text", (VERSION_TABLE,)).fetchone()[0] is None:
        return None
    row = cur.execute(f"SELECT version_num FROM {VERSION_TABLE}").fetchone()
    return row[0] if row else None


def schema_status(conn) -> SchemaStatus:  # noqa: ANN001
    return SchemaStatus(current=current_revision(conn), head=head_revision())


def upgrade(settings: DatabaseSettings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)


def render_sql(revision: str = "head") -> str:
    """Offline DDL for review — needs no database."""
    buffer = io.StringIO()
    command.upgrade(alembic_config(output=buffer), revision, sql=True)
    return buffer.getvalue()
