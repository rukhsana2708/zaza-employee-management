"""Alembic environment for the ZaZa CONTROL-PLANE database (Phase 10C).

A separate migration chain from the tenant schema
(``deskmate/zaza_server/postgres/migrations``): its own directory, its own
revision ids (``c0001_...``) and its own version table
(``zaza_control_version``), so the two can never be confused.

Run only through ``python -m deskmate.zaza_server control-migrate`` (see
``tenancy/control/migrate.py``) — never at web-server start-up. Refuses to
run in a database that already holds the tenant schema: the control plane
never shares a database with customer activity data.
"""

from __future__ import annotations

from alembic import context

config = context.config
VERSION_TABLE = "zaza_control_version"
TENANT_MARKERS = ("alembic_version", "employees", "devices", "activity_periods", "work_sessions")


def run_offline() -> None:
    context.configure(dialect_name="postgresql", literal_binds=True, transaction_per_migration=True,
                      version_table=VERSION_TABLE)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    settings = config.attributes.get("zaza_settings")
    if settings is None:
        raise RuntimeError("run control migrations via `python -m deskmate.zaza_server control-migrate`")
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


def _refuse_tenant_database(connection) -> None:  # noqa: ANN001
    from sqlalchemy import text  # noqa: PLC0415

    for name in TENANT_MARKERS:
        if connection.execute(text("SELECT to_regclass(:n)::text"), {"n": name}).scalar() is not None:
            raise RuntimeError(f"refusing to install the control plane: this database contains the tenant table "
                               f"{name!r}. The control plane needs its own database.")


def _run(connection) -> None:  # noqa: ANN001
    _refuse_tenant_database(connection)
    if connection.in_transaction():
        connection.commit()  # end the read-only check's implicit transaction
    context.configure(connection=connection, transaction_per_migration=True, version_table=VERSION_TABLE)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
