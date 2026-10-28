"""Phase 10D: Cloud SQL connection settings, secret files, password
redaction, the Cloud Run preflight and the Terraform definition (static).
Nothing here needs PostgreSQL, Docker or Google Cloud."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from deskmate.zaza_server import cloudsql
from deskmate.zaza_server.config import ConfigError, DatabaseSettings, database_settings_from_env
from deskmate.zaza_server.preflight import FAIL, OK, WARN, check_cloud_run_config, check_config

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "deskmate" / "zaza_server"
TF = ROOT / "deploy" / "gcp" / "terraform"
INSTANCE = "zaza-employee-monitor:asia-south1:zaza-pg"
SOCKET = f"/cloudsql/{INSTANCE}"
FAKE_PASSWORD = "not-a-real-password-5f1c9a"  # test value only


# ─── instance connection name / socket ──────────────────────────────────────


@pytest.mark.parametrize("name", [
    INSTANCE, "zaza-employee-monitor:us-central1:a", "abcdef:europe-west4:zaza-pg-16",
    " zaza-employee-monitor:asia-south1:zaza-pg ",
])
def test_valid_instance_connection_names(name):
    assert cloudsql.instance_connection_name(name) == name.strip()
    assert cloudsql.socket_directory(name) == f"/cloudsql/{name.strip()}"


@pytest.mark.parametrize("name", [
    "", "zaza-pg", "zaza-employee-monitor:zaza-pg", "zaza-employee-monitor:asia-south1",
    "Zaza-Employee-Monitor:asia-south1:zaza-pg", "zaza-employee-monitor:asia-south1:Zaza",
    "zaza-employee-monitor:asia-south1:zaza-pg:5432", "example.com:project:asia-south1:pg",
    "zaza-employee-monitor:asia-south1:../etc", "zaza-employee-monitor:asia-south1:pg/x",
    "zaza-employee-monitor:asia-south1:-pg", "zaza-employee-monitor:asia-south1:pg-",
    "zaza-employee-monitor:asia-south1:1pg", "abc:asia-south1:pg",  # project too short
    "zaza-employee-monitor:asia south1:pg", "zaza-employee-monitor:asia-south1:pg; DROP",
    "postgresql://u:p@host/db", "host=evil.example", "/cloudsql/x", "zaza--monitor:asia-south1:pg",
    "zaza-employee-monitor:asia-south1:" + "a" * 64, None, 42,
])
def test_invalid_instance_connection_names_are_refused(name):
    with pytest.raises(cloudsql.CloudSqlConfigError):
        cloudsql.instance_connection_name(name)


def test_socket_root_must_be_absolute():
    assert cloudsql.socket_directory(INSTANCE, root="/tmp/sockets") == f"/tmp/sockets/{INSTANCE}"
    for root in ("relative", "/a/../b", "//x"):
        with pytest.raises(cloudsql.CloudSqlConfigError):
            cloudsql.socket_directory(INSTANCE, root=root)
    assert cloudsql.is_cloudsql_socket(SOCKET)
    assert not cloudsql.is_cloudsql_socket("/var/run/postgresql")
    assert not cloudsql.is_cloudsql_socket("127.0.0.1")


def test_env_cloudsql_mode_builds_the_socket_host():
    s = database_settings_from_env({
        "ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE, "ZAZA_DB_NAME": "zaza", "ZAZA_DB_USER": "zaza_app",
        "ZAZA_DB_PASSWORD": FAKE_PASSWORD})
    assert s.host == SOCKET and s.uses_unix_socket and s.port == 5432
    kwargs = s.connect_kwargs()
    assert kwargs["host"] == SOCKET and kwargs["dbname"] == "zaza"
    assert FAKE_PASSWORD not in s.display() and "Cloud SQL socket" in s.display() and SOCKET in s.display()


@pytest.mark.parametrize("extra", [{"ZAZA_DB_HOST": "10.0.0.5"},
                                   {"ZAZA_DATABASE_URL": "postgresql://u@db.example.com/zaza"}])
def test_cloudsql_mode_cannot_be_combined_with_a_host(extra):
    with pytest.raises(ConfigError, match="cannot be combined"):
        database_settings_from_env({"ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE, **extra})


def test_invalid_cloudsql_name_in_env_is_a_config_error():
    with pytest.raises(ConfigError, match="ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME"):
        database_settings_from_env({"ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": "db.example.com"})


def test_tcp_mode_is_unchanged_for_local_and_windows():
    s = database_settings_from_env({"ZAZA_DB_HOST": "localhost", "ZAZA_DB_PORT": "5433", "ZAZA_DB_NAME": "zaza",
                                    "ZAZA_DB_USER": "zaza_app"})
    assert (s.host, s.port, s.uses_unix_socket) == ("localhost", 5433, False)
    assert s.display() == "postgresql://zaza_app@localhost:5433/zaza (sslmode=prefer)"
    assert database_settings_from_env({}).host == "127.0.0.1"
    for host in ("127.0.0.1", "::1", "db.internal.example", "zaza-test-pg", "/var/run/postgresql"):
        assert DatabaseSettings(host=host).host == host


@pytest.mark.parametrize("host", ["", "host with space", "evil.example dbname=x", "a=b", "postgresql://x",
                                  "/cloudsql/../etc", "relative/dir", "host1,host2", "x\ny"])
def test_unsafe_hosts_are_refused(host):
    with pytest.raises(ConfigError, match="database host"):
        DatabaseSettings(host=host)


def test_control_database_supports_cloudsql_socket():
    from deskmate.zaza_server.tenancy.config import control_database_settings_from_env

    s = control_database_settings_from_env({
        "ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE, "ZAZA_CONTROL_DB_NAME": "zaza_control",
        "ZAZA_CONTROL_DB_USER": "zaza_control_app"})
    assert s.host == SOCKET and s.dbname == "zaza_control" and (s.pool_min, s.pool_max) == (0, 2)
    with pytest.raises(ConfigError, match="cannot be combined"):
        control_database_settings_from_env({"ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE,
                                            "ZAZA_CONTROL_DB_HOST": "10.1.1.1", "ZAZA_CONTROL_DB_NAME": "zaza_control"})


# ─── secret files and redaction ─────────────────────────────────────────────


def test_password_files_are_read_and_never_shown(tmp_path):
    from deskmate.zaza_server.tenancy.config import control_database_settings_from_env

    app, ctl = tmp_path / "app", tmp_path / "ctl"
    app.write_text(FAKE_PASSWORD + "\n", encoding="utf-8")
    ctl.write_text(FAKE_PASSWORD + "-control\n", encoding="utf-8")
    env = {"ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE, "ZAZA_DB_USER": "zaza_app",
           "ZAZA_DB_PASSWORD_FILE": str(app), "ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE,
           "ZAZA_CONTROL_DB_NAME": "zaza_control", "ZAZA_CONTROL_DB_USER": "zaza_control_app",
           "ZAZA_CONTROL_DB_PASSWORD_FILE": str(ctl)}
    s, c = database_settings_from_env(env), control_database_settings_from_env(env)
    assert s.password == FAKE_PASSWORD and c.password == FAKE_PASSWORD + "-control"
    for settings in (s, c):
        for text in (repr(settings), settings.display(), str(settings)):
            assert FAKE_PASSWORD not in text
        assert settings.scrub(f"FATAL: password authentication failed: {settings.password}") \
            == "FATAL: password authentication failed: ***"


def test_unreadable_password_file_error_names_no_secret(tmp_path):
    with pytest.raises(ConfigError) as exc:
        database_settings_from_env({"ZAZA_DB_PASSWORD_FILE": str(tmp_path / "missing")})
    assert "cannot read ZAZA_DB_PASSWORD_FILE" in str(exc.value)


def test_log_formatter_redacts_both_database_passwords(tmp_path, monkeypatch):
    import logging

    from deskmate.zaza_server.__main__ import _log_secrets
    from deskmate.zaza_server.logs import RedactingFormatter

    app, ctl = tmp_path / "app", tmp_path / "ctl"
    app.write_text("app-" + FAKE_PASSWORD, encoding="utf-8")
    ctl.write_text("ctl-" + FAKE_PASSWORD, encoding="utf-8")
    for key in [k for k in list(__import__("os").environ) if k.startswith("ZAZA_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("ZAZA_DB_PASSWORD_FILE", str(app))
    monkeypatch.setenv("ZAZA_CONTROL_DB_NAME", "zaza_control")
    monkeypatch.setenv("ZAZA_CONTROL_DB_PASSWORD_FILE", str(ctl))
    secrets = _log_secrets("postgres")
    formatter = RedactingFormatter(secrets=secrets)
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed with app-%s and ctl-%s", (
        FAKE_PASSWORD, FAKE_PASSWORD), None)
    assert FAKE_PASSWORD not in formatter.format(record)
    assert _log_secrets("sqlite") == []


# ─── Cloud Run preflight ────────────────────────────────────────────────────


def _cloud_run_env(tmp_path, **overrides):
    (tmp_path / "app").write_text(FAKE_PASSWORD, encoding="utf-8")
    (tmp_path / "ctl").write_text(FAKE_PASSWORD + "c", encoding="utf-8")
    env = {
        "ZAZA_SERVER_BACKEND": "postgres", "PORT": "8080",
        "ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE, "ZAZA_DB_USER": "zaza_app",
        "ZAZA_DB_PASSWORD_FILE": str(tmp_path / "app"), "ZAZA_DB_POOL_MIN": "0", "ZAZA_DB_POOL_MAX": "2",
        "ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME": INSTANCE, "ZAZA_CONTROL_DB_NAME": "zaza_control",
        "ZAZA_CONTROL_DB_USER": "zaza_control_app", "ZAZA_CONTROL_DB_PASSWORD_FILE": str(tmp_path / "ctl"),
        "ZAZA_TENANT_POOL_MAX": "2", "ZAZA_TENANT_POOL_CACHE_MAX": "8", "ZAZA_TENANT_POOL_IDLE_SECONDS": "300",
        "ZAZA_CLOUD_RUN_MAX_INSTANCES": "3", "ZAZA_DB_CONNECTION_BUDGET": "70",
        "ZAZA_DASHBOARD_COOKIE_SECURE": "true", "ZAZA_PUBLIC_HOST": "zaza.example.com",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def _by_name(checks):
    return {c.name: c for c in checks}


def test_cloud_run_preflight_accepts_a_complete_configuration(tmp_path):
    checks = check_cloud_run_config(_cloud_run_env(tmp_path))
    assert [c for c in checks if c.status == FAIL] == []
    named = _by_name(checks)
    assert named["connection budget"].status == OK and "= 60 of 70" in named["connection budget"].detail
    assert named["front-end proxy"].status == WARN  # honest: not implemented yet
    assert all(FAKE_PASSWORD not in c.detail for c in checks)


@pytest.mark.parametrize(("overrides", "name"), [
    ({"ZAZA_SERVER_BACKEND": "sqlite"}, "backend"),
    ({"ZAZA_SERVER_BACKEND": None}, "backend"),
    ({"ZAZA_CONTAINER_ALLOW_SQLITE": "local-test-only"}, "sqlite opt-in"),
    ({"PORT": "http"}, "PORT"),
    ({"ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": None}, "database Cloud SQL"),
    ({"ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME": "db.example.com"}, "database Cloud SQL"),
    ({"ZAZA_DB_HOST": "10.0.0.3"}, "database address"),
    ({"ZAZA_DATABASE_URL": "postgresql://x@h/zaza"}, "database address"),
    ({"ZAZA_DB_PASSWORD": FAKE_PASSWORD}, "database password source"),
    ({"ZAZA_DB_PASSWORD_FILE": None}, "database password file"),
    ({"ZAZA_DB_PASSWORD_FILE": "/nonexistent/zaza/pw"}, "database password file"),
    ({"ZAZA_DB_USER": None}, "database user"),
    ({"ZAZA_DB_USER": "postgres"}, "database user"),
    ({"ZAZA_CONTROL_DB_NAME": None}, "control database"),
    ({"ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME": None}, "control database Cloud SQL"),
    ({"ZAZA_CONTROL_DB_PASSWORD": FAKE_PASSWORD}, "control database password source"),
    ({"ZAZA_CONTROL_DB_PASSWORD_FILE": None}, "control database password file"),
    ({"ZAZA_TENANT_POOL_MAX": "10"}, "tenant pools"),
    ({"ZAZA_TENANT_POOL_CACHE_MAX": "500"}, "tenant pools"),
    ({"ZAZA_TENANT_POOL_IDLE_SECONDS": "0"}, "tenant pools"),
    ({"ZAZA_DB_POOL_MAX": "10"}, "database pool"),
    ({"ZAZA_CLOUD_RUN_MAX_INSTANCES": None}, "cloud run max instances"),
    ({"ZAZA_CLOUD_RUN_MAX_INSTANCES": "1000"}, "cloud run max instances"),
    ({"ZAZA_DB_CONNECTION_BUDGET": None}, "db connection budget"),
    ({"ZAZA_CLOUD_RUN_MAX_INSTANCES": "10"}, "connection budget"),  # 10 x 20 = 200 > 70
    ({"ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE": "/secrets/key.json"}, "google credentials"),
    ({"ZAZA_DASHBOARD_COOKIE_SECURE": "false"}, "dashboard cookies"),
])
def test_cloud_run_preflight_failures(tmp_path, overrides, name):
    checks = check_cloud_run_config(_cloud_run_env(tmp_path, **overrides))
    failed = {c.name for c in checks if c.status == FAIL}
    assert name in failed, [(c.status, c.name, c.detail) for c in checks]
    assert all(FAKE_PASSWORD not in c.detail for c in checks)


def test_cloud_run_preflight_warns_about_a_log_file(tmp_path):
    named = _by_name(check_cloud_run_config(_cloud_run_env(tmp_path, ZAZA_LOG_FILE="/var/log/zaza.log")))
    assert named["log file"].status == WARN


def test_windows_production_preflight_is_unchanged(tmp_path):
    """--production keeps the Windows/VPS rules: a TCP host is fine there,
    and it still demands ZAZA_LOG_FILE and a password file."""
    pw = tmp_path / "pw"
    pw.write_text(FAKE_PASSWORD, encoding="utf-8")
    named = _by_name(check_config({"ZAZA_SERVER_BACKEND": "postgres", "ZAZA_DB_USER": "zaza_app",
                                   "ZAZA_DB_PASSWORD_FILE": str(pw)}, production=True))
    assert named["database"].status == OK and "127.0.0.1:5432" in named["database"].detail
    assert named["log file"].status == FAIL
    assert "connection budget" not in named


def test_check_config_cli_modes(tmp_path):
    env = {k: v for k, v in _cloud_run_env(tmp_path).items()}
    import os

    run = subprocess.run([sys.executable, "-m", "deskmate.zaza_server", "check-config", "--cloud-run"],
                         capture_output=True, text=True, cwd=ROOT, timeout=60,
                         env={**{k: v for k, v in os.environ.items() if not k.startswith("ZAZA_")}, **env})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "connection budget" in run.stdout and FAKE_PASSWORD not in run.stdout + run.stderr
    both = subprocess.run([sys.executable, "-m", "deskmate.zaza_server", "check-config", "--cloud-run",
                           "--production"], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert both.returncode == 2 and "not allowed with" in both.stderr


# ─── no request-controlled database address ─────────────────────────────────


def test_no_http_code_builds_database_settings():
    """Request handlers (app, dashboard, auth, service) never construct
    database settings or read the Cloud SQL/host variables."""
    for path in [SERVER / "app.py", SERVER / "auth.py", SERVER / "service.py", *(SERVER / "dashboard").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        for needle in ("DatabaseSettings(", "database_settings_from_env", "socket_directory", "ZAZA_DB_HOST",
                       "CLOUDSQL", "tenant_database_settings", "connect_kwargs"):
            assert needle not in text, f"{path.name} uses {needle}"


def test_cloudsql_instance_name_is_only_read_from_server_configuration():
    users = sorted(p.relative_to(SERVER).as_posix() for p in SERVER.rglob("*.py")
                   if "CLOUDSQL_INSTANCE_CONNECTION_NAME" in p.read_text(encoding="utf-8"))
    assert set(users) <= {"cloudsql.py", "config.py", "preflight.py", "tenancy/config.py"}, users


def test_health_endpoint_exposes_no_database_details(tmp_path):
    from fastapi.testclient import TestClient

    from deskmate.zaza_server.app import create_app
    from deskmate.zaza_server.repository import SqliteDevRepository

    repo = SqliteDevRepository(tmp_path / "c.db")
    try:
        body = TestClient(create_app(repo)).get("/api/v1/sync/health")
        assert body.status_code == 200
        text = body.text.lower()
        for word in ("cloudsql", "zaza_t_", "postgres", "password", "organization", "tenant", "socket", "host"):
            assert word not in text
    finally:
        repo.close()


def test_serve_never_migrates_or_provisions():
    main = (SERVER / "__main__.py").read_text(encoding="utf-8")
    serve = main[main.index("def _serve_prepare"):main.index('elif args.command == "add-employee"')]
    for word in ("upgrade", "migrat", "provision", "TenantMigrationRunner", "create_database"):
        assert word not in serve
    entry = (ROOT / "deploy" / "gcp" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "migrate" not in entry.split("exec python", 1)[1]


# ─── Terraform definition (static; never applied here) ──────────────────────


def _tf(*, comments: bool = True) -> str:
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(TF.glob("*.tf")))
    return text if comments else "\n".join(line.split("#")[0] for line in text.splitlines())


def test_terraform_files_exist_and_no_state_is_committed():
    assert {p.name for p in TF.glob("*.tf")} == {"versions.tf", "variables.tf", "main.tf", "cloudsql.tf",
                                                  "cloud_run.tf", "outputs.tf", "build.tf"}
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".terraform/", "*.tfstate", "*.tfstate.*", "*.tfvars", "*.tfplan"):
        assert pattern in gitignore.splitlines()
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("git is not available here (e.g. inside the server image)")
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                              "deploy/gcp/terraform"], capture_output=True, text=True, cwd=ROOT).stdout.split()
    assert tracked
    assert not [t for t in tracked if re.search(r"\.tfstate|\.tfplan|\.terraform/|(?<!\.example)\.tfvars$", t)]


def test_terraform_cloud_sql_instance_design():
    tf = _tf()
    assert 'database_version    = "POSTGRES_16"' in tf
    assert re.search(r'variable "region" \{[^}]*default\s+= "asia-south1"', tf, re.DOTALL)
    assert re.search(r'variable "project_id" \{[^}]*default\s+= "zaza-employee-monitor"', tf, re.DOTALL)
    assert not re.search(r'variable "sql_instance_name" \{[^}]*default\s*=', tf.split("variable \"sql_tier\"")[0],
                         re.DOTALL), "no real instance name is hard-coded yet"
    assert "point_in_time_recovery_enabled = true" in tf and re.search(r"backup_configuration \{\s+enabled\s+= true", tf)
    assert "transaction_log_retention_days = var.pitr_log_retention_days" in tf
    assert 'retention_unit   = "COUNT"' in tf
    assert "deletion_protection_enabled = var.deletion_protection" in tf
    assert re.search(r'variable "deletion_protection" \{[^}]*default\s+= true', tf, re.DOTALL)
    assert 'connector_enforcement = "REQUIRED"' in tf and "authorized_networks" not in tf
    assert 'name     = "zaza_control"' in tf


def test_terraform_contains_no_secrets_keys_or_tenant_databases():
    tf = _tf(comments=False) + (TF / "terraform.tfvars.example").read_text(encoding="utf-8")
    for forbidden in ("google_service_account_key", "google_sql_user", "google_secret_manager_secret_version",
                      "password =", "password  =", "private_key", "BEGIN ", "zaza_t_", "credentials",
                      "google_compute_global_address", "google_dns",
                      "google_compute_security_policy"):
        assert forbidden not in tf, forbidden
    # tenant databases are application lifecycle: only the control database is a Terraform resource
    assert tf.count('resource "google_sql_database" ') == 1


def test_terraform_cloud_run_is_gated_and_never_migrates():
    run = (TF / "cloud_run.tf").read_text(encoding="utf-8")
    assert re.search(r'variable "deploy_service" \{[^}]*default\s+= false', _tf(), re.DOTALL)
    assert re.search(r'variable "deploy_migration_job" \{[^}]*default\s+= false', _tf(), re.DOTALL)
    assert run.count("count               = local.deploy_service") == 1  # web service: its own gate
    assert run.count("count               = local.deploy_job") == 1      # migration job: its own gate
    assert "deploy_service = var.deploy_service ? 1 : 0" in run and "deploy_job     = var.deploy_migration_job ? 1 : 0" in run
    public = run.split('resource "google_cloud_run_v2_service_iam_member" "public"')[1].split("resource ")[0]
    assert "var.deploy_service && var.allow_unauthenticated" in public  # the job never makes anything public
    service = run.split('resource "google_cloud_run_v2_service" "server"')[1].split("resource ")[0]
    assert "command" not in service and "args" not in service  # the image entrypoint: serve only
    assert "max_instance_count = var.max_instances" in service and "min_instance_count = 0" in service
    assert "/api/v1/sync/health" in service
    job = run.split('resource "google_cloud_run_v2_job" "migrate"')[1]
    assert 'args    = ["control-db-status"]' in job  # the job reports by default; migrations are explicit
    assert "google_service_account.migrator.email" in job and "google_service_account.runtime.email" in service
    assert "ZAZA_DB_PASSWORD_FILE" in run and "ZAZA_DB_PASSWORD " not in run


def test_terraform_runtime_identity_is_least_privilege():
    main = (TF / "main.tf").read_text(encoding="utf-8")
    roles = set(re.findall(r'role\s+= "(roles/[^"]+)"', main))
    assert roles == {"roles/cloudsql.client", "roles/secretmanager.secretAccessor"}
    assert 'for_each  = toset(["db_app", "db_control"])' in main  # runtime cannot read the migrator secret
    assert "roles/owner" not in main and "roles/editor" not in main and "cloudsql.admin" not in main


def test_terraform_lock_file_pins_the_validated_provider():
    lock = (TF / ".terraform.lock.hcl").read_text(encoding="utf-8")
    assert 'provider "registry.terraform.io/hashicorp/google"' in lock and "h1:" in lock


def test_docker_build_context_still_excludes_terraform():
    ignore = (ROOT / "deploy" / "gcp" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert ignore.count("*") == 1 and not [line for line in ignore if "terraform" in line and line.startswith("!")]


# ─── code-level guards ──────────────────────────────────────────────────────


def test_new_modules_have_no_google_cloud_api_calls():
    for path in (SERVER / "cloudsql.py", *(SERVER / "tenancy").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [m for m in imported if m.startswith(("google", "googleapiclient", "requests", "httpx"))], path


def test_terraform_buckets_are_private_and_only_the_build_source_bucket():
    tf = _tf(comments=False)
    assert tf.count('resource "google_storage_bucket" ') == 1
    bucket = tf.split('resource "google_storage_bucket" "build_source"')[1].split("resource ")[0]
    for line in ("uniform_bucket_level_access = true", 'public_access_prevention    = "enforced"',
                 "force_destroy               = false", "location                    = var.region"):
        assert line in bucket
    assert "allUsers" not in tf.replace('member   = "allUsers"', "") and "allAuthenticatedUsers" not in tf


def test_build_identity_is_dedicated_and_least_privilege():
    build = (TF / "build.tf").read_text(encoding="utf-8")
    assert 'account_id   = "zaza-build"' in build
    roles = re.findall(r'role\s+= "(roles/[^"]+)"', build)
    assert sorted(roles) == ["roles/artifactregistry.writer", "roles/logging.logWriter", "roles/storage.objectViewer"]
    # Artifact Registry and Storage roles are scoped to ONE repository / ONE bucket, not the project
    assert 'resource "google_artifact_registry_repository_iam_member" "build_writer"' in build
    assert 'resource "google_storage_bucket_iam_member" "build_source_reader"' in build
    assert build.count("google_project_iam_member") == 1  # only the log writer is project-level
    everything = _tf(comments=False)
    for forbidden in ("roles/cloudbuild.builds.builder", "roles/owner", "roles/editor", "roles/storage.admin",
                      "roles/cloudsql.admin", "roles/iam.serviceAccountAdmin", "roles/resourcemanager.projectIamAdmin"):
        assert forbidden not in everything
    accessors = re.findall(r'resource "google_secret_manager_secret_iam_member"[^}]*?member\s+= "serviceAccount:\$\{google_service_account\.(\w+)', everything, re.DOTALL)
    assert sorted(set(accessors)) == ["migrator", "runtime"]  # the build identity never reads database secrets


def test_cloud_build_config_is_pinned_and_secret_free():
    cfg = (ROOT / "deploy" / "gcp" / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "--file=deploy/gcp/Dockerfile" in cfg and "DOCKER_BUILDKIT=1" in cfg and "--platform=linux/amd64" in cfg
    assert "logging: CLOUD_LOGGING_ONLY" in cfg and "${_IMAGE}:${_GIT_SHA}" in cfg
    assert "latest" not in cfg and "secretEnv" not in cfg and "availableSecrets" not in cfg
    assert not re.search(r"(?i)password|private_key|token=", cfg)
    ignore = (ROOT / "deploy" / "gcp" / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    assert "!deploy/gcp/cloudbuild.yaml" not in ignore  # not part of the image's build context
