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
- **Two roles in production:**
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
- **Production MUST set `ZAZA_DASHBOARD_COOKIE_SECURE=true` behind HTTPS
  (Phase 10).** The default `false` exists only so the dashboard works on
  `http://127.0.0.1` during development; `serve` prints a warning while it
  is off.
- **CSRF:** every POST carries a per-session token (an HMAC of the session
  token) and is refused if its `Origin` is foreign. The login form has its
  own double-submit token. GET requests don't change data.
- **Browser hardening:**
  - `Cache-Control: no-store`, `nosniff`, `Referrer-Policy: no-referrer`,
    `X-Frame-Options: DENY`.
  - A strict CSP with `script-src 'self'`, no inline scripts or styles, and
    `frame-ancestors 'none'`.
  - Everything is served locally: no CDN, fonts, analytics or tracking.
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
- **Not yet built:** login rate limiting / lockout. Phase 10 puts the
  dashboard behind the HTTPS reverse proxy, which should add rate limiting
  on `/manager/login`.

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
- Server-side rate limiting and TLS termination — Phase 10 deployment
  (reverse proxy).
- Manager account provisioning/auth mechanism — Phase 7.
- Central (server-side) retention windows: still open; decide before
  Phase 10 production. Local agent retention is defined in Phase 2.
- Production database and roles creation on the VPS, TLS settings for the
  database connection, and backup schedule — Phase 10 (needs separate
  approval; see ARCHITECTURE.md §4.10).
- Google Sheets service-account credential handling — designed and
  implemented in Phase 6 (§6.5). Production key storage on the VPS and the
  `zaza_report` role — Phase 10.
