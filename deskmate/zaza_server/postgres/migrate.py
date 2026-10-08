"""Schema migrations (Alembic), run only by an explicit operator command.

- ``upgrade(settings)``   — ``python -m deskmate.zaza_server migrate``.
  Idempotent: at head it does nothing. Each revision runs in its own
  transaction, so a failure leaves the schema at the previous revision.
- ``schema_status(...)``  — read-only check used at server start-up. The
  server refuses to start on an out-of-date schema; it never migrates (or
  drops anything) by itself.
- ``render_sql()``        — the DDL as text, without a database (review).

Downgrades exist in the revision files for completeness but are not exposed
by the CLI: they are destructive and need a deliberate Alembic invocation.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

# SQLAlchemy is used only by Alembic here, where speed is irrelevant. Its
# optional compiled extensions can be blocked by Windows Application Control
# (Smart App Control) on locked-down machines, so use the pure-Python path.
os.environ.setdefault("DISABLE_SQLALCHEMY_CEXT_RUNTIME", "1")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from ..config import DatabaseSettings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
VERSION_TABLE = "alembic_version"


@dataclass(frozen=True)
class SchemaStatus:
    current: str | None
    head: str

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


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
