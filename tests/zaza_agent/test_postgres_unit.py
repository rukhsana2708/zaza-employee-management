"""Phase 4 checks that need no database: repository interface conformance,
configuration and credential hygiene, and the migration DDL (rendered
offline by Alembic)."""

from __future__ import annotations

import inspect
import logging
import re

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("alembic")

from deskmate.zaza_server.config import (  # noqa: E402
    ConfigError,
    DatabaseSettings,
    backend_from_env,
    database_settings_from_env,
)
from deskmate.zaza_server.postgres import PostgresRepository, migrate  # noqa: E402
from deskmate.zaza_server.repository import (  # noqa: E402
    CentralRepository,
    InMemoryRepository,
    RepositoryUnavailable,
    SqliteDevRepository,
)

PASSWORD = "S3cret:p@ss/word%with spaces"


# ─── interface conformance ─────────────────────────────────────────────────


def _protocol_methods() -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(fn)
        for name, fn in vars(CentralRepository).items()
        if callable(fn) and not name.startswith("_")
    }


@pytest.mark.parametrize("impl", [PostgresRepository, InMemoryRepository, SqliteDevRepository])
def test_repository_implements_the_whole_interface(impl):
    for name, expected in _protocol_methods().items():
        assert hasattr(impl, name), f"{impl.__name__} lacks {name}"
        actual = inspect.signature(getattr(impl, name))
        assert list(actual.parameters) == list(expected.parameters), f"{impl.__name__}.{name} signature differs"
        for pname, param in expected.parameters.items():
            assert actual.parameters[pname].kind == param.kind, f"{impl.__name__}.{name}({pname}) kind differs"


def test_app_and_service_contain_no_sql():
    import deskmate.zaza_server.app as app
    import deskmate.zaza_server.service as service

    for module in (app, service):
        source = inspect.getsource(module)
        assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\s", source), module.__name__
        assert "psycopg" not in source and "sqlite3" not in source


# ─── configuration & credentials ───────────────────────────────────────────


def test_settings_from_url_and_overrides():
    url = "postgresql://zaza_app:S3cret%3Ap%40ss%2Fword%25with%20spaces@db.internal:6543/zaza?sslmode=require"
    s = database_settings_from_env({"ZAZA_DATABASE_URL": url, "ZAZA_DB_POOL_MAX": "3"})
    assert (s.host, s.port, s.dbname, s.user, s.sslmode, s.pool_max) == (
        "db.internal", 6543, "zaza", "zaza_app", "require", 3,
    )
    assert s.password == PASSWORD
    s2 = database_settings_from_env({"ZAZA_DATABASE_URL": url, "ZAZA_DB_HOST": "127.0.0.1", "ZAZA_DB_NAME": "zaza2"})
    assert (s2.host, s2.dbname, s2.port) == ("127.0.0.1", "zaza2", 6543)


def test_settings_from_separate_variables_and_password_file(tmp_path):
    pw = tmp_path / "pw.txt"
    pw.write_text(PASSWORD + "\n", encoding="utf-8")
    s = database_settings_from_env({
        "ZAZA_DB_HOST": "localhost", "ZAZA_DB_PORT": "5433", "ZAZA_DB_NAME": "zaza",
        "ZAZA_DB_USER": "zaza_app", "ZAZA_DB_PASSWORD_FILE": str(pw), "ZAZA_DB_SCHEMA": "zaza",
    })
    assert (s.host, s.port, s.user, s.password, s.schema) == ("localhost", 5433, "zaza_app", PASSWORD, "zaza")
    assert "search_path=zaza" in s.pg_options() and "TimeZone=UTC" in s.pg_options()


def test_password_never_appears_in_repr_display_or_scrubbed_text():
    s = DatabaseSettings(user="zaza_app", password=PASSWORD)
    assert PASSWORD not in repr(s)
    assert PASSWORD not in s.display() and "***" in s.display()
    leaked = f"connection failed for postgresql://zaza_app:{PASSWORD}@host password={PASSWORD} x"
    assert PASSWORD not in s.scrub(leaked)
    assert "password=***" in s.scrub("dsn: host=x password=hunter2 user=y")


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"ZAZA_DB_POOL_MAX": "50"}, "pool sizes"),
        ({"ZAZA_DB_POOL_MIN": "5", "ZAZA_DB_POOL_MAX": "2"}, "pool sizes"),
        ({"ZAZA_DB_SSLMODE": "sometimes"}, "SSLMODE"),
        ({"ZAZA_DB_SCHEMA": "public; DROP TABLE x"}, "SCHEMA"),
        ({"ZAZA_DB_PORT": "abc"}, "ZAZA_DB_PORT"),
    ],
)
def test_invalid_settings_rejected(env, fragment):
    with pytest.raises(ConfigError, match=fragment):
        database_settings_from_env(env)


def test_invalid_url_error_does_not_echo_it():
    with pytest.raises(ConfigError) as info:
        database_settings_from_env({"ZAZA_DATABASE_URL": f"postgresql://u:{PASSWORD}@h:notaport/db"})
    assert PASSWORD not in str(info.value)


def test_default_pool_is_small():
    s = database_settings_from_env({})
    assert (s.pool_min, s.pool_max) == (1, 4)


def test_backend_selection():
    assert backend_from_env({}) == "sqlite"
    assert backend_from_env({"ZAZA_SERVER_BACKEND": "Postgres"}) == "postgres"
    with pytest.raises(ConfigError):
        backend_from_env({"ZAZA_SERVER_BACKEND": "mysql"})


def test_unreachable_database_error_contains_no_password(caplog):
    caplog.set_level(logging.DEBUG)
    s = DatabaseSettings(host="127.0.0.1", port=1, user="zaza_app", password=PASSWORD,
                         connect_timeout=1, pool_timeout=1)
    with pytest.raises(RepositoryUnavailable) as info:
        PostgresRepository(s)
    assert PASSWORD not in str(info.value)
    assert PASSWORD not in caplog.text


def test_cli_db_status_never_prints_the_password(monkeypatch, capsys):
    from deskmate.zaza_server.__main__ import main

    monkeypatch.setenv("ZAZA_DATABASE_URL", "postgresql://zaza_app:S3cret%3Ap%40ss@127.0.0.1:1/zaza")
    monkeypatch.setenv("ZAZA_DB_CONNECT_TIMEOUT", "1")
    assert main(["db-status"]) == 2
    out = capsys.readouterr()
    assert "S3cret:p@ss" not in out.out + out.err
    assert "***" in out.out


def test_env_example_contains_placeholders_only():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "ZAZA_DATABASE_URL" in text and "ZAZA_DB_POOL_MAX" in text
    for line in text.splitlines():
        if line.startswith("ZAZA_DB_PASSWORD="):
            assert line.strip() == "ZAZA_DB_PASSWORD="
    assert "CHANGE_ME" in text


# ─── migrations (rendered offline) ─────────────────────────────────────────


@pytest.fixture(scope="module")
def ddl() -> str:
    return migrate.render_sql()


def test_single_linear_migration_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrate.alembic_config())
    assert len(script.get_heads()) == 1
    assert migrate.head_revision() == "0004_manager_dashboard_auth"
    assert script.get_revision("0004_manager_dashboard_auth").down_revision == "0003_schedule_timezone"
    assert script.get_revision("0003_schedule_timezone").down_revision == "0002_attendance_summaries"
    assert script.get_revision("0002_attendance_summaries").down_revision == "0001_initial"


def test_ddl_creates_every_table(ddl):
    for table in ("employees", "devices", "device_tokens", "work_schedules", "work_sessions",
                  "activity_periods", "idle_periods", "application_usage_daily", "audit_logs"):
        assert f"CREATE TABLE {table} (" in ddl


def test_every_timestamp_column_is_timestamptz(ddl):
    columns = re.findall(r"^\s+(\w+_(?:at|utc))\s+(\w+)", ddl, flags=re.MULTILINE)
    assert columns, "no timestamp columns found"
    assert {name: kind for name, kind in columns if kind != "timestamptz"} == {}
    assert "timestamp without time zone" not in ddl.lower()
    assert not re.search(r"\btimestamp\b(?!tz)", ddl.split("-- Running upgrade")[1].lower())


def test_integrity_constraints_present(ddl):
    for name in (
        "devices_employee_fk", "work_sessions_device_fk", "work_sessions_employee_fk",
        "activity_periods_session_fk", "idle_periods_session_fk", "work_schedules_employee_fk",
        "work_sessions_version_positive", "activity_periods_version_positive",
        "activity_periods_durations_valid", "work_sessions_durations_valid", "idle_periods_durations_valid",
        "activity_periods_end_after_start", "idle_periods_end_after_start", "work_sessions_end_after_start",
        "activity_periods_status_valid", "work_sessions_status_valid", "devices_status_valid",
        "device_tokens_hash_format", "device_tokens_hash_unique",
        "application_usage_daily_device_day_app_unique", "activity_periods_privacy_excluded_redacted",
        "work_schedules_hours_valid",
    ):
        assert f"CONSTRAINT {name} " in ddl, name


def test_reporting_indexes_present(ddl):
    for index in (
        "work_sessions_employee_started_idx", "activity_periods_employee_started_idx",
        "idle_periods_employee_started_idx", "application_usage_daily_employee_day_idx",
        "work_sessions_open_idx", "activity_periods_device_seq_idx", "audit_logs_entity_idx",
    ):
        assert f"INDEX {index} ON" in ddl, index


def test_no_column_could_hold_a_raw_token(ddl):
    assert not re.search(r"^\s+(token|secret|password|raw_token)\s", ddl, flags=re.MULTILINE)


def test_upgrade_is_non_destructive(ddl):
    upgrade = ddl.split("-- Running upgrade")[1]
    for verb in ("DROP", "TRUNCATE", "DELETE", "ALTER TABLE .* DROP"):
        assert not re.search(rf"^\s*{verb}\b", upgrade, flags=re.MULTILINE | re.IGNORECASE), verb


def test_server_never_runs_migrations_itself():
    import deskmate.zaza_server.app as app
    import deskmate.zaza_server.postgres.repository as repo

    for module in (app, repo):
        source = inspect.getsource(module)
        assert "upgrade(" not in source and "command." not in source


def test_schedule_constraint_allows_overnight_shifts(ddl):
    start = ddl.index("CONSTRAINT work_schedules_hours_valid")
    check = ddl[start:ddl.index("CONSTRAINT work_schedules_timezone_valid")]
    assert "end_time > start_time AND" not in check  # no longer forces same-day shifts
    assert "end_time <> start_time" in check and "+ 86400" in check
    assert "expected_work_seconds > 0" in check
