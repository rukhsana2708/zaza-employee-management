# ZaZa Employee Management System — Security & Privacy

**Status:** Specification (Phase 0).
**Last updated:** 2026-10-06

## 1. Operating principle

This is **transparent workplace monitoring**. Employees know the agent is
installed, know what it tracks, and know what it does not track. There is no
stealth mode, no hidden process naming, and no attempt to disguise the agent
as something else.

## 2. Data categories — explicit allow/deny list

### 2.1 Collected (activity metadata only)

- Employee identity and registered device
- Work session start/end timestamps
- Active application name
- Active window title
- Browser/domain information, where technically practical: **only the
  currently active tab's domain** (e.g. `github.com`). Explicitly excluded:
  full URL paths, search/query parameters, browser history (prior tabs or
  visits), and page contents. If the domain isn't readable without deeper
  inspection, nothing is recorded for that window — the agent never falls
  back to reading more to compensate.
- Keyboard activity: **boolean presence only** — "keys were pressed in this
  interval." The actual characters/keys are never read, buffered, or stored.
- Mouse activity: **boolean presence only** — movement/click occurred in this
  interval, not coordinates-as-content or click targets' text.
- Idle status (derived from the two booleans above vs. a threshold)
- Computer lock / unlock events
- Online/offline status of the agent
- Work schedule (configured, not observed)

### 2.2 Never collected — anywhere, by any component

- Screenshots or any screen image/video capture
- Typed text / keystroke content (no keylogging)
- Passwords or credential material
- Clipboard contents
- Microphone audio
- Webcam or other video
- Full browsing history, full URL paths, search/query parameters, or page
  content (only the current tab's domain, as above)

These are not feature-flagged off — there is no code path in the agent, the
sync API, PostgreSQL, Google Sheets, or the dashboard that handles any of
them. If a future requirement wants one of these back, it requires an
explicit, separate decision and a new privacy review — it is not something a
config toggle silently re-enables.

### 2.3 Sensitive application / site exclusions (Phase 2)

Configurable, comma-separated, matched case-insensitively (`.exe` optional):

- `ZAZA_EXCLUDED_APPS` — the app name is kept, but the window title is stored
  as `Excluded / Private` and `privacy_excluded = 1`. The real foreground
  probe checks the process name **before** reading the title, so the title of
  an excluded app is never read into the agent process at all.
- `ZAZA_HIDDEN_APPS` — the app name is also stored as `Excluded Application`.
  Hiding an app implies excluding its title.
- `ZAZA_EXCLUDED_DOMAINS` — the domain (and its subdomains) is stored as
  `Excluded / Private Site` and the window title as `Excluded / Private`,
  because a browser title is the page title. No domain detection exists yet.
  When it is added, it must resolve the domain before reading the title.

A second filter pass runs on every sample right before persistence, so even
a probe that didn't pre-filter can't persist an excluded title, name, or
domain. Domains are reduced to a bare hostname before storage: a full URL
loses its path, query, fragment, port, and credentials. Window titles are
normalized and capped at 256 characters. Exclusions never trigger deeper
inspection; they only ever cause less to be read.

### 2.4 How this is enforced in the agent (`deskmate/zaza/`)

Automated tests in `tests/zaza_agent/test_no_prohibited_capture.py` fail if:

- any ZaZa module imports DeskMate's screenshot, OCR, clipboard, UIA text,
  or audio modules, or those modules get loaded while the agent runs;
- the ZaZa source mentions screenshot/OCR/clipboard/audio APIs
  (`ImageGrab`, `BitBlt`, `PrintWindow`, `mss`, `GetClipboardData`,
  `win32clipboard`, `sounddevice`, `pytesseract`, ...);
- executable code (checked via the AST, ignoring comments and docstrings)
  references keystroke/pointer content APIs: `KBDLLHOOKSTRUCT`,
  `MSLLHOOKSTRUCT`, `vkCode`, `scanCode`, `ToUnicode`, `ToUnicodeEx`,
  `ToAscii`, `GetKeyboardState`, `GetKeyState`, `GetAsyncKeyState`,
  `GetKeyNameTextW`, `MapVirtualKeyW` — including via string literals such
  as `getattr(user32, "GetAsyncKeyState")`;
- the keyboard/mouse hook callbacks use `lparam` for anything other than
  forwarding it unchanged to `CallNextHookEx`.

Storage tests (`test_storage_schema.py`) pin the exact column set of every
SQLite table. They fail if any column name looks like content (clipboard,
screenshot, image, OCR, audio, video, password, keystroke/key code, typed
text, URL, path, query, history, content/body). They also check that the
write methods reject unknown keyword arguments. The privacy-exclusion tests
scan every stored text value to confirm an excluded title, hidden app name,
or excluded domain appears nowhere.

Unless both keyboard and mouse hooks are `HEALTHY`, time is recorded as
`UNKNOWN`, not as "employee idle". A monitoring failure, restart, or gap is
never reported as an attendance fact.

### 2.5 The shipped employee package (Phase 9)

- **Shipped first-party code:** `deskmate` (the version only) and
  `deskmate.zaza.*`.
- **Never shipped:**
  - upstream DeskMate packages (`a11y`, `audio`, `capture`, `screen`,
    `engine`, `mcp`, `ui` and the rest);
  - the central server;
  - the developer CLI;
  - every screenshot, OCR, audio, webcam, UI Automation, clipboard and
    keyboard library.

  The build environment does not even contain those libraries.
- **`installer/audit_package.py` checks the built `ZaZaWorkAgent.exe`:**
  - it lists every packaged Python module from the executable's own archive
    and every file next to it;
  - it fails the build on anything prohibited, and on any
    non-standard-library module outside the justified allowlist (pydantic,
    httpx and its dependencies, certifi, psutil);
  - it scans the source of every shipped ZaZa module for calls that would
    capture the clipboard, the screen, OCR, audio, the webcam, key
    identities or typed text, accessibility-tree content or browser URLs,
    or that would disable TLS verification.

  Allowed exceptions are documented in the report: tkinter (UI; its generic
  clipboard methods are never called) and ctypes (Win32 calls, each
  checked). The report is `dist/package-audit.txt`.
- **No hidden switch:** the installed product has no option to enable a
  capture feature, pass a token or disable TLS checks.
- **Transparent installation:** it is visible in Installed apps, the Start
  menu, Task Manager and Task Scheduler. It runs as the employee with least
  privilege, opens no inbound port and runs no local server.
- **Device token:**
  - it is entered in the enrollment window or provided on standard input
    (`--enroll-stdin`), and is never a command-line argument;
  - it is verified with the server first;
  - it is stored only DPAPI-encrypted (current user);
  - it never appears in `config.json`, SQLite, `status.json`, logs or the
    UI, and is never shown again.

  Re-enrollment needs the token again. Changing to a different device ID
  needs explicit confirmation.

  Verified with the frozen build in a test VM: a byte-identical copy of one
  user's credential file in another Windows user's profile cannot be
  decrypted. That user's agent stays unenrolled and sends nothing.
- **Logs:** `%LOCALAPPDATA%\ZaZa\WorkAgent\logs\agent.log`, rotated at
  1 MB with 5 kept. A filter drops window titles, application names and
  per-tick activity lines and masks bearer tokens, so the logs never become
  a second copy of the telemetry.
- **Signing:** builds are unsigned until a real Authenticode certificate is
  available, so SmartScreen / Smart App Control may warn or block. The
  build process can sign with `signtool` (`-SignToolCommand`).
  Certificates and passwords never go in Git.

## 3. Why this boundary exists

Computer activity (app in focus, input presence, idle/lock state) is enough to
support attendance and work-time reporting. It is not enough, and is not used,
to reconstruct what an employee wrote, said, or looked at. The system reports
**Active Hours** (or, if softened, **Estimated Work Hours**) — never "actual
work time" — because activity presence is a proxy for work, not proof of it.

## 4. Data flow security

- **Agent → Central API (Phase 3):** only summarized records are sent:
  sessions, periods, idle periods, and daily app usage. Raw per-tick events
  never leave the machine. Traffic is batched, authenticated per device, and
  idempotent on `(record_type, record_id, record_version)`, so retried or
  offline-replayed batches cannot double-count hours (ARCHITECTURE.md §3.2).
  **Production traffic must use HTTPS.** The agent refuses plain HTTP to
  anything but localhost unless a development override is set explicitly.
- **Local buffering:** SQLite queue on the employee's machine holds only the
  same metadata listed in §2.1 — nothing broader than what eventually syncs
  centrally.
- **Central API → PostgreSQL (Phase 4):** the system gets its own database on
  the VPS's existing PostgreSQL 16 instance. It does not read or write any
  other application's tables, and no new PostgreSQL server or instance is
  created. Credentials come only from the environment or a password file
  (§6.2). The API connects as a least-privilege role that cannot change
  the schema or delete data. Phase 4 was built and tested locally only;
  nothing has connected to the VPS.
- **PostgreSQL → Google Sheets (Phase 6):** one-way, server-initiated, and
  read-only on the database side.
  - The export reads one `READ ONLY` transaction and never writes to
    PostgreSQL. Nothing is read back from the spreadsheet, so manual edits
    there can never reach the database.
  - It exports activity periods and the Phase 5 summaries only: no raw
    events, record payloads, content or summary hashes, tokens, device
    credentials, or internal IDs.
  - Privacy-excluded periods stay redacted exactly as stored.
  - Values are written as RAW, so text from window titles can't become a
    spreadsheet formula.
  - Access to the spreadsheet is limited to the people it is shared with
    (managers) plus the service account (§6.5).
- **PostgreSQL → Dashboard (Phase 7):** manager-authenticated, read-mostly.
  - The dashboard reads PostgreSQL directly and never Google Sheets.
  - It has no write path into activity data.
  - Its only writes are: sessions; account bookkeeping (`last_login_at`);
    schedule changes, which go through the Phase 5 schedule store, are
    validated by the database and are audited; and the audit rows
    themselves.

## 5. Isolation from the existing VPS

The production VPS already runs other applications. This system must not:

- Share a process, port, or database with existing services.
- Modify any existing VPS service's configuration.
- Open any port beyond what it explicitly needs (`127.0.0.1:8100` as the
  starting point — see ARCHITECTURE.md §3 on how/when that gets exposed
  beyond localhost).

No deployment happens until Phase 10, and even then it is additive, not
disruptive to what's already running.

**Phase 10 (IN PROGRESS) — what exists on the production server so far.**
Phase 10B prepared the following, all additive:

- Python 3.14 in `C:\Program Files\Python314`: not on PATH, no file
  associations; Python 3.15 is unchanged.
- `C:\apps\ZaZaEmployeeManagement`, with its own ACLs (§6.6).
- A server-only virtual environment.

Nothing else changed:

- **ZazaVoice:** its service, configuration and database are untouched.
- **PostgreSQL:** its configuration and existing roles are untouched; no
  ZaZa database or role has been created yet.
- **Caddy, firewall, DNS, scheduled tasks, the GitHub runner and Docker:**
  untouched.
- **The ZaZa service:** not registered, so no port is open.

## 6. Access control (eventual, built out in Phases 3–7)

### 6.1 Device authentication (Phase 3)

- **What a device holds:** a random 256-bit bearer token (`zzd_…`), plus the
  `X-ZaZa-Device-Id` header. There are no usernames or passwords for agents,
  and no employee login.
- **Server side:** only `sha256(token)` is stored. Disabled devices and
  revoked tokens get 403. Multiple active tokens per device make rotation
  possible without downtime.
- **Agent side:** the token is encrypted with Windows DPAPI (current-user
  scope) in `device_credentials.json`, separate from the activity database.
  It is never in source code, config, environment variables, URLs, logs,
  `repr()`, or SQLite.
- **Entering the token:** `set-credentials` reads it from a hidden prompt
  (or stdin), never from command-line arguments, which would end up in
  shell history.
- **Where it is sent:** the token only ever goes in the `Authorization`
  header. Log lines and error messages never include headers. Tests cover
  this on both client and server.
- **Accepted limitation:** the employee's own Windows account can decrypt
  its DPAPI-protected token. A token only allows submitting records for its
  own device and bound employee: the server rejects records for any other
  device or employee, and record IDs owned by another device.
- **Provisioning in production (Phase 9/10):**
  1. A manager registers the device on the server, which displays the token
     once.
  2. The installer, run on that PC, receives it through a hidden prompt or
     a one-time, short-lived enrollment code, and stores it via DPAPI.
  3. The token is never sent by email or chat.
  4. Rotation: issue a new token, update the agent, then
     `rotate-token --revoke-old`.
  5. A lost or retired device: `disable-device`.
- **Input validation:** the server enforces a 4 MiB byte cap (checked while
  reading), at most 500 records per batch, a strict schema with no unknown
  fields, an allow-list of record types, length limits on strings, and
  UTC-only timestamps.

### 6.2 Central database credentials and roles (Phase 4)

- **Where credentials come from:** the environment only: `ZAZA_DATABASE_URL`,
  or `ZAZA_DB_*` parts, with `ZAZA_DB_PASSWORD_FILE` preferred (see
  ARCHITECTURE.md §4.1).
  - Nothing is hard-coded. `.env` files are git-ignored, and `.env.example`
    holds placeholders only.
  - The password is excluded from `repr()`. The only loggable form is
    `postgresql://user:***@host:port/db`.
  - Driver error messages are scrubbed of the password before they are shown
    or logged. Configuration errors never echo the URL.
  - Tests cover this: unreachable server, wrong password, CLI output, and
    captured logs.
- **Migrations:** Alembic gets the settings in memory. The URL is built from
  parts, never written to `alembic.ini` or a file.
- **No plaintext device tokens:**
  - `device_tokens.token_hash` has a CHECK that accepts only a 64-character
    hex SHA-256 digest, so a raw `zzd_…` token cannot be stored even by
    mistake.
  - Tests scan every table, including `audit_logs`, for the raw token.
- **Two roles in production** (Phase 10 names: `zaza_employee_owner` and
  `zaza_employee_app`, plus the read-only `zaza_employee_report`, on the
  database `zaza_employee_management`; same grants, in
  `deploy/postgres/02-grant-privileges.sql`, §6.6):
  - `zaza_owner` owns the database and runs `migrate`.
  - `zaza_app` is used by the running API.

  The grants for `zaza_app` (rehearsed locally; the app role was refused
  DROP, DELETE, CREATE and audit UPDATE):

  ```sql
  REVOKE ALL ON DATABASE zaza FROM PUBLIC;
  GRANT CONNECT ON DATABASE zaza TO zaza_app;
  GRANT USAGE ON SCHEMA public TO zaza_app;
  GRANT SELECT, INSERT, UPDATE ON employees, devices, device_tokens, work_schedules,
        work_sessions, activity_periods, idle_periods, application_usage_daily TO zaza_app;
  GRANT SELECT, INSERT ON audit_logs TO zaza_app;
  GRANT SELECT ON alembic_version TO zaza_app;
  -- Phase 5 (migration 0002): summaries are recalculated and upserted, never deleted
  GRANT SELECT, INSERT, UPDATE ON daily_summaries, weekly_summaries, monthly_summaries TO zaza_app;
  -- Phase 7 (migration 0004): manager accounts and sessions (never deleted; revoked/disabled)
  GRANT SELECT, INSERT, UPDATE ON manager_users, manager_sessions TO zaza_app;
  -- Phase 7: the dashboard removes upcoming schedule rules (audited); nothing else is deleted
  GRANT DELETE ON work_schedules TO zaza_app;
  ```

  Re-check the grants after any migration that adds a table. Retention jobs
  (Phase 5) will need a narrowly granted DELETE.
- **Audit log:** `audit_logs` records employee creation, device
  registration, disable/enable, and token issue/revoke.
  - Each entry is written in the same transaction as the change, with actor
    type and id.
  - It is append-only: triggers block UPDATE, DELETE and TRUNCATE, even for
    the table owner, unless a trigger is explicitly disabled.
  - Audit values never include token values or hashes.
- **Connectivity versus activity:** `devices.last_seen_at` records API
  connectivity after successful authentication, at 30-second resolution. It
  is never used as, or mixed with, employee activity.

### 6.5 Google Sheets service account (Phase 6)

- **How access works:** the server authenticates as a Google **service
  account** using its JSON key file.
  - The scope is `https://www.googleapis.com/auth/spreadsheets` only, with
    no Drive access.
  - The service account can open only spreadsheets that the manager has
    explicitly shared with its email (as Editor).
  - ZaZa never creates, lists or shares spreadsheets.
- **The key file must:**
  - live **outside the repository**, e.g.
    `C:\ProgramData\ZaZa\google-service-account.json`, readable only by
    the account that runs the server;
  - be named only by `ZAZA_GOOGLE_SERVICE_ACCOUNT_FILE`.
  - `.gitignore` excludes `*service-account*.json`,
    `*service_account*.json`, `google-credentials*.json`,
    `zaza-sheets*.json`, `*-sa-key*.json` and `secrets/`.
- **The key is never exposed:**
  - It is read only to build credentials in memory.
  - It is never logged, copied into PostgreSQL, written to another file,
    or included in an exception message. The parsed key is excluded from
    `repr()`.
  - Key-file errors name the file and the problem, never its contents.
  - Google errors are reduced to a short message. A scrubber removes PEM
    keys, the key ID, OAuth access tokens and `Bearer` values, and masks
    the spreadsheet ID (`1AbC…xyz9`).
  - The original exception is not chained, so tracebacks can't carry it.
  - Tests check all of this with captured logs and CLI output.
- **Spreadsheet ID:** treated as semi-sensitive. Commands print it masked.
- **Rotation:** create a new key in Google Cloud, replace the file, delete
  the old key in Google Cloud. If the file leaks, delete that key in Google
  Cloud immediately; the spreadsheet can also be un-shared from the service
  account.
- **Database role:** `sheets-sync` reads only. In production it should
  connect as a separate SELECT-only role. Rehearse this before Phase 10:

  ```sql
  CREATE ROLE zaza_report LOGIN;  -- password set out of band
  GRANT CONNECT ON DATABASE zaza TO zaza_report;
  GRANT USAGE ON SCHEMA public TO zaza_report;
  GRANT SELECT ON employees, activity_periods, daily_summaries, weekly_summaries,
        monthly_summaries, alembic_version TO zaza_report;
  ```

### 6.3 Manager dashboard access (Phase 7)

- **Who logs in:** employees do not; they are the subjects of reports.
  Managers and admins log in with `manager_users` accounts.
  - There is no default account. Accounts are created, disabled, enabled,
    reset and signed out with the `manager-*` CLI commands.
  - Passwords are typed at a `getpass` prompt, never passed as arguments.
- **Passwords:** Argon2id (`argon2-cffi`). A database CHECK refuses
  anything but an `$argon2id$` hash. Minimum 12 characters.
  - Login failures always give the same message.
  - Unknown usernames still cost one hash check.
- **Sessions:**
  - The browser holds an opaque random token; PostgreSQL stores only its
    SHA-256.
  - The cookie is HttpOnly, SameSite=Strict, `Path=/manager`, with an
    absolute expiry of `ZAZA_DASHBOARD_SESSION_HOURS`, default 12.
  - Logout, revocation, a password reset or disabling the account ends
    sessions immediately.
  - Tokens are never logged, rendered or audited.
- **Localhost-only in Phase 7:** `serve` mounts the dashboard only on a
  loopback bind host (`127.0.0.1`, `localhost`, `::1`). On `0.0.0.0`, LAN or
  public addresses it is not mounted (404 on `/manager`, plus a printed
  notice), so it is never exposed over plain network HTTP. The sync API is
  unchanged. This rule stays in Phase 10: production binds `127.0.0.1:8100`
  and is reached only through the HTTPS reverse proxy on the same server
  (§6.6).
- **Production MUST set `ZAZA_DASHBOARD_COOKIE_SECURE=true` behind HTTPS
  (Phase 10).** The default `false` exists only so the dashboard works on
  `http://127.0.0.1` during development; `serve` prints a warning while it
  is off.
- **CSRF:** every POST carries a per-session token (an HMAC of the session
  token) and is refused if its `Origin` is foreign. The login form has its
  own double-submit token. GET requests don't change data.
- **Browser hardening:**
  - `Cache-Control: no-store`, `nosniff`, `Referrer-Policy: same-origin`,
    `X-Frame-Options: DENY`. Other sites still get no referrer; the
    dashboard's own form posts carry their real `Origin`, which the
    same-origin check requires (`Origin: null` is refused).
  - A strict CSP with `script-src 'self'`, no inline scripts or styles, and
    `frame-ancestors 'none'`.
  - Everything is served locally: no CDN, fonts, analytics or tracking.
- **Charts and analysis (Phase 8):**
  - Charts are server-rendered SVG with auto-escaped text: no chart
    library, no CDN, no new script, CSP unchanged. Malicious employee and
    application names are tested to render as text.
  - `/manager/analytics` and `/manager/api/analytics` need the manager
    session; the JSON holds display values only.
  - The analysis is deterministic Python rules: no AI, no external calls,
    no new telemetry, and no productivity score or employee ranking number.
- **XSS:** window titles, application names, domains and employee names are
  untrusted. Templates auto-escape everything, and the script uses only
  `textContent`. Tests render `<script>` / `<img onerror>` /
  `javascript:` values and check they appear as text.
- **What the dashboard never shows:** payloads, content or summary hashes,
  record versions, device tokens, password hashes, database settings.
  Privacy-excluded periods show only the stored placeholders.
- **Audit (`audit_logs`):**
  - What is recorded: manager account creation, login, logout, password
    reset, disable/enable and session revocation (entity
    `manager_user`), and every schedule create/update/delete (entity
    `work_schedule`).
  - Each row has the actor (role + username), the action, the IDs and the
    old/new values.
  - Never recorded: passwords, tokens or CSRF values. Page views are not
    audited.
- **Login rate limiting (Phase 10):** application-level, chosen instead of a
  fixed IP allowlist because managers sign in from changing addresses.
  - **What is counted:** failed sign-ins per real client address (default
    10) and per submitted username (default 20), in a sliding 15-minute
    window.
  - **Which address:** the real address comes from the trusted local proxy
    only. A client cannot reset its count by sending its own
    `X-Forwarded-For`.
  - **No account discovery:** unknown usernames are counted exactly like
    real ones, so the limit says nothing about which accounts exist.
  - **When a limit is reached:** 429 with `Retry-After`, before any
    password is checked, so the correct password is refused too while
    limited. The generic failure message is unchanged.
  - **Reset:** a successful sign-in clears both counts.
  - **Memory:** the state is in memory, bounded and self-expiring (one
    server process).
  - **Logging:** only the client address is logged when a limit is
    reached, never the username or password.

  There is no permanent account lockout, so a known username can't be
  used to lock its owner out for good.

### 6.6 Production server hardening (Phase 10 — IN PROGRESS)

See [docs/zaza/PRODUCTION_SERVER.md](docs/zaza/PRODUCTION_SERVER.md).

- **Network:**
  - The server listens on `127.0.0.1:8100` only, and Caddy on the same
    machine terminates HTTPS. (The Caddy site block, DNS and host name
    come in Phase 10C.)
  - No firewall rule is needed: nothing new listens on a public interface.
- **Forwarded headers:**
  - `X-Forwarded-For` and `X-Forwarded-Proto` are trusted only from
    loopback addresses in `ZAZA_TRUSTED_PROXY_IPS`. Any other value is a
    start-up error, and the headers are ignored from every other peer.
  - uvicorn's proxy-header handling is off, so the headers are handled in
    one place (`http_security.py`).
- **Host header:** with `ZAZA_PUBLIC_HOST` set, unexpected `Host` values get
  400. Loopback names are accepted only for direct local requests.
- **Cookies:** `serve` refuses to start with a public host name unless
  `ZAZA_DASHBOARD_COOKIE_SECURE=true`.
- **Service identity:** the virtual account
  `NT SERVICE\ZaZaEmployeeManagement`. It has no password, and its own SID
  means the secret files can be granted to ZaZa alone. Plain
  `LocalService` was rejected because it is shared by many Windows
  services. No Windows user was created.
- **File permissions:**
  - `C:\apps\ZaZaEmployeeManagement` does not inherit from `C:\apps`, where
    `BUILTIN\Users` can read.
  - Only Administrators and SYSTEM have access. The service account gets
    read on the code and config, modify on `logs\` and `run\`, and read on
    single secret files.
  - `secrets\` and `backup\` block inheritance.
- **Secrets:**
  - Database passwords live in files in `secrets\`: 40 random
    characters, generated by `zaza_db_credentials.py`.
  - Each process reads only its own role's file. The owner (migration)
    password is never readable by the service.
  - Roles are created from **SCRAM-SHA-256 verifiers**, with statement
    logging off for that session. So a plaintext password never appears in
    SQL, on a command line, in the PostgreSQL log or on screen.
  - No real or placeholder secret exists yet.
- **Database privileges** (`deploy/postgres/02-grant-privileges.sql`):
  - Explicit per-table grants; nothing on "all tables".
  - The API role has DELETE only on `work_schedules`, and only INSERT on
    `audit_logs`.
  - The Sheets role is SELECT-only on six tables.
  - All three roles are `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS`, with connection limits. A test fails if a migrated table
    lacks an explicit grant.
- **Supply chain:**
  - The server installs only its own dependencies, with exact pins and
    SHA-256 hashes, wheels only.
  - No employee-side capture, OCR, audio, UI-automation or input-hook
    package is present; tests enforce this.
  - The Python 3.14.8 installer was checked against its PSF Authenticode
    signature and the SHA-256 published on python.org.
- **Code deployed:** an exact commit, exported with `git archive` from a
  server-only allowlist. `.git`, `.env`, secrets, caches and archives can't
  be included. Each release records its commit and per-file SHA-256.
- **Logs:**
  - Bounded: 60 MiB for the server log, plus pruned NSSM rotations and
    job logs.
  - Redacted: `Authorization`/`Cookie` values, bearer and session tokens,
    password/secret fields, database URL credentials, PEM keys, Google
    tokens and the database password are masked on every line, including
    tracebacks.

### 6.4 Reporting interpretation (Phase 5)

Attendance summaries are built to be **fair to employees**:

- **Activity is a proxy.** Active Hours mean computer input was seen, not
  that work was or wasn't done. No figure is a productivity score, and
  reports must not label one as such.
- **Missing or uncertain data is never the employee's fault.**
  - UNKNOWN time is never treated as idle, lateness or early leave.
  - A shift with no reliable data is `DATA_INCOMPLETE`, not `ABSENT`.
    Reliable data is missing when UNKNOWN covers half the shift or more, the
    employee has no enabled device, or a device hasn't synced since the
    shift ended.
  - Days marked `DATA_INCOMPLETE` count on neither side of the attendance %.
  - An agent crash or an unsynced device excludes the uncertain part of
    the shift's end from early leave; only reliably observed inactivity is
    charged.
- **Detected Break / Idle is not an official break.** It is a detected
  away-from-keyboard run of 15 min or more, and is labelled that way.
- **Configuration is explicit.** There are no hidden grace periods: every
  threshold is set openly in configuration and recorded with each daily
  summary (the `policy` column).
- **Privacy:** summaries hold durations, timestamps and statuses only.
  Window titles stay in `activity_periods` and are not copied into the
  summaries.

### 6.7 SaaS tenant isolation (Phase 10C — foundation, not deployed)

The SaaS direction (Google Cloud) isolates customers **database-per-tenant**,
with a separate control plane. Full design:
[docs/zaza/SAAS_TENANCY.md](docs/zaza/SAAS_TENANCY.md).

- **A customer's employees and activity live only in that customer's
  database.** The control database holds organizations, SaaS users,
  memberships, the tenant-database directory and an append-only control
  audit log: no activity, no passwords, no connection strings, no secrets.
- **The tenant is decided only by authenticated credentials:** a membership
  (managers) or the device credential (agents). An organization id or slug
  sent by a browser or agent is never sufficient. A valid token of
  organization A plus a claim of B is refused, and B's database is not
  opened.
- **Database names are generated by the server** (`zaza_t_` + 128 random
  bits) and checked by one strict pattern in code and in the database.
  Connection details come only from server configuration.
- **`employees.role` grants nothing in the SaaS.** Only
  `organization_memberships.role` (OWNER/ADMIN/MANAGER/VIEWER) does.
- **Platform staff are not tenant readers by default.** Future support
  access must be explicit, temporary, audited and visible to the customer
  where practical.
- **Jobs and reports run per tenant**, with one explicit context each. They
  never aggregate customers.
- The privacy model (§2) is unchanged.

**Phase 10D: production database access** (design:
[docs/zaza/CLOUD_SQL_PRODUCTION.md](docs/zaza/CLOUD_SQL_PRODUCTION.md); not
deployed):

- **Address.** Cloud Run reaches Cloud SQL only through the service's Unix
  socket. The instance connection name is server configuration, strictly
  validated, never from a request, the control database or a tenant
  descriptor. The instance accepts only Cloud SQL connectors (IAM-authorized
  and encrypted); there are no authorized networks.
- **Passwords.** They are Secret Manager secrets mounted as files
  (`ZAZA_DB_PASSWORD_FILE`, `ZAZA_CONTROL_DB_PASSWORD_FILE`), never in the
  image, source, Terraform variables or state. They are redacted from logs,
  errors, preflight output and migration reports.
- **No service-account JSON keys.** Cloud Run uses its runtime identity;
  `check-config --cloud-run` fails if a key file is configured.
- **Roles.** The web runtime role has no SUPERUSER, CREATEDB or CREATEROLE,
  and DML only on tenant tables. Migrations and tenant provisioning use a
  separate migrator role in a separate job. New tenant databases are
  closed to PUBLIC.
- **Tenant connections** take only a resolved `TenantContext`:
  - they are bounded per instance and across instances (the connection
    budget);
  - a failing tenant cannot affect another tenant's pool.
- **Public health** reveals no database, tenant or Cloud SQL detail.

**Phase 10F-A: Cloud Run HTTP trust model** (code only; no web service is
deployed):

- `ZAZA_HTTP_DEPLOYMENT_MODE` is explicit (`loopback-proxy` default,
  `local`, `cloud-run`) and never inferred from a request.
- **In `cloud-run` mode:**
  - the external scheme is https by deployment;
  - every forwarded header (`X-Forwarded-For/Proto/Host`, `Forwarded`) is
    ignored, so clients cannot choose the scheme, host or address;
  - the client address is "unverified" and no security decision uses it.
    IP-based controls belong to a future load balancer and Cloud Armor
    layer.
- **Host** is validated against `ZAZA_PUBLIC_HOST` (400 otherwise). Cloud
  Run mode will not start without it, or without Secure cookies.
- **Same-origin** has one rule, against the external origin: exactly one
  Origin, equal to `https://<host>`. `http`, `null`, other hosts or ports,
  paths and multiple values are refused. CSRF and login double-submit
  tokens are unchanged.
- **The legacy manager dashboard is never served in Cloud Run mode.** Its
  per-database accounts are not the SaaS identity model.
- **The in-memory login limiter is per process**, not global on Cloud Run.
  The SaaS login must use shared rate-limit state or an edge control.
- **The Windows loopback-proxy behaviour is unchanged.**

**Phase 10F-B: SaaS authentication and credential-derived tenant routing**
(code only; see [docs/zaza/SAAS_AUTH.md](docs/zaza/SAAS_AUTH.md)):

- **SaaS logins:**
  - control-plane `users` with Argon2id password hashes;
  - server-side sessions whose token is stored only as SHA-256; Secure,
    HttpOnly, SameSite=Strict cookies;
  - CSRF and double-submit tokens plus the central same-origin rule;
  - generic errors and a dummy hash check for unknown emails.
- **A SHARED login rate limit** lives in the control database: per login
  identity digest plus a global burst bucket, with atomic counters across
  instances. It never uses client IPs.
- **A session authorizes no organization by itself.** Each organization is
  resolved through an ACTIVE membership, an ACTIVE organization and a READY
  tenant, and permissions are explicit per role. OWNER is not a platform
  admin, and `employees.role` grants nothing in the SaaS.
- **Device credentials are routed, not trusted:**
  - the control plane maps a domain-separated hash of the token to an
    organization (never the raw token, never the tenant's hash);
  - the token is then verified inside that tenant database, whose
    repository stays leased for the request;
  - a stale or forged route authenticates nothing, and no client-supplied
    organization, slug or database name is ever used.
- **There is no self-signup.** Identities are created only through
  operator commands, with hidden password prompts.

**Phase 10G-A: one-time device enrollment** (code only; see
[docs/zaza/ENROLLMENT.md](docs/zaza/ENROLLMENT.md)):

- **Codes:** 160 random bits, shown once. The control database stores only
  a domain-separated hash. Codes expire, are single-use and are revocable,
  and a database trigger forbids reactivation. Listings never show the code
  or its hash.
- **Server-side authority:** the code's record decides the organization and
  the employee. The tenant must be READY, the organization ACTIVE and the
  employee active. No organization, tenant, database or employee field from
  the client is accepted.
- **A token becomes routable only when the enrollment completes:** the
  route and the COMPLETED state are written in one control transaction.
  Interruptions leave at most an unroutable token, which is revoked by
  compensation or by `enrollment-reconcile`. Raw tokens and codes are never
  stored or logged.
- **Cleanup is never abandoned:**
  - an enrollment whose token may still exist stays `CLEANUP_PENDING`, and
    every `enrollment-reconcile` run retries it, so an unreachable tenant
    only delays the revocation;
  - it becomes FAILED only once the tenant confirms the token is revoked or
    was never issued;
  - completion and reconciliation can't both win: both require status
    CLAIMED.
- **Concurrent use** of one code yields exactly one credential.
- **Rate limits:** shared per-code and global enrollment limits; no client
  IPs.
- **Edge:** the login and the enrollment endpoint must sit behind an
  external load balancer with Cloud Armor before public traffic, and the
  run.app URL must not stay a bypass.

## 7. Retention

Raw per-tick agent events are retained only long enough to build the
summarized `activity_periods` / `application_usage` rollups described in
ARCHITECTURE.md; daily/weekly/monthly summaries are the long-lived record.

On the employee machine (Phase 2, ARCHITECTURE.md §2.8): raw events are kept
**7 days**. Summarized records are kept until confirmed synced, then for a
**30-day** local safety window. Records that are unsynced or whose sync
failed are never deleted because of age. Both windows are configurable.

Google Sheets: the Activity Log shows the last `ZAZA_SHEETS_ACTIVITY_DAYS`
(30) local dates and the summary tabs the last `ZAZA_SHEETS_SUMMARY_MONTHS`
(12) months. Older rows disappear from the Sheet at the next refresh. This
is display only and deletes nothing from PostgreSQL. Google's own version
history of the spreadsheet is outside ZaZa's control.

Central (PostgreSQL): nothing is deleted automatically yet, and the API role
has no DELETE privilege. Phase 5 summaries are derived data and can always
be recalculated from the synced records. Central retention windows were not
part of the Phase 5 scope. They are an open decision for before production
(§8).

## 8. Open items for later phases (not blocking Phase 0 approval)

- Device authentication — designed and implemented in Phase 3 (§6.1).
  Production enrollment UX — Phase 9 installer.
- Server-side rate limiting and TLS termination — Phase 10. Done in 10B:
  manager login rate limiting in the application (§6.3) and trusted-proxy
  HTTPS handling (§6.6). TLS termination by Caddy is Phase 10C.
- Manager account provisioning/auth mechanism — Phase 7.
- Central (server-side) retention windows: still open; decide before
  Phase 10 production. Local agent retention is defined in Phase 2.
- Production database and roles creation on the VPS, TLS settings for the
  database connection, and backup schedule — Phase 10C (scripts prepared
  in 10B for review: `deploy/postgres/`; see ARCHITECTURE.md §9).
- Google Sheets service-account credential handling — designed and
  implemented in Phase 6 (§6.5). Production key storage on the VPS and the
  `zaza_report` role — Phase 10.
