"""Phase 10 production deployment artefacts (static checks + the credential
helper): server-only dependencies, the deployed file list, the scripts,
the configuration template and the database role SQL."""

from __future__ import annotations

import ast
import base64
import fnmatch
import hashlib
import hmac
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from deskmate.zaza_server.config import read_env_file

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "windows"
SQL = ROOT / "deploy" / "postgres"
REQUIREMENTS = DEPLOY / "requirements-server.txt"
LOCK = DEPLOY / "requirements-server.lock.txt"
TEMPLATE = DEPLOY / "zaza-server.env.template"
SERVER_SOURCES = [*sorted((ROOT / "deskmate" / "zaza_server").rglob("*.py")),
                  ROOT / "deskmate" / "zaza" / "sync" / "protocol.py"]

# Employee-side / DeskMate capture packages that must never be on the server.
PROHIBITED = {
    "mss", "pillow", "pywin32", "pywin32-ctypes", "comtypes", "uiautomation", "pynput", "keyboard", "mouse",
    "pyperclip", "pytesseract", "rapidocr", "rapidocr-onnxruntime", "easyocr", "openvino", "openvino-genai",
    "opencv-python", "opencv-python-headless", "sounddevice", "soundfile", "faster-whisper", "pyaudiowpatch",
    "pyaudio", "silero-vad", "torch", "torchaudio", "pyannote-audio", "onnxruntime", "onnxruntime-directml",
    "tokenizers", "fastembed", "modelscope", "transformers", "unsloth", "peft", "accelerate", "numpy",
    "psutil", "pyinstaller", "pyinstaller-hooks-contrib", "mcp", "pyyaml", "pydantic-settings", "typer",
}
PROHIBITED_PREFIXES = ("winrt-", "pyannote")

# Third-party top-level import -> the requirement that provides it.
PROVIDERS = {
    "fastapi": "fastapi", "starlette": "fastapi", "pydantic": "pydantic", "uvicorn": "uvicorn",
    "psycopg": "psycopg", "psycopg_pool": "psycopg-pool", "sqlalchemy": "sqlalchemy", "alembic": "alembic",
    "jinja2": "jinja2", "argon2": "argon2-cffi", "googleapiclient": "google-api-python-client",
    "google": "google-auth", "google_auth_httplib2": "google-auth-httplib2", "httplib2": "httplib2",
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith(("-", "--")):
            names.add(_norm(re.split(r"[\[<>=!~; ]", line, maxsplit=1)[0]))
    return names


def _lock_entries() -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    current = None
    for line in LOCK.read_text(encoding="ascii").splitlines():
        if m := re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", line):
            current = _norm(m.group(1))
            entries[current] = []
        elif m := re.match(r"^\s+--hash=sha256:([0-9a-f]{64})", line):
            entries[current].append(m.group(1))
    return entries


def _is_prohibited(name: str) -> bool:
    return name in PROHIBITED or name.startswith(PROHIBITED_PREFIXES)


# ─── dependencies ──────────────────────────────────────────────────────────


def test_server_requirements_contain_no_employee_side_packages():
    names = _requirement_names(REQUIREMENTS)
    assert names, "no requirements parsed"
    assert not {n for n in names if _is_prohibited(n)}


def test_lock_contains_no_employee_side_packages_even_transitively():
    names = set(_lock_entries())
    assert not {n for n in names if _is_prohibited(n)}


def test_lock_pins_every_requirement_with_hashes():
    lock = _lock_entries()
    assert _requirement_names(REQUIREMENTS) - {"psycopg"} <= set(lock)
    assert {"psycopg", "psycopg-binary"} <= set(lock)  # psycopg[binary]
    assert all(len(hashes) >= 1 for hashes in lock.values()), "every pin needs a --hash"


def _third_party_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in SERVER_SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top not in sys.stdlib_module_names and top not in ("deskmate", "__future__"):
                    found.setdefault(top, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_server_import_is_covered_by_the_server_requirements():
    imports = _third_party_imports()
    unknown = set(imports) - set(PROVIDERS)
    assert not unknown, f"server imports not mapped to a requirement: { {m: imports[m] for m in unknown} }"
    names = _requirement_names(REQUIREMENTS)
    assert {_norm(PROVIDERS[m]) for m in imports} <= names


def test_server_requirements_are_not_padded():
    """Every direct requirement is imported by the server (or is tzdata)."""
    used = {_norm(PROVIDERS[m]) for m in _third_party_imports()} | {"tzdata"}
    assert _requirement_names(REQUIREMENTS) <= used


def test_server_code_imports_only_the_shared_protocol_from_the_agent():
    for path in SERVER_SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("deskmate."):
                assert node.module.startswith(("deskmate.zaza_server", "deskmate.zaza.sync.protocol")), \
                    f"{path}: {node.module}"


# ─── the deployed file list ────────────────────────────────────────────────


def _file_list() -> tuple[list[str], list[str]]:
    """(include paths, ``!`` exclusion wildcards) from server-files.txt."""
    lines = [line.strip() for line in (DEPLOY / "server-files.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    return [x for x in lines if not x.startswith("!")], [x[1:] for x in lines if x.startswith("!")]


def test_file_list_uses_plain_paths_only():
    includes, excludes = _file_list()
    assert includes and excludes
    assert not [x for x in includes if x.startswith(":") or "*" in x]  # git ls-tree has no pathspec magic


def _deployed_files() -> list[str]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    # tracked + new (not ignored) files: what the next commit, and so the next deployment, will contain
    includes, excludes = _file_list()
    result = subprocess.run([git, "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "--",
                             *includes], capture_output=True,
                            text=True, check=False)
    if result.returncode != 0:
        pytest.skip(f"git ls-files failed: {result.stderr.strip()}")
    # PowerShell -like semantics: `*` matches any characters, including `/`
    return [f for f in result.stdout.split() if not any(fnmatch.fnmatchcase(f, x) for x in excludes)]


def _deploy_forbidden() -> re.Pattern:
    text = (DEPLOY / "Deploy-ZaZaServer.ps1").read_text(encoding="utf-8")
    pattern = re.search(r"^\$Forbidden = '(.+)'$", text, re.MULTILINE).group(1)
    return re.compile(pattern, re.IGNORECASE)


def test_deployed_files_are_only_server_code():
    files = _deployed_files()
    assert "deskmate/zaza_server/__main__.py" in files and "deskmate/zaza/sync/protocol.py" in files
    assert "deskmate/zaza_server/postgres/migrations/versions/0004_manager_dashboard_auth.py" in files
    assert "deskmate/zaza_server/dashboard/templates/login.html" in files
    forbidden = _deploy_forbidden()
    for f in files:
        assert f.startswith(("deskmate/zaza_server/", "deskmate/zaza/sync/protocol.py", "deskmate/__init__.py",
                             "deskmate/zaza/__init__.py", "deskmate/zaza/sync/__init__.py", "LICENSE")), f
        assert not forbidden.search(f), f
    assert not any(f.endswith(".zip") for f in files)
    for agent_module in ("deskmate/zaza/agent.py", "deskmate/zaza/sync/worker.py", "deskmate/zaza/sync/credentials.py"):
        assert agent_module not in files


@pytest.mark.parametrize("name", [
    ".git/config", ".env", "config/.env.production", "zaza-server.env", "secrets/db-app.password",
    "deskmate/zaza_server/postgres/migrations.zip", "deskmate/zaza_server/__pycache__/app.cpython-314.pyc",
    "x/google-service-account.json", ".claude/settings.json", "transcript.jsonl", "tasks/example-task.output",
    "key.pem", "central_dev.db", "zaza-server.log",
])
def test_deploy_refuses_forbidden_names(name):
    assert _deploy_forbidden().search(name.replace("/", "\\"))
    assert _deploy_forbidden().search(name)


def test_deployed_tree_imports_on_its_own(tmp_path):
    """Copy ONLY the deployed files and import every server module from that
    copy in a fresh, isolated interpreter: the file list is complete, and no
    capture/UI package gets imported."""
    for module in ("fastapi", "psycopg", "psycopg_pool", "alembic", "sqlalchemy", "jinja2", "argon2",
                   "googleapiclient", "google_auth_httplib2"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{module} is not installed in this environment")
    for f in _deployed_files():
        target = tmp_path / f
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / f, target)
    modules = sorted(
        ".".join(p.relative_to(tmp_path).with_suffix("").parts).removesuffix(".__init__")
        for p in (tmp_path / "deskmate" / "zaza_server").rglob("*.py")
        if "migrations" not in p.parts)
    code = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        f"for m in {modules!r}:\n"
        "    mod = importlib.import_module(m)\n"
        f"    assert mod.__file__.startswith({str(tmp_path)!r}), (m, mod.__file__)\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('mss', 'PIL', 'win32api', 'win32gui', 'pywintypes', 'comtypes', 'uiautomation', 'winrt', 'pynput', "
        "'numpy', 'deskmate.zaza.agent'))\n"
        "bad += sorted(m for m in sys.modules if m.startswith('deskmate.zaza.') and m not in "
        "('deskmate.zaza.sync', 'deskmate.zaza.sync.protocol'))\n"
        "assert not bad, bad\n"
        "print('ok', len(sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-3000:]


# ─── scripts ───────────────────────────────────────────────────────────────


SCRIPTS = sorted(DEPLOY.glob("*.ps*1"))


def _code_lines(path: Path) -> list[str]:
    """Script lines without comments (``#`` lines and ``<# ... #>`` blocks)."""
    text = re.sub(r"<#.*?#>", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_scripts_exist():
    names = {p.name for p in SCRIPTS}
    assert {"Deploy-ZaZaServer.ps1", "Rollback-ZaZaServer.ps1", "ZaZaService.ps1", "Test-ZaZaServer.ps1",
            "Invoke-ZaZaJobs.ps1", "Initialize-ZaZaDatabase.ps1", "Install-ZaZaConfig.ps1",
            "ZaZaProduction.psm1"} <= names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_fail_fast(script):
    text = script.read_text(encoding="utf-8")
    assert "Set-StrictMode -Version Latest" in text
    if script.suffix == ".ps1":
        assert "$ErrorActionPreference = 'Stop'" in text


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_do_not_touch_other_systems(script):
    code = "\n".join(_code_lines(script))
    for word in ("ZazaVoice", "Caddyfile", "caddy reload", "caddy.exe", "pg_hba", "postgresql.conf", "ALTER ROLE",
                 "ALTER SYSTEM", "NetFirewall", "netsh", "Register-ScheduledTask", "actions-runner", "docker",
                 "pm2", "Invoke-Expression", "iex "):
        assert word.lower() not in code.lower(), f"{script.name}: {word}"
    for path in re.findall(r"C:\\apps\\[^\\'\"\s]*", code):
        assert path == "C:\\apps\\ZaZaEmployeeManagement", f"{script.name}: {path}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_contain_no_secret_values(script):
    text = script.read_text(encoding="utf-8")
    assert not re.search(r"SCRAM-SHA-256\$\d+:[A-Za-z0-9+/]{8,}", text)
    assert not re.search(r"(?i)password\s*=\s*['\"][^'\"$]{6,}['\"]", text)
    assert not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text)


def test_service_command_is_loopback_single_worker_and_uses_the_venv():
    text = (DEPLOY / "ZaZaService.ps1").read_text(encoding="utf-8")
    module = (DEPLOY / "ZaZaProduction.psm1").read_text(encoding="utf-8")
    assert "serve --host $($p.BindHost) --port $($p.Port)" in text
    assert "BindHost    = '127.0.0.1'" in module and "Port        = 8100" in module
    assert "Python      = Join-Path $script:Root 'venv\\Scripts\\python.exe'" in module
    assert "Account     = 'NT SERVICE\\ZaZaEmployeeManagement'" in module
    assert "--workers" not in text and "0.0.0.0" not in text
    assert "Nssm        = 'C:\\tools\\nssm\\nssm.exe'" in module  # the server's existing NSSM


def test_jobs_run_sheets_only_after_attendance_and_never_overlap():
    text = (DEPLOY / "Invoke-ZaZaJobs.ps1").read_text(encoding="utf-8")
    assert "'recalculate', '--recent-days'" in text and "[int]$RecentDays = 7" in text
    assert "$Job -eq 'AttendanceThenSheets' -and $ok" in text
    assert "'OpenOrCreate', 'ReadWrite', 'None'" in text and "exit 75" in text
    assert "ZAZA_DB_USER          = 'zaza_employee_report'" in text
    assert "taskkill.exe /PID $proc.Id /T /F" in text


# ─── configuration template ────────────────────────────────────────────────


def test_env_template_has_no_secrets_and_production_values():
    values = read_env_file(TEMPLATE)
    assert "ZAZA_DB_PASSWORD" not in values and "ZAZA_DATABASE_URL" not in values
    assert "ZAZA_GOOGLE_SHEET_ID" not in values and "ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE" not in values
    assert values["ZAZA_SERVER_BACKEND"] == "postgres"
    assert (values["ZAZA_DB_HOST"], values["ZAZA_DB_PORT"]) == ("127.0.0.1", "5432")
    assert values["ZAZA_DB_NAME"] == "zaza_employee_management" and values["ZAZA_DB_USER"] == "zaza_employee_app"
    assert values["ZAZA_DB_PASSWORD_FILE"] == r"C:\apps\ZaZaEmployeeManagement\secrets\db-app.password"
    assert int(values["ZAZA_DB_POOL_MIN"]) == 1 and int(values["ZAZA_DB_POOL_MAX"]) <= 4
    assert values["ZAZA_DASHBOARD_COOKIE_SECURE"] == "true"
    assert values["ZAZA_TRUSTED_PROXY_IPS"] == "127.0.0.1,::1"
    assert "ZAZA_PUBLIC_HOST" not in values  # pending: never an invented host name
    assert values["ZAZA_LOG_FILE"].startswith("C:\\apps\\ZaZaEmployeeManagement\\logs\\")


def test_env_template_passes_production_checks_except_the_pending_host(tmp_path):
    from deskmate.zaza_server.preflight import FAIL, check_config

    pw = tmp_path / "db-app.password"
    pw.write_text("x" * 40, encoding="ascii")
    env = {**read_env_file(TEMPLATE), "ZAZA_DB_PASSWORD_FILE": str(pw), "ZAZA_LOG_FILE": str(tmp_path / "z.log")}
    failed = [c.name for c in check_config(env, production=True) if c.status == FAIL]
    assert failed == ["public host"]


# ─── database role SQL ─────────────────────────────────────────────────────


def test_create_script_makes_unprivileged_roles_without_password_literals():
    text = (SQL / "01-create-database-and-roles.sql").read_text(encoding="utf-8")
    roles = re.findall(r"CREATE ROLE (\w+) LOGIN\s+(.*?);", text, re.DOTALL)
    assert [r[0] for r in roles] == ["zaza_employee_owner", "zaza_employee_app", "zaza_employee_report"]
    for _, options in roles:
        for flag in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
            assert flag in options
        assert re.search(r"PASSWORD :'\w+_verifier'", options)  # psql variable, never a literal
    assert not re.search(r"PASSWORD\s+'", text)
    assert "OWNER zaza_employee_owner" in text and "REVOKE ALL ON DATABASE zaza_employee_management FROM PUBLIC" in text
    assert "SET log_statement = 'none'" in text
    for word in ("ALTER ROLE", "ALTER SYSTEM", "pg_hba", "DROP ", "zazavoice"):
        assert word.lower() not in re.sub(r"--[^\n]*", "", text).lower()


def _grants(text: str, role: str) -> dict[str, set[str]]:
    """table -> privileges granted to ``role`` in the grant script."""
    found: dict[str, set[str]] = {}
    for privileges, tables in re.findall(rf"GRANT ([A-Z, ]+?) ON\s+([\w,\s]+?)\s+TO {role};", text):
        for table in (t.strip() for t in tables.split(",")):
            found.setdefault(table, set()).update(p.strip() for p in privileges.split(","))
    return found


def test_grant_script_is_least_privilege_and_covers_every_table():
    text = re.sub(r"--[^\n]*", "", (SQL / "02-grant-privileges.sql").read_text(encoding="utf-8"))
    app, report = _grants(text, "zaza_employee_app"), _grants(text, "zaza_employee_report")
    assert report == {t: {"SELECT"} for t in ("alembic_version", "employees", "activity_periods", "daily_summaries",
                                              "weekly_summaries", "monthly_summaries")}
    assert app["audit_logs"] == {"SELECT", "INSERT"} and app["alembic_version"] == {"SELECT"}
    assert app["work_schedules"] == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert all(p <= {"SELECT", "INSERT", "UPDATE"} for t, p in app.items() if t != "work_schedules")
    assert "ALL TABLES IN SCHEMA public TO" not in text  # explicit tables only
    created = set()
    for migration in (ROOT / "deskmate" / "zaza_server" / "postgres" / "migrations" / "versions").glob("*.py"):
        created |= set(re.findall(r"CREATE TABLE (\w+)", migration.read_text(encoding="utf-8")))
    assert created and created | {"alembic_version"} == set(app), "every migrated table needs an explicit grant"
    for word in ("TRUNCATE", "SUPERUSER", "CREATE", "ALTER", "DROP"):
        assert word not in text

def test_report_role_can_read_everything_sheets_sync_reads():
    from deskmate.zaza_server.sheets import queries

    sql = " ".join(v for k, v in vars(queries).items() if k.endswith("_SQL") and isinstance(v, str))
    assert not re.search(r"(?i)\b(insert|update|delete|truncate|create|alter|drop)\b", sql)
    read = set(re.findall(r"(?i)\bFROM\s+(\w+)", sql))
    granted = {"alembic_version", "employees", "activity_periods", "daily_summaries", "weekly_summaries",
               "monthly_summaries"}
    assert read <= granted
    assert "READ ONLY" in Path(queries.__file__).read_text(encoding="utf-8")


# ─── credential helper ─────────────────────────────────────────────────────


def _credentials_module():
    spec = importlib.util.spec_from_file_location("zaza_db_credentials", DEPLOY / "zaza_db_credentials.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scram_keys_match_rfc_7677():
    creds = _credentials_module()
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    stored_key, server_key = creds.scram_keys("pencil", salt, 4096)
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r={nonce}".encode()
    server_signature = hmac.new(server_key, auth, hashlib.sha256).digest()
    assert base64.b64encode(server_signature).decode() == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    client_signature = hmac.new(stored_key, auth, hashlib.sha256).digest()
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature, strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key


def test_verifier_format_and_password_strength():
    creds = _credentials_module()
    password = creds.generate_password()
    assert len(password) == 40 and set(password) <= set(creds.ALPHABET)
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}:[A-Za-z0-9+/=]{44}",
                        creds.scram_verifier(password))
    assert password not in creds.scram_verifier(password)
    with pytest.raises(ValueError):
        creds.scram_verifier("has space")


def test_credential_files_are_created_once_and_never_printed(tmp_path, capsys):
    creds = _credentials_module()
    assert creds.main(["--secrets-dir", str(tmp_path), "owner", "app", "report"]) == 0
    out = capsys.readouterr().out
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["db-app.password", "db-app.scram", "db-owner.password", "db-owner.scram",
                     "db-report.password", "db-report.scram"]
    for p in tmp_path.iterdir():
        assert p.read_text(encoding="ascii") not in out
    before = (tmp_path / "db-app.password").read_text(encoding="ascii")
    assert creds.main(["--secrets-dir", str(tmp_path), "app"]) == 1  # never overwritten
    assert (tmp_path / "db-app.password").read_text(encoding="ascii") == before
