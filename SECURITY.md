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
- **Central API → PostgreSQL:** the system gets its own database/schema on the
  VPS's existing PostgreSQL 16 instance; it does not read or write any other
  application's tables, and no new PostgreSQL server/instance is created.
- **PostgreSQL → Google Sheets:** one-way, summarized, server-initiated sync.
  Sheets never writes back into PostgreSQL. Sheets access is limited to
  whoever the workbook is shared with (managers), same as the dashboard.
- **PostgreSQL → Dashboard:** manager-authenticated reads only; the dashboard
  has no write path into raw activity data (schedule edits and similar are
  the only writes, and those go through `audit_logs`).

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

### 6.2 Manager and employee access (Phases 5–7)

- Employees do not get dashboard login — they are the subjects of reports,
  not viewers of the manager dashboard, unless a future requirement adds an
  employee-facing self-view.
- Manager/PM accounts (`manager_users`) authenticate to the dashboard and to
  whatever admin actions exist (editing schedules, managing devices).
- All manager actions that change state (schedules, employee/device records)
  are written to `audit_logs` — who did what, when.

## 7. Retention

Raw per-tick agent events are retained only long enough to build the
summarized `activity_periods` / `application_usage` rollups described in
ARCHITECTURE.md; daily/weekly/monthly summaries are the long-lived record.

On the employee machine (Phase 2, ARCHITECTURE.md §2.8): raw events are kept
**7 days**. Summarized records are kept until confirmed synced, then for a
**30-day** local safety window. Records that are unsynced or whose sync
failed are never deleted because of age. Both windows are configurable.
Central (PostgreSQL) retention remains a Phase 4/5 decision.

## 8. Open items for later phases (not blocking Phase 0 approval)

- Device authentication — designed and implemented in Phase 3 (§6.1).
  Production enrollment UX — Phase 9 installer.
- Server-side rate limiting and TLS termination — Phase 10 deployment
  (reverse proxy).
- Manager account provisioning/auth mechanism — Phase 7.
- Central (server-side) retention windows — Phase 4/5. Local agent
  retention is defined in Phase 2.
- Google Sheets service-account credential handling — Phase 6.
