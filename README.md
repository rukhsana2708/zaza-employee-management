# ZaZa Employee Management System

**ZaZa Employee Management** is an **activity-based remote employee work
monitoring and reporting system**, currently under active development. It
tracks activity *metadata* (application in use, window title, keyboard/mouse
presence, idle state, lock/unlock) on company Windows machines, aggregates it
centrally, and reports it to managers through PostgreSQL-backed summaries, a
live Google Sheets workbook, and a web dashboard.

This is **transparent workplace monitoring** — employees know what is
tracked, and the system is built around a strict rule: **it tracks activity,
not content.**

## What this system does NOT do

- ❌ It does **not** collect screenshots.
- ❌ It does **not** collect OCR or any other screen/visual content.
- ❌ It does **not** record typed text or keystroke contents (no keylogging).
- ❌ It does **not** collect clipboard contents.
- ❌ It does **not** record audio or webcam/video.

No code path in this project does any of the above — not hidden, not
feature-flagged off, simply not implemented. See [SECURITY.md](SECURITY.md)
for the full data policy.

## What this system does

- ✅ Tracks activity metadata only: active application, active window title,
  keyboard/mouse activity **presence** (boolean, never content), idle status,
  computer lock/unlock, and work session timing.
- ✅ **PostgreSQL is the central source of truth.** All employee, device,
  schedule, session, and summary data lives there.
- ✅ **Google Sheets is a live reporting layer**, not a database — summarized
  activity and attendance data is synced to it automatically for quick
  manager visibility, modeled on the existing "Working Time Tracker" workbook.
- ✅ A **manager web dashboard** (in development) will provide KPIs, daily
  timelines, employee/date filters, interactive charts, and deterministic
  attendance/activity analysis — labeled **Active Hours**, never "actual work
  time," since computer activity is a proxy for work, not proof of it.

Full design: [PROJECT_PLAN.md](PROJECT_PLAN.md) (scope & phased roadmap),
[ARCHITECTURE.md](ARCHITECTURE.md) (system design), and
[SECURITY.md](SECURITY.md) (privacy & data policy).

## Project status

**Under development.** See [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md)
for the current phase and what's implemented so far. As of this writing:
- **Approved:**
  - Phases 1–2: the privacy-safe Windows activity agent and its local SQLite
    storage, in `deskmate/zaza/`.
  - Phase 3: a versioned sync API and the agent's sync client.
  - Phase 4: PostgreSQL central storage.
- **Implemented, awaiting review:** Phase 5, deterministic attendance and
  work-time calculations in `deskmate/zaza_server/attendance/`. It produces
  daily, weekly and monthly summaries in PostgreSQL. Every formula is
  documented in ARCHITECTURE.md §4.11.

PostgreSQL has only been tested locally. Nothing is deployed to the VPS yet.
There is no Google Sheets sync and no dashboard yet.

## Derived from DeskMate — attribution

The Windows activity agent in this project is derived from
**[DeskMate](docs/UPSTREAM_DESKMATE_README.md)**, an existing open-source,
local-first desktop activity recorder, reused here for its low-level Windows
capture plumbing (foreground window detection, input hooks, idle detection).

DeskMate is **MIT licensed** — see [LICENSE](LICENSE). The original DeskMate
copyright notice and license terms remain in this repository and apply to
the DeskMate-derived portions of the code.

**Important distinction:** DeskMate itself is a general-purpose product that
*does* capture screenshots, OCR text, and clipboard content, and *can*
capture audio. ZaZa Employee Management is a separate, narrower derivative
that deliberately does **not** use, import, or enable any of that — see
`deskmate/zaza/` for the isolated agent module, and
[docs/UPSTREAM_DESKMATE_README.md](docs/UPSTREAM_DESKMATE_README.md) for
DeskMate's own (unmodified) documentation, preserved for reference.

## Repository layout

```
deskmate/zaza_server/ # central sync API (FastAPI) + repositories: in-memory,
                      # SQLite (development), PostgreSQL (postgres/, Phase 4)
deskmate/zaza/      # ZaZa activity agent (Phases 1-3) — the only code this
                     # project currently runs; see deskmate/zaza/README
                     # docstrings and the Phase 1 section of DEVELOPMENT_STATUS.md
deskmate/            # upstream DeskMate source, left in place for comparison
                     # (NOT used by, or part of, ZaZa Employee Management)
docs/                # upstream DeskMate technical docs + UPSTREAM_DESKMATE_README.md
tests/zaza_agent/    # automated tests for the ZaZa agent
PROJECT_PLAN.md       # scope, phased roadmap, V1 definition
ARCHITECTURE.md       # system architecture (agent, API, PostgreSQL, Sheets, dashboard)
SECURITY.md           # what is/isn't collected, data flow, access control
DEVELOPMENT_STATUS.md # current phase and progress
```

## Running the agent locally (developer/manual test)

```powershell
.\.venv\Scripts\Activate.ps1
python -m deskmate.zaza
```

This starts the local agent and a work session, prints activity events to
the console as they occur, and writes them to a local SQLite file
(`~/.zaza_agent/activity.db`, WAL mode; `ZAZA_HOME` overrides the folder).
Press `Ctrl+C` to stop. This closes the session and prints today's
application usage. There is no network activity; this is entirely local.

To see what is stored (recent sessions, today's activity periods, idle
periods, and app usage) without starting the agent:

```powershell
python -m deskmate.zaza --report
```

Optional environment variables: `ZAZA_EMPLOYEE_ID`, `ZAZA_DEVICE_ID`,
`ZAZA_IDLE_THRESHOLD_SECONDS` (default 300), `ZAZA_TICK_SECONDS` (10),
`ZAZA_TITLE_DEBOUNCE_SECONDS` (30), `ZAZA_RAW_RETENTION_DAYS` (7),
`ZAZA_SYNCED_RETENTION_DAYS` (30), and the privacy exclusions
`ZAZA_EXCLUDED_APPS`, `ZAZA_HIDDEN_APPS`, `ZAZA_EXCLUDED_DOMAINS`
(comma-separated, e.g. `ZAZA_EXCLUDED_APPS=KeePassXC.exe,1Password.exe`).
See SECURITY.md §2.3.

## Trying synchronization locally (development only)

This runs a development "central server" and the agent on the same PC, then
shows that data still arrives after the server has been down for a while.
Everything stays on this computer. Each `$env:...` line only applies to the
PowerShell window you type it in.

**Window 1 — the development server**

```powershell
cd D:\Projects\zaza-employee-management
.\.venv\Scripts\Activate.ps1
python -m deskmate.zaza_server add-employee --employee-id alice --name "Alice Example"
python -m deskmate.zaza_server register-device --device-id laptop-01 --employee-id alice
```

A device always belongs to an existing employee, so add the employee first.
`register-device` prints a long token starting with `zzd_`. Copy it; it is shown only
once. Then start the server and leave the window open:

```powershell
python -m deskmate.zaza_server serve
```

**Window 2 — the employee agent**

```powershell
cd D:\Projects\zaza-employee-management
.\.venv\Scripts\Activate.ps1
$env:ZAZA_DEVICE_ID="laptop-01"; $env:ZAZA_EMPLOYEE_ID="alice"
$env:ZAZA_SYNC_URL="http://127.0.0.1:8765"; $env:ZAZA_SYNC_INTERVAL_SECONDS="15"
python -m deskmate.zaza set-credentials
```

Paste the token when asked (nothing appears while you paste; that's
normal), then press Enter. It should say *"Server accepted the token"*.
Now start the agent:

```powershell
python -m deskmate.zaza
```

**Create some activity and check that it arrived**

1. Use the PC normally for 1–2 minutes and switch between a few apps. In
   Window 2, `[sync]` lines appear, ending in `sync HEALTHY`.
2. To sync right away instead of waiting, open **Window 3**, run the same
   `cd`, `Activate.ps1` and the two `$env:` lines as Window 2, then run
   `python -m deskmate.zaza sync-now`.
3. Check what the server received:
   `python -m deskmate.zaza_server records`. You should see
   `work_session`, `activity_period` and `app_usage_daily` records for
   `laptop-01`.

**Server outage and catch-up**

4. In Window 1, press `Ctrl+C` to stop the server.
5. Keep using the PC for a few minutes. Window 2 shows `sync OFFLINE`, and
   the agent keeps recording. `python -m deskmate.zaza sync-status` in
   Window 3 shows `pending_count` going up.
6. Start the server again in Window 1: `python -m deskmate.zaza_server serve`.
7. Within about 5 minutes at most (the retry delay grows while the server is
   down), Window 2 shows `sync HEALTHY` again. You can also run `sync-now`
   in Window 3 to catch up immediately.
8. Run `python -m deskmate.zaza_server records` again. The total has grown,
   and `sync-status` shows `pending_count 0`.

Press `Ctrl+C` in Window 2 to stop the agent; it sends the closed session
before exiting. To reset everything, delete `%USERPROFILE%\.zaza_server_dev`
and `%USERPROFILE%\.zaza_agent`.

## Trying the PostgreSQL storage locally (optional, for developers)

The steps above use the simple SQLite development store. The production
store is PostgreSQL. To try it, you need a PostgreSQL server **you control
on your own PC**.

> **Do not** point this at the production VPS database. Creating the real
> database there is a separate, later step (Phase 10).

1. Install the PostgreSQL extras once:
   `pip install -e .[zaza-postgres]`
2. Create an empty database and a login role for it, e.g. `zaza_dev` owned by
   `zaza_dev_owner`. Use pgAdmin or `psql`.
3. In **Window 1**, tell the server to use it. The password is read from
   the environment and is never printed:

   ```powershell
   $env:ZAZA_SERVER_BACKEND="postgres"
   $env:ZAZA_DB_HOST="127.0.0.1"; $env:ZAZA_DB_NAME="zaza_dev"; $env:ZAZA_DB_USER="zaza_dev_owner"
   $env:ZAZA_DB_PASSWORD="(your password)"
   python -m deskmate.zaza_server db-status   # shows "(empty database)"
   python -m deskmate.zaza_server migrate     # creates the tables; safe to run again
   ```

4. Continue exactly as in the section above, in the same window:
   `add-employee`, `register-device`, `serve`. The agent steps (Window 2)
   are unchanged; the agent does not know which store the server uses.
5. `python -m deskmate.zaza_server records` and `list-devices` now read from
   PostgreSQL. `list-devices` also shows when each device last connected.

The server refuses to start on a database that hasn't been migrated, and it
never changes the schema by itself. All variables are listed in
`.env.example` and in ARCHITECTURE.md §4.1.

### Attendance summaries (Phase 5, PostgreSQL only)

With the PostgreSQL settings from the section above, in the same window:

1. Give the employee a schedule. Weekdays use ISO numbers (1 = Monday). A
   shift that ends earlier than it starts runs overnight, and belongs to the
   day it starts. Schedules always use the **employee's** timezone (set with
   `add-employee --timezone`); a different `--timezone` is rejected.
   Weekly rules start today in the employee's timezone unless you give
   `--effective-from`.

   ```powershell
   python -m deskmate.zaza_server add-schedule --employee-id alice --weekdays 1-5 --start 09:00 --end 17:00 --expected-hours 8
   python -m deskmate.zaza_server add-schedule --employee-id alice --weekdays 6,7 --day-off
   python -m deskmate.zaza_server list-schedules --employee-id alice
   ```

2. Let the agent sync some activity, as in the steps above.
3. Calculate the summaries. Each line shows the status, the hours,
   late/early/overtime, Attendance % and data quality:

   ```powershell
   python -m deskmate.zaza_server summarize-day --date 2026-10-08
   python -m deskmate.zaza_server summarize-week --date 2026-10-08
   python -m deskmate.zaza_server summarize-month --month 2026-10
   python -m deskmate.zaza_server recalculate --recent-days 7
   ```

`--recent-days 7` means today and the 6 days before it, in each
employee's own timezone. Running a command again updates the same rows and
never duplicates them.
Today's figures are marked *(provisional)* until the day is over. What every
number means is in ARCHITECTURE.md §4.11.

**Automated PostgreSQL tests (opt-in).** The normal `pytest tests/zaza_agent`
run does not touch any PostgreSQL. To also run the PostgreSQL tests, point
them at a **disposable** database whose name contains `test`. They create
and drop their own `zaza_pytest*` schemas and touch nothing else:

```powershell
$env:ZAZA_TEST_POSTGRES_URL="postgresql://user:password@127.0.0.1:5432/zaza_test"
pytest tests/zaza_agent -q
```

At startup it prints a component health table (keyboard hook, mouse hook,
session-lock watcher, foreground-window watcher, SQLite storage — each
`HEALTHY`, `DEGRADED`, or `ERROR`) and prints a `[health]` line whenever a
component changes state. If a keyboard or mouse hook fails to install, the
agent shows `ERROR` and records idle status as unknown rather than reporting
the employee as idle.

## License

**MIT** — see [LICENSE](LICENSE). This project is free to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software,
subject to the MIT license terms, consistent with its DeskMate origins.
