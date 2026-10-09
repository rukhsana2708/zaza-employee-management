# ZaZa Employee Management System — Development Status

**Last updated:** 2026-10-10 (Phase 9 frozen Windows validation)

## Current phase: Phase 9 — Windows employee installer (validated on Windows, pending your review)

Phase 8 was approved with its review fix. Phase 9 packages the approved ZaZa
agent as `ZaZaWorkAgentSetup.exe`. **No VPS deployment, pilot or AI.** The
central server is not deployed.

### Phase 9 summary

- **Product:** ZaZa Work Agent 0.9.0, publisher ZaZa.
  - `ZaZaWorkAgent.exe` (PyInstaller 6.22.3, one folder, no console window).
  - `ZaZaWorkAgentSetup.exe` (Inno Setup 6.7.3; installs into Program
    Files, admin once).
- **New agent modules** (`deskmate/zaza/`):
  - `workagent.py` (entry point), `runner.py`, `enrollment.py`;
  - `instance.py` (single-instance lock), `autostart.py` (logon task XML),
    `control.py` (stop requests);
  - `status.py`, `ui.py` (Status & Privacy and enrollment windows);
  - `app_logging.py` (rotating, privacy-filtered logs), `edition.py`,
    `privacy_notice.py`.

  `paths.py` now uses `%LOCALAPPDATA%\ZaZa\WorkAgent` and migrates the old
  path. The recorder and sync code are unchanged.
- **Installer tooling** (`installer/`): `build.ps1`, `ZaZaWorkAgent.spec`,
  `build_config.py`, `audit_package.py`, `ZaZaWorkAgent.iss`,
  `smoke-test.ps1`, `make_icon.py` and `assets/zaza.ico` (a replaceable
  placeholder). `installer/vm/` holds the disposable Hyper-V VM tooling used
  for the frozen validation.
- **Docs:** `docs/zaza/ADMIN_INSTALL.md`, `docs/zaza/EMPLOYEE_PRIVACY.md`,
  `installer/vm/README.md`.

### Phase 9 frozen Windows validation (2026-10-09/10)

The real frozen artifacts were run in a **disposable Hyper-V VM**: Windows 11
Enterprise Evaluation, build 26300, with Smart App Control off inside the VM
only. The build PC's security settings were not changed. Only the local
development server (`http://127.0.0.1`) was used, never the VPS.

- **Build:** clean `installer\build.ps1`, production flavour, **with tests**.
  - Build PC: Windows 11 Pro 10.0.26300, Python 3.14.6 (64-bit),
    PyInstaller 6.22.3, Inno Setup 6.7.3.
  - `ZaZaWorkAgentSetup.exe`: 15,884,037 bytes, **unsigned**.
    SHA-256 `a0aca39aa0be46cd1886e36a5be77f7980c2ea4284e4e8401818385520f4ce72`.
  - `ZaZaWorkAgent.exe`: SHA-256
    `84e7449851f5bc8b3831da5d4f02076a1b3dce62325118a1426ddcab45800568`.

  The build writes the hashes to `dist\SHA256SUMS.txt` and the audit to
  `dist\package-audit.txt`; both are kept with the build output, not in Git.
- **Package privacy audit:** `RESULT: PASS`. An independent read of the
  `PYZ.pyz` inside the built exe found:
  - 509 modules, of which 36 are first party, all `deskmate.zaza.*`;
  - no upstream DeskMate module, no server, PostgreSQL, Sheets or dashboard
    code, and no prohibited library.
- **Frozen exe before installation**, with no Python on the machine:
  - `--version` prints `ZaZa Work Agent 0.9.0`;
  - `--status` opens the Status & Privacy window (Tcl/Tk loads from the
    bundle).
- **`installer\smoke-test.ps1`** against that exact installer, from a fresh
  VM checkpoint: **78 passed, 0 failed**. It covers:
  - the install, version resource, the real logon task and
    `--enroll-stdin`;
  - DPAPI and the token absent from every data file;
  - URL/TLS rules (public CA accepted; self-signed, expired and wrong-host
    certificates refused);
  - the recorder as the user, not SYSTEM and not elevated;
  - single instance, online sync, the offline queue and its drain;
  - upgrade with pending records, uninstall, reinstall with the same device
    identity, log privacy, and `--remove-local-data`.
- **Interactive pass**, as the standard user `zazaemp`, by screenshots and
  keyboard/mouse:
  - UAC credential prompt ("Publisher: Unknown").
  - Wizard: Welcome → What is recorded → Server and device enrollment →
    Ready → Finish.
  - Enrollment as the employee:
    - a non-loopback `http://` address is refused;
    - a wrong token gets "Device token was not accepted";
    - the real token gets "Connection successful".
  - Status & Privacy shows every required field and no secrets.
  - Re-enroll never shows the stored token.
- **Real logon:** after sign-out and sign-in at the lock screen, the logon
  task started exactly one `ZaZaWorkAgent.exe --background`:
  - parent: the Task Scheduler service host;
  - user `ZAZA-TEST\zazaemp`, not SYSTEM, in the user's interactive session;
  - not elevated;
  - it synced with its DPAPI credentials.

  The same was repeated after the upgrade.
- **Cross-user DPAPI:** with a byte-identical copy of `zazaemp`'s
  `device_credentials.json` and `config.json` in `zazaemp2`'s profile,
  `zazaemp2`'s agent stayed `WAITING_FOR_ENROLLMENT`. It had no database and
  sent no request to the server. The Status window said "Device credentials
  missing - re-enroll this device".
- **Interactive upgrade** while offline with pending records:
  - no restart needed, nothing scheduled to be replaced at reboot;
  - the agent was stopped first;
  - config and credentials stayed byte-identical, and pending records were
    kept;
  - one task and one recorder afterwards; no re-enrollment;
  - the queue drained when the server returned.
- **Uninstall from Settings › Installed apps:**
  - removed: the agent process, program files, task, Start-menu folder and
    uninstall registration;
  - `activity.db`, config, credentials and logs were kept.
- **Reinstall:** the existing enrollment was reused, with no enrollment
  window. The server still had exactly one device.
- **`--remove-local-data`:**
  - with unsynced records it warns "8 activity record(s) have NOT been
    uploaded yet. Unsynced activity records will be permanently deleted";
  - **No** keeps everything, and the agent restarts;
  - **Yes** removes the data folder and the DPAPI credentials.
- **Logs:** the employee's `agent.log` contained no token, Bearer or
  Authorization text, URL, window title, per-tick activity, clipboard,
  screenshot or keystroke content, or database credentials. It is rotated at
  1 MB with 5 files kept.

**Defects found by the frozen validation and fixed** (each has a regression
test where testable):

1. **Upgrade ignored a failed stop.** `--stop-agents` always returned 0, and
   `PrepareToInstall` ignored the exit code. Now a survivor gives exit 1,
   and setup aborts before changing anything. Startup-task registration and
   the uninstall cleanup also check their results.
2. **No console in the windowed exe.** `--version` and `--enroll-stdin`
   output went nowhere. Output now uses a redirected stdout or the parent
   console, and `--enroll-stdin` without input fails clearly.
3. **Tk's feather icon** on every window. The ZaZa icon is now bundled and
   used.
4. **Status window taller than a 768-pixel screen,** so its buttons were
   off-screen. The buttons are now pinned at the bottom, the privacy lists
   sit side by side, and the window is sized to the screen.
5. **Welcome page skipped.** Inno Setup 6 hides it by default; it is now
   enabled (`DisableWelcomePage=no`).
6. **"No" in `--remove-local-data` left monitoring stopped** until the next
   sign-in. The agent now restarts. With 0 unsynced records, the dialog says
   so.
7. **Re-enrollment could restart recording twice** and split the work
   session. The credentials file and `config.json` are written separately;
   the runner now waits until both have settled (2 s). This showed up as a
   flaky test.
8. **Enrollment window:** the cursor starts in the first empty field, and
   Enter submits.

The smoke test itself was also corrected:
- PowerShell 5.1 `.Count` on a single CIM object;
- a locked `agent.lock` during the token scan;
- `[::1]` expectations;
- counting the VM harness's own tasks.

### Phase 9 test results

- **Default run** (build PC and VM): 756 passed, 0 failed, 129 skipped.
- **PostgreSQL-enabled** (in the VM, throwaway loopback PostgreSQL 16.15):
  884 passed, 0 failed, 1 skipped (the live Google Sheets test).
- `test_installer.py`: 67 tests.
- **Frozen Windows smoke test:** 78 passed, 0 failed.

### Phase 9 known limitations / risks

- **Unsigned build:** SmartScreen and Smart App Control warn or block until
  a real Authenticode certificate is used. On a PC with Smart App Control
  on, the unsigned installer will not run at all.
- **The build PC's Smart App Control blocks unsigned DLLs, intermittently.**
  For example, SQLAlchemy's compiled extension was blocked once during a
  build. So the PostgreSQL-enabled suite was run in the VM, and the build's
  test step may need a re-run on this PC.
- **Enrollment window focus:** it opens on top, but Windows' foreground
  lock can leave it without keyboard focus until the employee clicks into
  it.
- **Sign-out ends the agent abruptly.** The session is closed by Phase 2
  recovery at the next start (logged as an interrupted session), not by a
  clean shutdown.
- **No tray icon** (deferred), and no automatic updates.
- **Enrollment** happens after installation, as the employee (DPAPI is per
  user); silent installs need a separate per-user enrollment step.
- **Website domains** are not detected (unchanged from Phase 2); the
  privacy notice says so.
- `deskmate/zaza.zip`, an old source snapshot committed in Phase 3, is still
  in the repository. It is not packaged, and can be deleted in a cleanup.

### Phase 8 (approved)

Approved with its review fix (summary coverage gates comparisons).


Phase 7 was approved with its review fixes. Phase 8 adds six charts and
automatic, **deterministic rule-based analysis (not artificial
intelligence)** to the dashboard. **No installer, VPS deployment, pilot or
AI.** No new telemetry. Everything runs on localhost only.

### Phase 8 summary

- **Code:**
  - `dashboard/analysis.py`: chart data, previous-period comparison,
    eligibility, insight rules.
  - `dashboard/charts.py`: SVG geometry.
  - `templates/_charts.html`, `templates/analytics.html`, and chart CSS.
- **Charts:** server-rendered SVG, with no JavaScript library and no CDN,
  so the CSP is unchanged.
  - The six charts: Active Hours by Employee; Active/Idle/Unknown/Locked;
    Daily Active Hours; Weekly Work Trend; Application Usage (top 10);
    Attendance (late, early, absent, data-incomplete days).
  - Every chart has a title, units, a description, a legend, patterns as
    well as colours, and a data table.
- **Pages:**
  - New `/manager/analytics` page: all charts, up to 8 insights and a
    previous-period comparison table.
  - Overview: 3 compact charts and up to 5 insights.
  - Employee page: 4 charts above the existing tables.
  - `GET /manager/api/analytics` (version 1).
- **Data sources:** stored `daily_summaries` (one query per page),
  `application_usage_daily` (one aggregated query) and the Phase 7
  employee-local selection. Nothing is recalculated from activity periods.
- **Analysis:**
  - Fixed rule order: data quality, absence, Active Hours (team total,
    average, highest/lowest among eligible employees, active % of
    scheduled), comparison, late/early, high idle (≥ 40% of tracked),
    overtime, top application.
  - Neutral wording, with disclaimers; no productivity score.
- **Tests:**
  - Default run: 689 pass and 129 skip. The skips are the opt-in PostgreSQL
    tests and the opt-in live-Google test.
  - With `ZAZA_TEST_POSTGRES_URL` set: 817 pass and 1 skip (the live
    Google test).
  - New: 62 tests in `test_analytics.py` and 1 PostgreSQL analytics test.

### Phase 8 review fix (approved)

- **Summary coverage.** For each employee and period, the expected dates are
  the selected local dates up to their local today; future dates are never
  missing.
- **Comparable employee** now also requires no missing expected summary.
  This applies to highest/lowest, the comparable average and the high-idle
  observation. Charts still show all recorded data.
- **Team wording separates the two groups:** "Total recorded Active Hours
  across available summaries" versus "N employee(s) have complete enough
  data for employee-to-employee comparison. Average … among those
  employees". Excluded employees are named.
- **Period comparisons check both periods.** Missing summaries or
  INSUFFICIENT/DATA_INCOMPLETE days on either side make the comparison
  **incomplete**: no percentage or increase/decrease, a DATA_QUALITY notice,
  and table rows marked "not comparable".
  START/END_UNCERTAIN or AWAITING_DEVICE_SYNC makes it **qualified**: the
  change is shown with a caution.

### Phase 8 known limitations / risks

- **No manual browser review yet.** SVG layout, long labels (cut at 28
  characters, full name on hover and in the table) and the 420 px chart grid
  have only been checked through TestClient output.
- **Comparing a period still in progress** (This Week, This Month) with a
  full previous period naturally shows a decrease. The insight and the page
  say "still in progress", but managers may still misread it.
- **Team weekly buckets add up each employee's local week.** With mixed time
  zones, the week boundaries differ by employee; this is labelled.
- **The "active % of scheduled" caution threshold** (UNKNOWN above 10% of
  tracked time) is a fixed constant, not a setting.
- **Highest/lowest** names individuals. It is neutral and disclaimed, but it
  is still a comparison of individuals; managers should read it with the
  data-quality notes.

### Phase 7 (approved)

Approved with its review fixes (ID-based KPI counts, `is_active` Active
Employees, localhost-only dashboard, real `serve` wiring tests).


Phase 6 was approved with its review fixes. Phase 7 adds a server-rendered
manager dashboard under `/manager` in the existing FastAPI server. It reads
PostgreSQL directly and never Google Sheets. **No charts, analysis,
installer, VPS deployment or AI.** It runs on localhost only. PostgreSQL
tests used a throwaway local instance, which has since been deleted.

### Phase 7 summary

- **Code:** `deskmate/zaza_server/dashboard/`:
  - `config`, `security`, `models`
  - `queries`: the PostgreSQL repository, plus an in-memory one for tests
  - `auth`, `service`
  - `routes`: rendering only
  - `templates/`, `static/`
- **Migration `0004_manager_dashboard_auth`** (forward-only; 0001–0003
  untouched):
  - `manager_users`: lower-case unique username; a CHECK that only accepts
    Argon2id hashes; role ADMIN/MANAGER.
  - `manager_sessions`: SHA-256 hashes of the session and CSRF tokens; an
    expiry; revocation.
  - A `(device_id, ended_at)` index for current status.
  - `audit_logs` now also accepts entity `manager_user`.
- **Auth:**
  - Argon2id passwords (minimum 12 characters) and a generic login error.
  - Opaque random session token in a cookie that is HttpOnly,
    SameSite=Strict, Path=/manager and Secure when configured; absolute
    12-hour expiry.
  - Per-session CSRF token on every POST, plus an Origin check.
  - The login form has its own token.
  - Strict security headers and CSP.
  - Accounts are managed only by the CLI: `manager-create`, `-list`,
    `-disable`, `-enable`, `-reset-password`, `-revoke-sessions`, with
    `getpass` prompts.
- **Pages:** Overview, Employees, Employee detail, Attendance, Applications,
  Schedules; plus authenticated JSON for current status, overview,
  attendance and applications. Status refreshes every 60 s while the tab is
  visible. No charts.
- **Figures:**
  - Totals are sums of stored `daily_summaries` over each employee's own
    local period.
  - Attendance % = Σ credit ÷ Σ basis, never an average of daily
    percentages.
  - Where employees' local periods differ, the page says so.
- **Current status:**
  - Enabled devices only.
  - Online means `last_seen_at` within 300 s; the device's state comes from
    its latest period if it is recent.
  - Several devices: ACTIVE > IDLE > LOCKED > UNKNOWN > online-no-activity
    > offline.
  - UNKNOWN is never shown as idle.
- **Schedules:**
  - They use the Phase 5 write path, so the database validates every rule
    and every change is audited with the manager's identity and old/new
    values.
  - Changes apply from the employee's local today; running weekly rules are
    split or ended from a date, and past rules are read-only.
- **Tests:**
  - Default run: 627 pass and 128 skip. The skips are the opt-in PostgreSQL
    tests and the opt-in live-Google test.
  - With `ZAZA_TEST_POSTGRES_URL` set: 754 pass and 1 skip (the live
    Google test).
  - New: 96 dashboard tests (`test_dashboard.py`, no database) and 7
    PostgreSQL tests (`test_dashboard_postgres.py`).

### Phase 7 review fixes (approved)

1. **Employee-level flags are counted by employee ID.** Late, absent and
   data-incomplete employees are deduplicated by ID, not display name.
   Duplicate names are shown with their ID ("John Smith (emp-01)"). The JSON
   adds `*_count` and `*_ids`.
2. **"Active Employees" counts `is_active` employees.** An explicitly
   selected former employee gives 0, while their history stays reportable.
3. **The dashboard is localhost-only.** `serve` mounts `/manager` only on a
   loopback bind host (`build_server_app()`); otherwise it prints a notice
   and serves the sync API alone, whose behaviour is unchanged.
4. **Real `serve` wiring tests.** `main(["serve", …])` is run with
   `uvicorn.run` replaced: offline with an in-memory repository, and on
   PostgreSQL with a full login. The tests check that the dashboard and the
   sync API share the app, and that the dashboard is refused on non-loopback
   hosts.

### Phase 7 known limitations / risks

- **The loopback check trusts the `--host` value.** A host name other than
  `localhost` is refused, even one that resolves locally. Phase 10 replaces
  this with the HTTPS reverse-proxy setup.
- **No login rate limiting or lockout.** Argon2 slows guessing, but Phase 10
  should rate-limit `/manager/login` at the reverse proxy.
- **The Secure cookie flag is off by default** so the dashboard works on
  localhost. Production must set `ZAZA_DASHBOARD_COOKIE_SECURE=true` behind
  HTTPS.
- **The Origin check compares with the request's own scheme and host.**
  Behind a TLS-terminating proxy, Phase 10 must forward the original scheme
  and host (proxy headers) or adjust the check.
- **Figures are only as fresh as the last `recalculate`.** The dashboard
  shows "last calculated …", and today's row may be missing until the next
  run. There is still no scheduler (Phase 10).
- **Application usage** is grouped by the device's calendar date, which can
  differ from an overnight shift's attendance date. This is labelled on the
  pages.
- **Removing an upcoming rule is a real DELETE** (audited, with old values),
  so production `zaza_app` needs `GRANT DELETE ON work_schedules`
  (SECURITY.md §6.2).
- **Account management is CLI-only**, and the ADMIN role adds nothing in the
  UI yet.
- **No manual browser testing yet.** The pages have only been exercised
  through FastAPI's TestClient.

### Phase 6 (approved)

Approved with its review fixes (one-connection refresh, interval-overlap
Activity Log window, truthful per-employee Dashboard labels).


Phase 5 was approved with its review fixes. Phase 6 adds a one-way,
read-only export from PostgreSQL to an existing Google spreadsheet. **No
dashboard, charts, installer, VPS deployment or AI.** Nothing connected to
a real Google account or to the VPS. PostgreSQL tests used a throwaway local
instance, which has since been deleted.

### Phase 6 summary

- **Code:** `deskmate/zaza_server/sheets/`:
  - `config.py`, `models.py`, `queries.py`, `formatter.py`
  - `client.py`: the `SheetsClient` interface; `GoogleSheetsClient` using
    the official Google libraries (optional extra `zaza-sheets`);
    `FakeSheetsClient` for tests.
  - `exporter.py`
- **CLI:** `sheets-init`, `sheets-sync` and `sheets-status`.
- **Tabs:** Activity Log, Daily Summary, Weekly Summary, Monthly Summary,
  Dashboard (no charts).
  - The Activity Log has one row per stored activity period. It has no raw
    events and invents no LOGIN/LOGOUT events.
  - Each duration goes only in its status's column.
  - Privacy-excluded periods stay redacted exactly as stored.
- **Authority:** every figure is a stored Phase 5 value. Dashboard totals
  are sums of stored seconds; team Attendance % is Σ credit ÷ Σ basis.
- **Time zones:** employee rows use the employee's time zone. Team-level
  times use `ZAZA_SHEETS_TIMEZONE`, or the employees' common zone, or UTC,
  and are always labelled.
- **Safety:**
  - PostgreSQL is read in one read-only snapshot before Google is touched.
  - Tabs are overwritten first, and stale rows are cleared only afterwards.
  - The Dashboard's "Last successful refresh" is written last.
  - An advisory lock prevents concurrent refreshes. The lock and the
    read-only snapshot share one connection, so `ZAZA_DB_POOL_MAX=1` works.
  - Values are written as RAW, so no formula injection.
- **Secrets:** the key file is validated without echoing it. Errors are
  scrubbed (PEM keys, key IDs, OAuth tokens), and the spreadsheet ID is
  masked.
- **Tests:**
  - Default run: 531 pass and 121 skip. The skips are 120 opt-in PostgreSQL
    tests and 1 opt-in live-Google test.
  - With `ZAZA_TEST_POSTGRES_URL` set: 651 pass and 1 skip (the live test).
  - New: 78 Sheets tests (`test_sheets.py`), 8 PostgreSQL tests
    (`test_sheets_postgres.py`), and 1 opt-in live test
    (`test_sheets_google_live.py`).
  - The Google adapter is tested offline with the library's
    `HttpMockSequence`.

### Phase 6 review fixes (approved)

1. **One connection per refresh.** `PostgresReportSource.refresh()` takes the
   advisory lock, reads the read-only snapshot on the same connection, and
   keeps the lock through the Google writes. Sheets sync works with
   `ZAZA_DB_POOL_MAX=1`, and a concurrent refresh is still refused.
2. **Activity window overlap.** A period is shown if it overlaps the window
   (`ended_at > cutoff OR started_at >= cutoff`), both in SQL and in memory,
   with each employee's own cutoff. A period crossing the boundary is shown
   once, whole.
3. **Truthful Dashboard period labels.** When active employees are on
   different local dates / weeks / months, the label reads "Per employee
   local date / week / month (…)" instead of naming one.

### Phase 6 known limitations / risks

- **No scheduler yet.** Phase 10 runs `recalculate --recent-days 7` and then
  `sheets-sync`. The Sheet is only as fresh as the last run of both.
- **Not atomic across tabs.** A refresh that fails part-way can leave a tab
  with a mix of new and old rows. The Dashboard then still shows the old
  "Last successful refresh" and "IN PROGRESS", and the next successful run
  repairs it.
- **Activity Log size.** With many short periods, 30 days for 5 employees
  can reach tens of thousands of rows (Google's limit is 10 million cells).
  Lower `ZAZA_SHEETS_ACTIVITY_DAYS` if the Sheet becomes slow. The grid
  grows but is never shrunk (cleared rows stay as empty grid).
- **Managed tabs are fully rewritten.** Manual edits or extra columns in
  the five tabs are lost at the next refresh. Other tabs are untouched.
- **Not yet tested against real Google.** Only the fake and the official
  library's offline mocks have been used. The opt-in live test needs a
  disposable spreadsheet.
- **Spreadsheet version history** (Google's) keeps older contents. That is
  outside ZaZa's control.

### Phase 5 (approved)

Approved with its review fixes (schedule timezone = employee timezone,
per-employee `--recent-days`, early-leave uncertainty, full policy
configuration).

Phase 4 was approved after the overnight-shift schedule fix. Phase 5 adds
deterministic attendance calculations. The results are stored in PostgreSQL
and are the authoritative source for Sheets, the dashboard and charts (Phases
6–8). **No Sheets, dashboard, charts, AI, installer or VPS work.** All
PostgreSQL testing used a throwaway local instance, which has since been
deleted.

### Phase 5 summary

- **Code:** `deskmate/zaza_server/attendance/`. The daily calculation is a
  pure function, so the same inputs always give the same output.
  - `calculator.py`: the daily calculation.
  - `schedule.py`: schedule resolution, DST-correct UTC shift times, and
    date attribution.
  - `timeline.py`: the non-overlapping timeline.
  - `rollup.py`: ISO week and month roll-ups.
  - `summary_service.py`: `calculate_daily`, `calculate_week`,
    `calculate_month` and `recalculate`.
  - `store.py` and `postgres_store.py`: storage.
- **Migration `0002_attendance_summaries`:** forward-only. It adds
  `daily_summaries`, `weekly_summaries` and `monthly_summaries`, with keys,
  foreign keys and named CHECKs (for example, tracked = active + idle +
  unknown + locked; no lateness on a non-late day; ISO Monday weeks). It
  also adds team-view indexes. `0001_initial` was not touched.
- **Fairness:**
  - UNKNOWN is never treated as idle, lateness or early leave.
  - Missing or unsynced data gives `DATA_INCOMPLETE`, not `ABSENT`.
  - An agent crash or unsynced device excludes only the uncertain part
    of the shift's end from early leave.
  - Every instant belongs to exactly one date, so nothing is counted twice.
- **Formulas and statuses:** ARCHITECTURE.md §4.11; the reporting
  interpretation is in SECURITY.md §6.4.
- **CLI:** `add-schedule`, `list-schedules`, `summarize-day`,
  `summarize-week`, `summarize-month` and `recalculate`. They need the
  PostgreSQL backend.
- **Tests:**
  - Without PostgreSQL: 453 pass in `tests/zaza_agent/` and 112 skip. The
    skips are the opt-in PostgreSQL tests.
  - With `ZAZA_TEST_POSTGRES_URL` set: 565 pass and 0 skip.
  - New: 84 engine tests (`test_attendance.py`) and 13 PostgreSQL tests
    (`test_attendance_postgres.py`). The PostgreSQL tests include exact
    equality between the PostgreSQL and in-memory results.

### Phase 5 review fixes (approved)

1. **Schedule timezone = employee timezone.** `add_schedule` /
   `add-schedule` default to the employee's timezone and reject any other;
   migration `0003_schedule_timezone` adds a composite foreign key so direct
   SQL can't create a mismatch (and refuses to apply over existing
   mismatches). Per-schedule travel timezones are not supported.
2. **`--recent-days N`** = the last N local dates including today, per
   employee timezone (`recalculate_recent`); N ≥ 1. `add-schedule`'s default
   `--effective-from` is the employee's local today.
3. **Early leave** excludes only uncertain time (UNKNOWN, after a crash until
   the agent restarts, after a device's last contact). Earlier reliable
   inactivity still counts. `calculation_version` is now 2.
4. **Policy settings:** every `AttendancePolicy` field is configurable from
   the environment with validation (`.env.example`).

### Phase 5 known limitations / risks

- **No production scheduler.** Summaries are only as fresh as the last
  `recalculate`. Phase 10 should run `recalculate --recent-days 7`
  periodically.
- **"Device has synced since" uses `devices.last_seen_at`.** It is a single
  timestamp, so a backlog still uploading after a reconnect could briefly
  look complete.
- **Attribution margin (4 h).** Work more than 4 h after a shift and past
  midnight counts on the next calendar day. It is still counted exactly once,
  and it never hides lateness, because punctuality only looks at the shift
  neighbourhood.
- **Overlapping shifts** (allowed by the schedule constraints) are resolved
  by the earliest-starting shift and flagged `SCHEDULE_OVERLAP`. They are not
  blocked at entry.
- **Schedule rules can't be edited or removed yet.** Only `add-schedule`
  exists, so managing schedules properly is a Phase 7 dashboard feature. A
  newer weekly rule supersedes an older one from its `effective_from` date.
- **The policy is global.** It is not per employee, and changing it
  requires recalculating.
- **One timezone per employee.** Schedules can't use a different zone
  (travel, remote shifts in another region). Changing an employee's timezone
  is blocked while they have schedules; it needs a deliberate migration.
- **Crash point = last heartbeat.** After an INTERRUPTED session, uncertainty
  starts at its last heartbeat; reliable data synced just after it (within
  one heartbeat interval) is not charged as early leave. This errs in the
  employee's favour.
- **`application_usage_daily` is not part of the attendance figures,** by
  design, because of its device-local dates.

### Phase 4 (approved)

Approved after the overnight-shift fix to `work_schedules_hours_valid`.

Phase 4 added a production
PostgreSQL repository behind the unchanged Phase 3 repository interface. The
wire protocol and the agent are unchanged. **Nothing connected to the
production VPS**: all development and testing used a throwaway local
PostgreSQL 16 in a temporary folder, which has been deleted. No Sheets, no
dashboard, no attendance calculations.

### Phase 4 summary

- **Library:** psycopg 3 driver + `psycopg_pool`, using plain SQL with no
  ORM. Alembic handles migrations only. Everything is in the optional extra
  `zaza-postgres`.
- **Code:** `deskmate/zaza_server/postgres/`, with three parts:
  - `repository.py`: `PostgresRepository`.
  - `migrate.py`: upgrade, status, and offline SQL rendering.
  - `migrations/versions/0001_initial_schema.py`: the schema.

  Configuration is in `zaza_server/config.py`, and backend selection in
  `zaza_server/backends.py`.
- **Schema:**
  - Tables: employees, devices, device_tokens, work_schedules (prepared
    only), work_sessions, activity_periods, idle_periods,
    application_usage_daily, and audit_logs (append-only).
  - All columns are typed, and every instant is TIMESTAMPTZ. Each synced row
    also keeps its wire record in JSONB for audit only.
  - See ARCHITECTURE.md §4.2–4.4 and §4.9.
- **Integrity:** primary keys, foreign keys (periods → a session of the
  *same* device), unique keys, named CHECKs, and triggers for time zones,
  `updated_at` and audit append-only. Python validation is not the only
  guard.
- **Idempotency and concurrency:** the shared `decide()` runs on a row
  locked with `SELECT … FOR UPDATE`. A new record uses `INSERT … ON CONFLICT
  DO NOTHING` and then re-decides if it lost a race. See ARCHITECTURE.md
  §4.5.
- **Transactions:** one transaction per batch with a savepoint per record,
  so a constraint failure rejects only that record. A deadlock or
  serialization failure rolls back the whole batch and retries it, up to 3
  times, then answers 503. See ARCHITECTURE.md §4.6.
- **Migrations:** run only by an operator (`migrate`) and idempotent. The
  server refuses to start on an out-of-date schema and never migrates
  itself.
- **Device auth:** the registry is now in PostgreSQL. Token hashes are
  CHECK-constrained to SHA-256 hex, so a raw token can't be stored.
  `last_seen_at` (connectivity, not activity) and token `last_used_at` are
  updated after successful authentication.
- **Shared-interface changes:**
  - Employees are now part of the interface. A device needs an existing
    employee, so the CLI gains `add-employee` and `list-employees`.
  - Every backend now rejects an orphan period, or one that points to another
    device's session.
  - The API runs repository calls in the thread pool and answers 503 when
    storage is unavailable.
- **Tests:**
  - Without PostgreSQL: 369 pass in `tests/zaza_agent/` and 99 skip. The
    skips are the opt-in PostgreSQL tests.
  - With `ZAZA_TEST_POSTGRES_URL` set: 468 pass and 0 skip.
  - A real-process rehearsal (owner/app roles, live agent, outage and
    catch-up, leak checks) also passed.

### Phase 4 review fix (2026-10-08): overnight shifts

`work_schedules_hours_valid` now allows shifts that cross midnight
(20:00→04:00 = 8 h, ending the next local day).
- Equal start and end times, and `24:00`, are rejected: neither is read as
  a 24-hour shift.
- A working day must expect more than 0 seconds, and no more than the span.
- No Phase 4 database had been deployed or kept, so the initial migration
  `0001_initial` was corrected in place rather than adding a revision.
- The day rule for Phase 5 is in ARCHITECTURE.md §4.2.

### Phase 4 known limitations / risks

- **Not yet run on the real VPS.** The tests used PostgreSQL 16.2 locally,
  and the VPS runs 16.x. Creating the database and roles there is Phase 10
  (ARCHITECTURE.md §4.10, SECURITY.md §6.2).
- **The `payload` JSONB duplicates the typed columns,** window titles
  included. It is kept for audit and forward compatibility. It roughly
  doubles storage per row, which is small for 5 employees.
- **Composite session FK:** a period whose session never reaches the server
  is rejected and retried each cycle, which shows as BACKLOG. The agent's
  ordering makes this an edge case: a session that is itself rejected, or
  data lost on the server.
- **Durations use `double precision`.** That's exact enough for seconds, but
  Phase 5 sums should round for display.
- **No central retention or deletion yet** (Phase 5). The API role has no
  DELETE privilege.
- **Coupling to the agent's privacy placeholders:** the
  `privacy_excluded` CHECK mirrors the agent's placeholder strings, so
  changing them in the agent needs a migration.
- **Smart App Control blocks SQLAlchemy's compiled extensions on this PC.**
  `migrate.py` forces SQLAlchemy's pure-Python mode, so migrations work
  regardless.

### Phase 3 (approved)

Approved after the two client fixes: an invalid `stale` reply is never marked
synced, and any closed unconfirmed record reports BACKLOG.

### Phase 3 summary

- **Wire contract** (`deskmate/zaza/sync/protocol.py`, Pydantic):
  `GET /api/v1/sync/health`, `GET /api/v1/devices/me`, and
  `POST /api/v1/sync/batch` (1–500 records, 4 MiB). Requests are strict
  (unknown fields rejected, UTC-only timestamps). Each record is validated
  on its own, so one bad record doesn't fail the batch. See ARCHITECTURE.md
  §3.1.
- **Development server** (`deskmate/zaza_server/`): FastAPI app →
  `SyncService` → `CentralRepository`, with in-memory (tests) and SQLite
  development implementations. A CLI handles device registration, token
  rotation, disabling devices, and viewing records.
- **Idempotency:** keyed on `(record_type, record_id)` + `record_version` +
  content hash. Results are accepted / already_current / updated / stale
  (server keeps the newer) / conflict / rejected, for each record. See
  ARCHITECTURE.md §3.2–3.3.
- **Device auth:** an opaque per-device token stored only as a hash on the
  server, DPAPI-encrypted on the agent outside SQLite. Disable, revoke and
  rotate are supported. See SECURITY.md §6.1.
- **Sync worker** (`deskmate/zaza/sync/worker.py`): its own thread and DB
  connection. Batches go out in device order, and records are marked synced
  only for the version the server confirmed. Backoff is exponential with
  jitter (5s→300s), honours 429 Retry-After, and uses a long fixed retry for
  auth errors. A 413 halves the batch size. Sync health is HEALTHY /
  BACKLOG / OFFLINE / AUTH_ERROR / SERVER_ERROR / NOT_CONFIGURED. See
  ARCHITECTURE.md §3.4–3.5.
- **Agent CLI:** `set-credentials`, `sync-now`, `sync-status`, `report`. The
  normal run starts the sync worker automatically when it is configured.
- **Tests:** 329 in `tests/zaza_agent/`, all passing (137 new for Phase 3, including 11 review-fix regressions).
  A real end-to-end run (uvicorn server + live agent + DPAPI credentials
  + server down/up) also passed.

### Phase 3 known limitations / risks

- **No server-side rate limiting or TLS** in the development server. Both
  belong at the Phase 10 reverse proxy.
- **The employee can read their own token.** DPAPI protects the token from
  other accounts and machines, not from the employee's own account. The
  server limits the damage: a token can only write its own device/employee
  records.
- **Open records are re-sent each cycle** while open, because their version
  moves every tick. That's about 3 small records per minute: fine for 5
  devices, but worth revisiting at scale.
- **A permanently rejected record** stays FAILED and is retried every cycle,
  which keeps the state at BACKLOG. That's visible, but not auto-resolved.
- **`stale` after a local database restore:** the server's newer copy is
  kept, and the local version adopts the server's number.
- **App usage day bounds** come from the agent's current timezone when it
  is sent. If the PC's timezone changes, a later resend can produce a
  `conflict`, which resolves automatically by re-sending a newer version.
- **Concurrent sync processes** (e.g. `sync-now` while the agent runs) may
  send the same records twice. The server is idempotent, so the result is
  harmless.

### Phase 2 (approved)

Phase 1 was approved after Windows manual testing. Phase 2 is implemented
locally. Nothing in it touches the network, PostgreSQL, Google Sheets, a
dashboard, or AI.

### Phase 2 summary

- **Schema v2** (`deskmate/zaza/schema.py`): versioned forward-only
  migrations (a Phase 1 v1 database is upgraded in place), WAL mode. New
  tables: `work_sessions`, `activity_periods`, `idle_periods`,
  `app_usage_daily`, `sync_state`, `counters`. Raw `activity_events` gained
  `event_id`, `session_id`, `status`, `privacy_excluded`. See
  ARCHITECTURE.md §2.5.
- **Aggregation** (`aggregator.py`): raw ticks become contiguous periods
  keyed by status + app + domain. Window titles are normalized, and a title
  change splits a period only after 30s. Idle begins at last input + the idle
  threshold, since the threshold is a grace period. Idle periods are merged.
  See ARCHITECTURE.md §2.6.
- **ACTIVE / IDLE / UNKNOWN / LOCKED:** input is trusted only while both
  hooks are HEALTHY, and lock state only while the session-lock watcher is
  HEALTHY with a lock state established during that stint. DEGRADED/ERROR
  monitoring, an untrusted or stale lock state, a new or restarted session
  with no input yet, and telemetry gaps are all UNKNOWN, never idle. See
  ARCHITECTURE.md §2.3.
- **Work sessions & crash recovery:** a session per agent run, with
  heartbeats and totals. On restart, an OPEN session left by a dead run is
  marked INTERRUPTED at its last heartbeat. A backwards clock step closes the
  session (`CLOCK_CHANGED`). See ARCHITECTURE.md §2.7.
- **App usage** (`rollup.py`): per local day and app, recomputed from
  periods whenever one closes, split at midnight, with deterministic IDs.
- **Privacy exclusions** (`privacy.py`): excluded apps (the title is never
  read), hidden app names, and excluded domains. Domains are reduced to a
  bare hostname. See SECURITY.md §2.3.
- **Retention:** raw events are kept 7 days, including during a long OPEN
  session. Synced records are kept 30 days after being synced.
  PENDING/FAILED records are never deleted. See ARCHITECTURE.md §2.8.
- **Sync readiness (no network):** UUID record IDs, a device-wide
  `local_seq`, `record_version`, sync status/attempts/error fields, and
  `pending_sync` / `mark_synced` (version-checked) / `mark_sync_failed`.
- **Console:** `python -m deskmate.zaza --report` shows stored sessions,
  periods, idle periods, and app usage.
- **Tests:** 192 in `tests/zaza_agent/`, all passing.

### Phase 2 review fixes (2026-10-07)

1. **Idle threshold is a grace period.** IDLE now starts at
   `last input + threshold` (or `trusted since + threshold`), not at the last
   input. Before the first input, the pre-threshold time stays
   UNKNOWN/AWAITING_INPUT and is not converted to idle.
2. **Lock-watcher health gates IDLE.** Without a trusted lock state (watcher
   not HEALTHY, or a state from before an outage), the agent reports
   UNKNOWN/LOCK_STATE_UNAVAILABLE instead of IDLE, and never a stale LOCKED.
   The watcher now queries the current lock state when it registers, and the
   health registry has a per-component generation counter so stale state is
   detectable.
3. **Raw retention applies to OPEN sessions too.** The blanket OPEN-session
   exemption is removed. Nothing reads raw events to rebuild state.

Regression tests: `tests/zaza_agent/test_review_fixes.py`, plus
`test_retention_sync.py::test_long_running_open_session_old_raw_events_deleted_summaries_kept`
and `::test_crash_recovery_works_without_any_raw_events`.

### Phase 2 known limitations / risks

- **Single instance assumed.** If two agent processes share one database,
  the second treats the first's OPEN session as interrupted. The Phase 9
  installer should enforce one instance (e.g. a named mutex).
- **Gap detection** relies on ticks running. A hung (not dead) tick thread
  shows up as a gap only when it resumes, and if the process dies the time
  isn't recorded at all, as intended.
- **Low-level hook silent removal** (`LowLevelHooksTimeout`) is still
  undetectable. Such time would read as idle. Carried over from Phase 1.
- **Input as proof of presence while the lock watcher is down** assumes
  low-level hooks don't receive input typed on the secure lock-screen
  desktop. If that assumption were wrong, lock-screen input could count as
  up to one threshold of ACTIVE time. It would still never count as IDLE.
- **A missed LOCK while the watcher is down** after recent input: up to one
  threshold (5 min) is counted ACTIVE before the time turns
  UNKNOWN/LOCK_STATE_UNAVAILABLE.
- **Lock-state query** uses Windows 10/11 `SessionFlags` semantics. Windows
  7 / Server 2008 R2 report these inverted and are not supported targets.
- **Unsynced data grows** until Phase 3 exists (estimated at a few MB per
  year).
- `synchronous=NORMAL`: an OS crash or power loss can lose the last few
  seconds of committed ticks. An application crash cannot.
- Raw `activity_events` still holds per-tick window titles for 7 days.

### Phase 1 (approved)

Phase 0 (architecture & specification) is **approved**, subject to two
documentation corrections that have been applied:

1. **README.md** now represents the ZaZa Employee Management System first —
   what it is, what it never collects, PostgreSQL/Sheets/dashboard framing,
   and current under-development status — with DeskMate attribution/MIT
   license information kept intact. The original, unmodified DeskMate README
   is preserved at
   [docs/UPSTREAM_DESKMATE_README.md](docs/UPSTREAM_DESKMATE_README.md) for
   upstream reference.
2. **ARCHITECTURE.md** and **SECURITY.md** now explicitly state that browser
   tracking means only the *currently active domain*, where technically
   practical — never full URL paths, browser history, page contents, or
   search/query parameters.

Phase 1 then implemented the privacy-safe local Windows activity agent:

- **New, clearly separated module:** `deskmate/zaza/` — zero changes to any
  existing upstream `deskmate/` file. Upstream DeskMate source is left
  exactly as it was, so the two remain directly comparable.
- **What it tracks:** timestamp, active application/process, active window
  title, keyboard activity presence (boolean), mouse activity presence
  (boolean), idle/active status (default 5-minute threshold, configurable),
  and Windows lock/unlock events.
- **What it cannot capture:** typed characters/key names, clipboard
  contents, screenshots, OCR/accessibility screen text, audio, video — not
  hidden from a UI, not present in any code path or storage column. See
  `tests/zaza_agent/test_no_prohibited_capture.py` for the automated guard.
- **Storage:** a new, separate SQLite database (`~/.zaza_agent/activity.db`,
  overridable via `ZAZA_HOME`) with a single `activity_events` table and a
  fixed, narrow column set. No sync, no PostgreSQL, no network calls.
- **Manual run:** `python -m deskmate.zaza` — console output only, per the
  Phase 1 allowance.
- **Tests:** 82 tests in `tests/zaza_agent/` at Phase 1 approval. Existing
  DeskMate test suite re-run and unaffected (pre-existing, unrelated
  failures noted below, not caused by this work).

### Phase 1 review fixes (2026-10-07)

- **Win64 ctypes types:** `HOOKPROC` (`input_hooks.py`), `WNDPROC` and
  `DefWindowProcW.restype` (`session_lock.py`), and `CallNextHookEx.restype`
  now use `LRESULT = ctypes.c_ssize_t` (pointer-sized, signed) instead of
  `c_long`. `test_win32_signatures.py` guards against regressions.
- **Component health:** new `deskmate/zaza/health.py` — keyboard hook, mouse
  hook, session-lock watcher, foreground-window watcher, SQLite storage, each
  `HEALTHY` / `DEGRADED` / `ERROR`, plus an overall state. A failed hook is
  `ERROR`, not silent: ACTIVITY rows store NULL for the affected flag and for
  `idle`, and IDLE/CONTINUE are suppressed. Shown in the console run
  (startup table + a `[health]` line on each change). `test_health.py`
  simulates hook-install failure with fake `user32`/`kernel32`.
- **Privacy guards:** AST-based checks for keystroke/pointer content APIs in
  executable code (docstrings/comments ignored), a check that hook callbacks
  only forward `lparam` to `CallNextHookEx`, and self-tests proving the
  guards catch offending code. Existing screenshot/OCR/clipboard/audio/video
  guards are unchanged.
- **Known limitation:** Windows can silently remove a low-level hook whose
  callback exceeds `LowLevelHooksTimeout`; that can't be detected directly
  and is not reported as a health change.

## Phase checklist

- [x] Phase 0 — Architecture & specification documents (approved, with README/domain corrections applied)
- [x] Phase 1 — Privacy-safe local DeskMate-derived Windows activity agent (`deskmate/zaza/`)
- [x] Phase 2 — Local SQLite activity storage & aggregation (approved)
- [x] Phase 3 — Central synchronization API (approved)
- [x] Phase 4 — PostgreSQL central storage (approved; local testing only, no VPS)
- [x] Phase 5 — Attendance & work-time calculations (approved)
- [x] Phase 6 — Google Sheets reporting (approved)
- [x] Phase 7 — Manager web dashboard (approved; localhost only)
- [x] Phase 8 — Charts & automatic rule-based analysis (approved)
- [x] Phase 9 — Windows employee installer (implemented, pending review; unsigned; frozen-exe smoke test pending on a Windows VM)
- [ ] Phase 10 — Production VPS deployment
- [ ] Phase 11 — Pilot testing
- [ ] Phase 12 — Optional AI analysis

## Known pre-existing issues (not introduced by this work)

Running the full upstream test suite in this environment surfaces a few
pre-existing issues unrelated to the ZaZa module (confirmed via `git status`
— none of the affected files were touched in this or the prior pass):

- `tests/test_live_translation.py` fails to collect — `numpy` is not
  installed in this venv (an optional extra, not installed here).
- `tests/test_ai_prompt_journal.py` (2 tests), `tests/test_day_recap_context.py`
  (1 test) — timezone/locale-sensitive assertions that don't match this
  machine's local timezone.
- `tests/test_console.py::test_safe_stream_handler_falls_back_on_invalid_handle`
  and `tests/test_smoke.py::test_rapidocr_maps_result_to_normalized_words` —
  also pre-existing, the latter due to the same missing `numpy`.

None of these are in `deskmate/zaza/` or `tests/zaza_agent/`.

## Blocking item

**Awaiting your review of Phase 9** before Phase 10 begins. Before approval,
run `installer\smoke-test.ps1` on a disposable Windows VM where unsigned
builds may run (see Phase 9 risks). The production VPS, Caddy, DNS and
PostgreSQL have **not** been touched.

## Explicitly cancelled from any earlier direction

- Screenshot recording/storage
- Google Drive screenshot storage (or any Google Drive integration)
- Audio recording, webcam/video recording, keylogging, clipboard collection

None of the above exist in the current specification or in the Phase 1
implementation, and none should be reintroduced without a new, explicit
requirements change.
