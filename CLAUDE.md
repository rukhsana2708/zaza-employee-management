# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ZaZa Employee Management: transparent, activity-*metadata* monitoring of company Windows machines, aggregated centrally and reported to managers. It is mid-pivot to a multi-tenant SaaS on Google Cloud (project `zaza-employee-monitor`, region `asia-south1`, Cloud Run + Cloud SQL, database-per-tenant).

The repo is a fork of the open-source **DeskMate** (MIT). The Python package is still named `deskmate` (see `pyproject.toml`). **Most of `deskmate/` (a11y, audio, capture, db, apps, ...) and most of `tests/*.py` is upstream DeskMate code that ZaZa does not use or import.** ZaZa code lives only in:

- `deskmate/zaza/`: Windows activity agent (SQLite local queue + sync client)
- `deskmate/zaza_server/`: central FastAPI sync API, PostgreSQL repos, attendance, Sheets, dashboard, SaaS tenancy
- `tests/zaza_agent/`: the ZaZa test suite
- `installer/` (PyInstaller + Inno Setup), `deploy/gcp/` (Cloud Run image + Terraform), `docs/zaza/`

## Hard privacy rule

The system tracks activity, never content. No screenshots, OCR, keystroke contents, clipboard, audio or video: not behind a flag, simply not implemented. Never import DeskMate's capture, OCR, audio or clipboard modules from `deskmate/zaza*`. The installer build runs a package audit that fails the build if forbidden code ships (`installer/audit_package.py`). See `SECURITY.md`.

Terminology (UI, Sheets, docs): never say "Actual Work Hours". Use "Active Hours", "Computer Active" and "No Recent Input". There is no composite productivity score. Unreliable or stale signals become `UNKNOWN` (or a connection state), never `IDLE`. `ACTIVE` means observed computer activity, not proof of productivity. Any future AI task inference is suggestion-only, uses privacy-filtered metadata, and the employee confirms it.

## Product direction

The long-term product is a remote-work operating system (Live Team, Tasks, Teams, Clients/CRM, Messages, Reports, Settings). Monitoring is context for work management, not the whole product. The untracked `zaza_conversation_archive.md` is a ChatGPT-era history of the Phase 10 decisions, the phase verdict ledger and the security invariants. Its ledger lags the repo: 10G-E-A is already implemented here and pending review.

Intended rollout order after 10G-E-A review:
1. 10G-E-B: deploy the reviewed operator image to the `zaza-migrate` job.
2. Create the internal SaaS user with `--no-password`, add an OWNER membership to `zaza-internal`, and create the first tenant employee through the tenant-aware operator.
3. Build and review the public edge: external Application Load Balancer, Cloud Armor, serverless NEG, Cloud Run ingress restricted to the load balancer.
4. Deploy the web service.
5. Only then activate the login password, issue the first enrollment code, enroll the internal device and start the internal pilot.

## Commands

Windows / PowerShell, venv at `.venv`:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,zaza-postgres,zaza-sheets,zaza-dashboard]"

pytest tests/zaza_agent -q                                  # ZaZa suite
pytest tests/zaza_agent/test_attendance.py::test_name -q    # single test
ruff check deskmate/zaza deskmate/zaza_server tests/zaza_agent   # line-length 100, py310

python -m deskmate.zaza                  # run the agent locally (SQLite in ~/.zaza_agent, ZAZA_HOME overrides)
python -m deskmate.zaza --report         # show stored sessions
python -m deskmate.zaza_server serve     # central server
python -m deskmate.zaza_server --help    # operator CLI: migrate, control-migrate, tenant-migrate,
                                         # saas-*-create, enrollment-code-*, recalculate, sheets-sync, ...
installer\build.ps1                      # build ZaZaWorkAgentSetup.exe (needs Inno Setup; -Flavor development -SkipTests)
```

PostgreSQL tests are opt-in. Without `ZAZA_TEST_POSTGRES_URL` they are skipped. Point it at a disposable database whose name contains `test` (tests create and drop their own `zaza_pytest*` schemas):

```powershell
$env:ZAZA_TEST_POSTGRES_URL="postgresql://user:password@127.0.0.1:5432/zaza_test"
pytest tests/zaza_agent -q
```

Server configuration is environment-only (`ZAZA_*`; see `.env.example`).

## Architecture (read `ARCHITECTURE.md` for detail)

Data flow: **agent (SQLite queue) -> sync API -> PostgreSQL (sole source of truth) -> Google Sheets (one-way, read-only) and manager dashboard.**

- **Agent** (`deskmate/zaza/`): a tick-based `agent.py` derives a status (ACTIVE / IDLE / LOCKED / UNKNOWN) from input hooks, the session-lock watcher and the foreground window. Idle is only inferred while the keyboard hook, mouse hook and lock state are all trusted (`health.py`). A failed hook yields `UNKNOWN`, never `IDLE`. The idle threshold is a grace period. Only summarized activity periods sync (`sync/`, wire protocol in `sync/protocol.py`, shared with the server). Raw events never leave the machine. Records are marked synced only after a server ack. Sync is idempotent (device id + sequence).
- **Server** (`deskmate/zaza_server/`): `app.py` has `create_app(repo)` (single-database path) and `tenancy/saas_web.py` has `create_saas_app(runtime)` (SaaS path). In the SaaS path a device credential is resolved through the control plane to a tenant, then authenticated inside that tenant's database. Repositories sit behind the interface in `repository.py`: in-memory, SQLite (development) and `postgres/` (psycopg3 + pool, Alembic migrations in `postgres/migrations`). Deterministic attendance and work-time calculations are in `attendance/`. They are labeled **Active Hours**, never "work time", and there is no AI and no productivity score.
- **Tenancy** (`tenancy/`): the shared **control plane** database (`zaza_control`, migrations `c000N`) holds organizations, SaaS users, memberships, the tenant directory, enrollment codes and device routes. Each customer gets its own `zaza_t_*` database using the tenant schema (migrations `0001`-`0004`). Tenant pools are bounded and lazy. `tenant-migrate` is explicit. Provisioning, `TenantResolver` and the immutable `TenantContext` enforce routing.
- **Legacy vs SaaS commands:** `add-employee`, `list-employees` and `register-device` use the single-database `open_repository` path and **must never be used for a SaaS organization**. Use the `saas-*` commands, which resolve the tenant from `--slug`.
- **Deployment:** `deploy/gcp/` (Dockerfile, Cloud Build, Terraform, `zaza-migrate` Cloud Run job). Passwords come from Secret Manager files. `check-config --cloud-run` validates the setup. `deploy/windows/` is the abandoned VPS path, kept for reference only. `deploy/gcp/requirements-server.lock.txt` is generated by `lock-requirements.sh`.

## Working conventions

- This project is delivered in numbered phases, each reviewed by the user. `DEVELOPMENT_STATUS.md` (newest first) records what each phase did and did not change. `README.md`'s "Project status" lists approved and pending phases. Update both when finishing a phase.
- Production Cloud SQL / GCP is live, but the web service is not deployed and there are no real users. Do not run `gcloud`, `terraform apply` or any production-touching command unless the user explicitly asks. Code in the repo may be ahead of what production runs.
- Invariants to preserve: client request values never choose a database. Credentials resolve to the organization (control metadata) first, then `TenantResolver` issues an immutable `TenantContext`. `zaza_control` stores no employee activity or business data. `zaza_app` has tenant DML only, with no DDL or ownership. `zaza_migrator` is only used behind the trusted operator job, never over HTTP. No raw enrollment code or device token is stored in the control plane. Disaster recovery restores to new instances, never over production.
- Windows-only agent code (pywin32, uiautomation) is isolated behind seams in `deskmate/zaza/`. Tests inject fakes (see `FakeClock` and `Probe` in `tests/zaza_agent/conftest.py`) rather than touching real OS hooks.
