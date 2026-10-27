"""Phase 10B: the Google Cloud Run container of the central server
(``deploy/gcp/``). Static checks of the Dockerfile, build-context policy,
entrypoint and Linux dependency lock, plus the entrypoint's behaviour with a
stand-in ``python``. The image itself is built and smoke-tested with Docker
(see deploy/gcp/README.md); nothing here needs Docker or Google Cloud."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from deskmate.zaza_server.preflight import FAIL, check_config

from .test_production_deploy import (
    PROVIDERS,
    _file_list,
    _is_prohibited,
    _norm,
    _requirement_names,
    _third_party_imports,
)

ROOT = Path(__file__).resolve().parents[2]
GCP = ROOT / "deploy" / "gcp"
WINDOWS = ROOT / "deploy" / "windows"
DOCKERFILE = GCP / "Dockerfile"
IGNORE = GCP / "Dockerfile.dockerignore"
ENTRYPOINT = GCP / "entrypoint.sh"
REQUIREMENTS = GCP / "requirements-server.txt"
LOCK = GCP / "requirements-server.lock.txt"
README = GCP / "README.md"

SERVER_CLOSURE = {"LICENSE", "deskmate/__init__.py", "deskmate/zaza/__init__.py", "deskmate/zaza/sync/__init__.py",
                  "deskmate/zaza/sync/protocol.py", "deskmate/zaza_server"}
BUILD_INPUTS = {"deploy/gcp/requirements-server.lock.txt", "deploy/gcp/entrypoint.sh"}


def _instructions() -> list[str]:
    """Dockerfile instructions, continuation lines joined, comments dropped."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _copies() -> list[list[str]]:
    """Arguments of every COPY (flags removed)."""
    out = []
    for line in _instructions():
        if line.upper().startswith("COPY "):
            out.append([a for a in line.split()[1:] if not a.startswith("--")])
    return out


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.strip().startswith("#"))


# ─── source allowlist ──────────────────────────────────────────────────────


def test_dockerfile_copies_only_the_server_closure_and_its_build_inputs():
    sources = {args[0] for args in _copies() if not any(a.startswith("/") for a in args[:-1])}
    from_stage = [args for args in _copies() if args[0].startswith("/")]  # COPY --from=deps /opt/venv
    assert sources == SERVER_CLOSURE | BUILD_INPUTS
    assert from_stage == [["/opt/venv", "/opt/venv"]]


def test_container_closure_matches_the_reviewed_server_file_list():
    includes, _ = _file_list()
    assert set(includes) == SERVER_CLOSURE


def test_dockerfile_never_copies_the_whole_repository():
    for args in _copies():
        assert args[0] not in (".", "./", "*", "/") and not args[0].startswith(("..", "deploy/windows")), args
    upper = [line.upper() for line in _instructions()]
    assert not [line for line in upper if line.startswith("ADD ")], "ADD can fetch URLs / unpack archives"


def test_build_context_is_an_allowlist_that_excludes_secrets():
    lines = [line.strip() for line in IGNORE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert lines[0] == "*", "everything must be excluded first"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == SERVER_CLOSURE | BUILD_INPUTS
    never = set(lines) - {"*"} - {f"!{a}" for a in allowed}
    for pattern in ("**/__pycache__", "**/*.pyc", "**/*.zip", "**/.git*", "**/.env", "**/*.env", "**/.env.*",
                    "**/secrets", "**/*.json", "**/*.pem", "**/*.key", "**/*.db", "**/*.sqlite3", "**/*.log",
                    "**/.claude*", "**/.pytest_cache"):
        assert pattern in never, pattern
    # the re-exclusions come AFTER the allowlist, so they win inside allowed paths
    assert max(lines.index(f"!{a}") for a in allowed) < min(lines.index(p) for p in never)


def test_no_secret_or_env_file_is_copied_or_baked_in():
    text = _code(DOCKERFILE)
    for args in _copies():
        for src in args[:-1]:
            assert not re.search(r"(?i)(\.env|secret|password|credential|\.json$|\.pem$|\.key$|\.db$)", src), src
    for line in _instructions():
        if line.upper().startswith(("ENV ", "ARG ")):
            assert not re.search(r"(?i)(password|secret|token|api_key|private_key|credential|_URL=)", line), line
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in text and "ZAZA_DB_PASSWORD" not in text


# ─── image settings ────────────────────────────────────────────────────────


def test_base_image_is_a_pinned_python_314_slim_debian_linux_image():
    arg = re.search(r"^ARG BASE_IMAGE=(\S+)$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE).group(1)
    assert re.fullmatch(r"python:3\.14\.\d+-slim-(trixie|bookworm)@sha256:[0-9a-f]{64}", arg), arg
    assert all(line.split()[1] == "${BASE_IMAGE}" for line in _instructions() if line.upper().startswith("FROM "))
    assert "latest" not in arg and "windowsservercore" not in DOCKERFILE.read_text(encoding="utf-8").lower()


def test_runtime_runs_as_a_non_root_user_without_pip():
    lines = _instructions()
    user = [line for line in lines if line.upper().startswith("USER ")]
    assert user and user[-1] == "USER 10001:10001"
    assert lines.index(user[-1]) > max(i for i, line in enumerate(lines) if line.upper().startswith(("COPY", "RUN")))
    text = _code(DOCKERFILE)
    assert "--require-hashes --only-binary=:all: --no-deps" in text
    assert text.count("pip uninstall --yes pip") == 2  # the venv's pip and the base image's pip
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in text and not [ln for ln in lines if ln.upper().startswith("CMD")]


def test_container_files_keep_lf_line_endings():
    assert "deploy/gcp/** text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for path in (DOCKERFILE, ENTRYPOINT, IGNORE, LOCK, REQUIREMENTS, GCP / "lock-requirements.sh"):
        assert b"\r" not in path.read_bytes(), path.name


# ─── entrypoint ────────────────────────────────────────────────────────────


def test_entrypoint_execs_the_server_on_all_interfaces_and_port():
    code = _code(ENTRYPOINT)
    assert code.startswith("#!/bin/sh") or ENTRYPOINT.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert "set -eu" in code
    assert 'PORT="${PORT:-8080}"' in code
    assert code.rstrip().endswith(
        'exec python -E -s -u -m deskmate.zaza_server serve --host 0.0.0.0 --port "$PORT"')
    assert "127.0.0.1" not in code and "localhost" not in code


def test_entrypoint_starts_nothing_but_the_web_server():
    code = _code(ENTRYPOINT)
    for word in ("migrate", "manager-create", "add-employee", "register-device", "recalculate", "attendance",
                 "sheets", "sheets-refresh", "alembic", "--env-file", "ZAZA_LOG_FILE", "pip ", "curl", "wget"):
        assert word not in code.replace("ZAZA_SERVER_BACKEND", ""), word
    assert code.count("python ") == 1  # the single exec


def _run_entrypoint(tmp_path: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX sh available")
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    script = tmp_path / "entrypoint.sh"
    script.write_bytes(ENTRYPOINT.read_bytes().replace(b"cd /app", b"cd \"$ZAZA_TEST_APP\""))
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "python").write_bytes(b'#!/bin/sh\necho "python $*"\n')
    os.chmod(fake / "python", 0o755)
    sh_dir = str(Path(sh).parent)
    full = {"PATH": os.pathsep.join([str(fake), sh_dir, "/usr/bin", "/bin"]), "ZAZA_TEST_APP": str(app), **env}
    return subprocess.run([sh, str(script), *args], capture_output=True, text=True, env=full, check=False)


SQLITE_OK = {"ZAZA_SERVER_BACKEND": "sqlite", "ZAZA_CONTAINER_ALLOW_SQLITE": "local-test-only"}


@pytest.mark.parametrize(("env", "port"), [
    (SQLITE_OK, "8080"),                                   # Cloud Run default when PORT is unset
    ({**SQLITE_OK, "PORT": "9090"}, "9090"),
    ({"ZAZA_SERVER_BACKEND": "postgres", "PORT": "8080"}, "8080"),
])
def test_entrypoint_honours_port(tmp_path, env, port):
    result = _run_entrypoint(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"python -E -s -u -m deskmate.zaza_server serve --host 0.0.0.0 --port {port}"


@pytest.mark.parametrize(("env", "args"), [
    ({}, ()),                                              # backend must be explicit
    ({"ZAZA_SERVER_BACKEND": "sqlite"}, ()),               # SQLite only with the local-test opt-in
    ({"ZAZA_SERVER_BACKEND": "sqlite", "ZAZA_CONTAINER_ALLOW_SQLITE": "yes"}, ()),
    ({"ZAZA_SERVER_BACKEND": "mysql"}, ()),
    ({**SQLITE_OK, "PORT": "80x"}, ()),
    ({**SQLITE_OK, "PORT": "70000"}, ()),
    ({**SQLITE_OK, "PORT": "0"}, ()),
    (SQLITE_OK, ("migrate",)),                             # no other command through this image
])
def test_entrypoint_refuses_unsafe_starts(tmp_path, env, args):
    result = _run_entrypoint(tmp_path, env, *args)
    assert result.returncode == 64 and "python" not in result.stdout, (result.stdout, result.stderr)


# ─── Linux dependencies ────────────────────────────────────────────────────


def _lock() -> dict[str, tuple[str, list[str], list[str]]]:
    """name -> (version, sha256 hashes, wheel file names)."""
    entries: dict[str, tuple[str, list[str], list[str]]] = {}
    current = None
    for line in LOCK.read_text(encoding="ascii").splitlines():
        if m := re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", line):
            current = _norm(m.group(1))
            entries[current] = (m.group(2), [], [])
        elif m := re.match(r"^\s+--hash=sha256:([0-9a-f]{64})", line):
            entries[current][1].append(m.group(1))
        elif m := re.match(r"^\s+#\s+(\S+\.whl)$", line):
            entries[current][2].append(m.group(1))
    return entries


def test_cloud_run_requirements_are_the_same_server_only_set():
    names = _requirement_names(REQUIREMENTS)
    assert names == _requirement_names(WINDOWS / "requirements-server.txt")
    assert not {n for n in names if _is_prohibited(n)}
    imports = _third_party_imports()
    assert {_norm(PROVIDERS[m]) for m in imports} <= names
    assert names <= {_norm(PROVIDERS[m]) for m in imports} | {"tzdata"}  # not padded


def test_linux_lock_pins_every_package_with_a_linux_wheel_hash():
    lock = _lock()
    assert _requirement_names(REQUIREMENTS) - {"psycopg"} <= set(lock)
    assert {"psycopg", "psycopg-binary"} <= set(lock)
    assert not {n for n in lock if _is_prohibited(n)}
    for name, (version, hashes, wheels) in lock.items():
        assert len(hashes) == 1 and len(wheels) == 1, name
        wheel = wheels[0]
        assert wheel.endswith(("-none-any.whl", "x86_64.whl")), wheel  # pure Python or Linux x86-64
        assert "win" not in wheel.split("-")[-1] and "macosx" not in wheel, wheel
        if not wheel.endswith("-none-any.whl"):
            assert "manylinux" in wheel, wheel
    header = LOCK.read_text(encoding="ascii")[:400]
    assert "Linux x86_64" in header and "--require-hashes --only-binary=:all: --no-deps" in header


def test_linux_lock_is_not_a_copy_of_the_windows_lock():
    windows = (WINDOWS / "requirements-server.lock.txt").read_text(encoding="ascii")
    assert "win_amd64" in windows.splitlines()[1]
    assert set(_lock()) == {_norm(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==", windows, re.MULTILINE)}
    assert "win_amd64" not in LOCK.read_text(encoding="ascii")


# ─── the Windows deployment and its production checks stay as they were ───


def test_windows_deployment_is_intact():
    for name in ("Deploy-ZaZaServer.ps1", "Rollback-ZaZaServer.ps1", "ZaZaService.ps1", "Test-ZaZaServer.ps1",
                 "Invoke-ZaZaJobs.ps1", "Initialize-ZaZaDatabase.ps1", "Install-ZaZaConfig.ps1",
                 "ZaZaProduction.psm1", "README.md", "server-files.txt", "requirements-server.txt",
                 "requirements-server.lock.txt", "zaza-server.env.template", "zaza_db_credentials.py"):
        assert (WINDOWS / name).is_file(), name
    assert "BindHost    = '127.0.0.1'" in (WINDOWS / "ZaZaProduction.psm1").read_text(encoding="utf-8")


def test_production_preflight_still_requires_a_log_file():
    """Cloud Run logs to stderr, but the existing --production rules (written
    for the Windows server) are not weakened for it."""
    checks = {c.name: c for c in check_config({"ZAZA_SERVER_BACKEND": "sqlite"}, production=True)}
    assert checks["log file"].status == FAIL
    assert checks["backend"].status == FAIL


def test_http_security_stays_fail_closed():
    """Cloud Run's front end is not on loopback; forwarded headers from it are
    still NOT trusted, and non-loopback trusted proxies are still refused
    (a Cloud Run-specific design is a later phase - see README)."""
    from deskmate.zaza_server.config import ConfigError
    from deskmate.zaza_server.http_security import http_settings_from_env

    assert http_settings_from_env({}).trusted_proxies == ("127.0.0.1", "::1")
    with pytest.raises(ConfigError):
        http_settings_from_env({"ZAZA_TRUSTED_PROXY_IPS": "169.254.1.1"})


# ─── documentation ─────────────────────────────────────────────────────────


def test_readme_documents_scope_and_contains_no_secrets():
    text = README.read_text(encoding="utf-8")
    for needle in ("zaza-employee-monitor", "asia-south1", "Cloud SQL", "NOT been deployed", "PORT", "8080",
                   "0.0.0.0", "migrate", "Dockerfile.dockerignore", "lock-requirements.sh"):
        assert needle in text, needle
    assert not re.search(r"zzd_[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AIza[0-9A-Za-z_-]{30,}", text)
    assert not re.search(r"\b[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b", text)  # billing account id
    assert not re.search(r"postgres(ql)?://[^\s:/]+:[^\s@]+@", text)  # no connection string with a password
