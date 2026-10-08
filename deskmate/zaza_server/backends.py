"""Repository selection.

``sqlite``   — :class:`SqliteDevRepository` (local development, the default).
``postgres`` — :class:`PostgresRepository` (production). Requires the
               ``zaza-postgres`` extra and a migrated database.

The FastAPI app, the sync service and the agent are identical for both.
"""

from __future__ import annotations

from pathlib import Path

from .config import DatabaseSettings, backend_from_env, database_settings_from_env
from .repository import CentralRepository, SqliteDevRepository


def open_repository(
    backend: str | None = None,
    *,
    sqlite_path: Path | None = None,
    settings: DatabaseSettings | None = None,
    actor_type: str = "SYSTEM",
    actor_id: str | None = None,
    require_current_schema: bool = True,
) -> CentralRepository:
    backend = backend or backend_from_env()
    if backend == "sqlite":
        if sqlite_path is None:
            raise ValueError("sqlite backend needs a database path")
        return SqliteDevRepository(sqlite_path)
    from .postgres import PostgresRepository  # noqa: PLC0415 — optional dependency

    return PostgresRepository(
        settings or database_settings_from_env(),
        actor_type=actor_type, actor_id=actor_id, require_current_schema=require_current_schema,
    )
