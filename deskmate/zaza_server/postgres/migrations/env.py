"""Alembic environment for the ZaZa central schema.

Run through ``python -m deskmate.zaza_server migrate`` (see
``deskmate/zaza_server/postgres/migrate.py``), which passes the database
settings in ``config.attributes`` — the connection URL with its password is
never written to a file, a log, or ``alembic.ini``.

Offline mode (``--sql``) renders the DDL without any database connection,
for review.
"""

from __future__ import annotations

from alembic import context

config = context.config


def run_offline() -> None:
    context.configure(
        dialect_name="postgresql",
        literal_binds=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    settings = config.attributes.get("zaza_settings")
    if settings is None:
        raise RuntimeError("run migrations via `python -m deskmate.zaza_server migrate`")
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    engine = create_engine(
        settings.sqlalchemy_url(), poolclass=NullPool,
        connect_args={"options": settings.pg_options(), "connect_timeout": settings.connect_timeout},
    )
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


def _run(connection) -> None:  # noqa: ANN001
    # Each migration runs in its own transaction: DDL in PostgreSQL is
    # transactional, so a failed migration leaves the schema untouched.
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
