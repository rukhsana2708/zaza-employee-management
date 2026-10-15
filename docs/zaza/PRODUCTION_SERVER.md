# ZaZa central server — production runbook (Phase 10)

> **Status: Phase 10 IN PROGRESS.** Phase 10B prepared the server runtime
> only. The PostgreSQL database and roles do **not** exist yet, Caddy has
> **not** been changed, the Windows service is **not** registered, Google
> Sheets is **not** configured, and **no employee traffic** reaches the
> server. Phase 11 (pilot) has not begun.

The server is a Windows Server 2022 VPS that already runs ZazaVoice
(backend, frontend), PostgreSQL 18, Caddy, a GitHub Actions runner and
Docker. ZaZa is **additive**: its own directory, Python, virtual
environment, database, roles, service and log files. It changes none of
the existing systems.

## 1. Layout

```
C:\Program Files\Python314\          Python 3.14 (all users; not on PATH, no file
                                     associations; Python 3.15 stays the default)
C:\tools\nssm\nssm.exe               the server's existing NSSM 2.24 (reused)
C:\apps\ZaZaEmployeeManagement\
    app\          junction -> releases\<active commit>
    releases\<commit>\               one exported, verified commit per folder:
        deskmate\...                 server code only (see §2)
        DEPLOYED_COMMIT.json         commit, time, operator, SHA-256 of every file
        requirements-server.lock.txt the hashed dependency lock of that commit
    venv\         Python 3.14 venv, server-only packages (§3)
    config\zaza-server.env           non-secret settings (§4)
    secrets\      password files (and later the Google key); does not inherit
    logs\         zaza-server.log (+5 rotated), service-std*.log, jobs.log
    run\          release history, job lock
    backup\       saved configs (does not inherit)
    scripts\      (reserved)
```

**Permissions:** the root does not inherit from `C:\apps`. Only
`Administrators` and `SYSTEM` have access, and `BUILTIN\Users` has none.
`secrets\` and `backup\` additionally block inheritance, so later grants on
the root never reach them. When the service is installed (Phase 10C) its
virtual account gets these rights:

- read + execute on the root;
- modify on `logs\` and `run\`;
- read on `secrets\db-app.password` only (plus the Google key file once
  configured).

## 2. Deploying an exact commit

The deployed code is always one exact Git commit, never the
administrator's working tree.

```powershell
cd C:\Users\Administrator\Documents\Projects\zaza-employee-management
git fetch origin
$c = git rev-parse origin/zaza-development
.\deploy\windows\Deploy-ZaZaServer.ps1 -Commit $c -DryRun     # list files, run checks
.\deploy\windows\Deploy-ZaZaServer.ps1 -Commit $c -Activate   # export + verify + switch `app`
```

The script works like this:

- It runs `git archive` of the commit, limited to
  `deploy/windows/server-files.txt` as stored in that commit. Because the
  files come from the commit and not the working tree, `.git`, `.env`
  files, secrets, caches and untracked files cannot be deployed.
- The export contains only `deskmate/zaza_server/**`, the shared wire
  protocol `deskmate/zaza/sync/protocol.py` (with its package `__init__`
  files) and `LICENSE`. The employee agent, DeskMate capture code, the
  installer and the archived zip snapshots are excluded.
- It refuses forbidden names: `.git*`, `.env`, `*.env`, `__pycache__`,
  `.claude*`, `secret(s)`, and the extensions `*.zip`, `*.json`, `*.pem`,
  `*.key`, `*.db`, `*.log`, `*.jsonl`, `*.output`.
- It writes `DEPLOYED_COMMIT.json`, then runs an import check with the
  production venv.
- `-Activate` switches the `app` junction. The previous release stays in
  `releases\`.
- It never starts or stops the service and never touches the database.

Rollback:

```powershell
.\deploy\windows\Rollback-ZaZaServer.ps1 -DryRun
.\deploy\windows\Rollback-ZaZaServer.ps1 -Restart     # previous release
```

The server refuses to start unless the database schema is exactly the
release's migration head. So `Rollback-ZaZaServer.ps1` refuses to cross a
migration unless the database has first been restored from the
pre-migration backup (`-AllowSchemaMismatch`).

## 3. Server-only dependencies

`deploy/windows/requirements-server.txt` lists what the server imports. A
test derives this from the code. The list is:

| Area | Packages |
|---|---|
| Core | pydantic, fastapi, uvicorn (plain, no `[standard]`), tzdata |
| PostgreSQL | psycopg[binary], psycopg-pool, SQLAlchemy, alembic |
| Dashboard | jinja2, argon2-cffi |
| Sheets | google-api-python-client, google-auth, google-auth-httplib2, httplib2 |

Production installs the exact pins with SHA-256 hashes from
`requirements-server.lock.txt`, using
`--require-hashes --only-binary=:all: --no-deps`. That lock currently holds
44 packages, resolved on Python 3.14.8 for Windows x64.

None of the employee-agent packages are present: mss, Pillow, pywin32,
comtypes, uiautomation, WinRT, OCR, audio, psutil, numpy and PyInstaller.
Tests fail if any of them appears, even as a dependency of a dependency.

## 4. Configuration and secrets

`config\zaza-server.env` is installed from
`deploy/windows/zaza-server.env.template` by `Install-ZaZaConfig.ps1`. It
holds `ZAZA_*` settings only and no secrets:

| Area | Settings |
|---|---|
| Database | `ZAZA_SERVER_BACKEND=postgres`, `ZAZA_DB_HOST=127.0.0.1`, `ZAZA_DB_PORT=5432`, `ZAZA_DB_NAME=zaza_employee_management`, `ZAZA_DB_USER=zaza_employee_app` |
| Database password | `ZAZA_DB_PASSWORD_FILE=C:\apps\ZaZaEmployeeManagement\secrets\db-app.password` (no password variable) |
| Pool | `ZAZA_DB_POOL_MIN=1`, `ZAZA_DB_POOL_MAX=3` (the server has about 6 GB of RAM, shared) |
| Dashboard | `ZAZA_DASHBOARD_COOKIE_SECURE=true`, login rate-limit settings (§6) |
| Proxy | `ZAZA_TRUSTED_PROXY_IPS=127.0.0.1,::1` |
| Logging | `ZAZA_LOG_FILE`, `ZAZA_LOG_MAX_BYTES`, `ZAZA_LOG_BACKUP_COUNT` |

**Pending until Phase 10C:**

- `ZAZA_PUBLIC_HOST`, the ZaZa host name;
- `ZAZA_GOOGLE_SHEET_ID` and `ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE`.

Every command reads this file with `--env-file`. A variable already set in
the process wins, which lets the migration and report jobs use their own
role. To validate the configuration without printing secrets, run:

```powershell
cd C:\apps\ZaZaEmployeeManagement\app
..\venv\Scripts\python.exe -E -s -m deskmate.zaza_server --env-file ..\config\zaza-server.env check-config --production
```

**Secrets** are files in `secrets\`, which only Administrators and SYSTEM
can read. No placeholder secret files exist, so nothing can accidentally
start with a fake value.

- `db-owner.password`: migrations only. Never readable by the service.
- `db-app.password`: the API and the attendance job.
- `db-report.password`: the Sheets job.
- `google-service-account.json`: Phase 10C.

`Initialize-ZaZaDatabase.ps1` generates these as 40-character random
passwords (`zaza_db_credentials.py`, Python standard library) together
with SCRAM-SHA-256 verifiers. The roles are created from the verifiers, so
a plaintext password never appears in SQL text, on a command line, in the
PostgreSQL log or on screen.

## 5. Service (Phase 10C — prepared, not registered)

`ZaZaService.ps1 -Action Plan` prints the definition:

```
Service   ZaZaEmployeeManagement (NSSM), automatic start, account NT SERVICE\ZaZaEmployeeManagement
Command   "C:\apps\ZaZaEmployeeManagement\venv\Scripts\python.exe" -E -s -u -m deskmate.zaza_server
          --env-file "C:\apps\ZaZaEmployeeManagement\config\zaza-server.env" --backend postgres
          serve --host 127.0.0.1 --port 8100
Directory C:\apps\ZaZaEmployeeManagement\app
Output    logs\service-stdout.log / service-stderr.log (NSSM online rotation at 10 MiB)
```

There is **one process and one worker**: the login rate limiter is in
memory, and RAM is limited. uvicorn's own proxy-header handling is off
(`proxy_headers=False`); see §6.

**Service identity.** `LocalService` would work for every requirement:

- read the app files;
- write logs;
- reach PostgreSQL on 127.0.0.1 with a password;
- make outbound HTTPS calls to Google.

But `LocalService` is a single account shared by many Windows services. Any
file readable by `LocalService` (the database password file, the Google
key) would be readable by all of them.

The service instead runs as its **virtual service account**,
`NT SERVICE\ZaZaEmployeeManagement`. Like `LocalService`, it has no
password and no Windows user to manage, and its network identity is the
same. Unlike `LocalService`, it has its own SID, so the secret files can be
granted to ZaZa alone. No Windows user is created.

`-Action Install` refuses unless:

- a release is active;
- `check-config --production` passes;
- `db-status` shows the current schema;
- port 8100 is free.

## 6. Reverse proxy, HTTPS, Host check, login rate limiting

Caddy (Phase 10C) will proxy `https://<ZAZA_PUBLIC_HOST>` to
`127.0.0.1:8100`. The application side is ready.

- **Trusted proxy** (`http_security.py`): `X-Forwarded-For` and
  `X-Forwarded-Proto` are honoured only when the TCP peer is a loopback
  address listed in `ZAZA_TRUSTED_PROXY_IPS`.
  - Non-loopback values are refused at start-up.
  - From any other peer the headers are ignored, so a client cannot fake
    its address or scheme.
  - The client address is the right-most forwarded entry that is not the
    proxy.
- **HTTPS:** a request that the proxy marks `https` is handled as https.
  The dashboard's same-origin check then matches the browser's `https://`
  Origin. Session cookies are `Secure`, and `serve` refuses to start with a
  public host name but non-Secure cookies.
- **Host check:** with `ZAZA_PUBLIC_HOST` set, any other `Host` gets 400.
  `127.0.0.1`, `localhost` and `::1` are accepted only for direct local
  requests (no `X-Forwarded-For`), for health checks on the server. Without
  `ZAZA_PUBLIC_HOST` (local development) there is no Host check.
- **Login rate limiting** (`dashboard/ratelimit.py`):
  - **Limits:** failed sign-ins are counted per real client address
    (default 10) and per submitted username (default 20) in a sliding
    15-minute window. The username count is kept even for names that don't
    exist, so the limit reveals nothing about which accounts exist.
  - **When a limit is reached:** `POST /manager/login` answers **429** with
    `Retry-After` *before* any password check.
  - **Messages:** the response keeps the generic message.
  - **Reset:** a successful sign-in clears both counts.
  - **Memory:** the state is in memory and bounded (10,000 keys, expired
    entries dropped).
  - **Logging:** passwords and usernames are never logged; only the
    client address is logged when a limit is reached.

## 7. Logging

| Log | Size bound |
|---|---|
| `logs\zaza-server.log` | Python `RotatingFileHandler`: 10 MiB × (1 + 5 kept) = 60 MiB at most |
| `logs\service-stdout.log`, `service-stderr.log` | NSSM rotates online at 10 MiB; `Invoke-ZaZaJobs.ps1` keeps the newest 5 rotated files of each |
| `logs\jobs.log` | 5 MiB × 5 |

Every server log line passes a redacting formatter. It masks:

- `Authorization` and `Cookie` values;
- bearer tokens;
- `password=`/`token=`/`secret=`-style fields;
- credentials in database URLs;
- PEM private keys;
- Google `ya29.` tokens;
- the database password itself.

The application already avoids logging these; the formatter is the safety
net.

## 8. Database (Phase 10C — prepared, not run)

| Role | Purpose | Privileges |
|---|---|---|
| `zaza_employee_owner` | owns the database and every ZaZa object; runs `migrate` only | `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, 3 connections |
| `zaza_employee_app` | API, dashboard, attendance job | explicit SELECT/INSERT/UPDATE on the ZaZa tables; DELETE on `work_schedules` only; `audit_logs` SELECT/INSERT; `alembic_version` SELECT; 10 connections |
| `zaza_employee_report` | Google Sheets export | SELECT on exactly `alembic_version`, `employees`, `activity_periods`, `daily/weekly/monthly_summaries`; 3 connections |

`sheets-sync` performs **no PostgreSQL writes**. It reads in one
`REPEATABLE READ, READ ONLY` transaction and takes a session advisory lock,
which needs no table privilege. So the read-only role is enough.

Scripts for review (`deploy/postgres/`):

- `01-create-database-and-roles.sql`, run as `postgres`;
- `02-grant-privileges.sql`, run as the owner after `migrate`.

`Initialize-ZaZaDatabase.ps1` runs them in this order:

1. Credentials
2. CreateDatabase
3. Migrate
4. Grant
5. Cleanup

psql asks for the `postgres` password interactively; it is never stored.
The scripts do not alter any existing role or database, `pg_hba.conf` or
`postgresql.conf`.

**Migrations:** Alembic head is `0004` (`0001` initial schema, `0002`
attendance summaries, `0003` schedule time zone, `0004` manager dashboard
auth). Production migration command, run as the owner role:

```powershell
Import-Module .\deploy\windows\ZaZaProduction.psm1
Invoke-ZaZaCli -Arguments migrate -Environment @{ ZAZA_DB_USER='zaza_employee_owner';
    ZAZA_DB_PASSWORD_FILE='C:\apps\ZaZaEmployeeManagement\secrets\db-owner.password' }
```

## 9. Recurring jobs (Phase 10C — prepared, not scheduled)

`Invoke-ZaZaJobs.ps1` (default `-Job AttendanceThenSheets`) runs two
commands in order:

1. `recalculate --recent-days 7` as `zaza_employee_app`.
2. `sheets-sync` as `zaza_employee_report`, **only if step 1 succeeded**.

Why 7 days: each employee's own last 7 local dates are recalculated,
including today and whole affected weeks and months. That covers
time-zone differences, overnight shifts (a shift belongs to the date it
starts) and devices that upload late after a few days offline.

Agents keep unsynced data indefinitely. Data uploaded after more than 7
days is picked up by a manual `recalculate --from/--to`, or by an optional
weekly `-RecentDays 35` run.

| Property | Behaviour |
|---|---|
| Overlap | An exclusive lock file `run\jobs.lock` covers the whole run. A second copy exits with code 75. `sheets-sync` also holds its own PostgreSQL advisory lock. |
| Timeout | Each step is killed, with its process tree, after 20 minutes (`-TimeoutMinutes`). |
| Log | `logs\jobs.log` |
| Account | Planned: a scheduled task as `SYSTEM`, which already has access to `secrets\`. A per-task service SID can replace it later. |
| Schedule | To be decided in 10C, e.g. every 30–60 minutes. |

## 10. Manager bootstrap (Phase 10C — not done)

The command, run after the database exists:

```powershell
cd C:\apps\ZaZaEmployeeManagement\app
..\venv\Scripts\python.exe -E -s -m deskmate.zaza_server --env-file ..\config\zaza-server.env `
    manager-create --username <name> --name "<Display Name>" --role ADMIN
```

The password is typed twice at a hidden `getpass` prompt: at least 12
characters, and it must not contain the username. It is never passed as an
argument, put in a script or environment variable, or committed.

## 11. Checks

```powershell
.\deploy\windows\Test-ZaZaServer.ps1 -Check Config    # production rules
.\deploy\windows\Test-ZaZaServer.ps1 -Check Schema    # db-status
.\deploy\windows\Test-ZaZaServer.ps1 -Check Port      # 8100: loopback only, owned by the service
.\deploy\windows\Test-ZaZaServer.ps1 -Check Health    # GET http://127.0.0.1:8100/api/v1/sync/health
```

## 12. Phase 10C steps (not started)

1. **Database.** Provide the PostgreSQL administrative password
   interactively. Then run `Initialize-ZaZaDatabase.ps1`: credentials →
   create database and roles → migrate → grant → cleanup.
2. **Host name.** Choose the ZaZa host name and create its DNS record.
   Decide on Cloudflare (proxied or DNS-only), then set `ZAZA_PUBLIC_HOST`.
3. **Service.** Run `ZaZaService.ps1 -Action Install`, then `-Action Start`.
   Check the health endpoint locally.
4. **Caddy.** Add a new, separate site block for the ZaZa host that
   proxies to `127.0.0.1:8100`. Validate it, then reload. Existing blocks
   are untouched.
5. **Manager account.** Run `manager-create` (§10).
6. **Google Sheets.** Put the service-account key in `secrets\`, set the
   sheet ID, run `sheets-init`, and grant the key file to the jobs account.
7. **Jobs.** Schedule `Invoke-ZaZaJobs.ps1`.
8. **Backups.** Schedule a `pg_dump` of `zaza_employee_management`.
9. **End-to-end check.** Then hand over for the Phase 11 pilot.
