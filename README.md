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
Phases 1–2 (the privacy-safe Windows activity agent and its local SQLite
storage, in `deskmate/zaza/`) are approved. Phase 3 (synchronization: a
versioned sync API with a *development* server in `deskmate/zaza_server/`,
and the agent's sync client) is implemented and awaiting review. There is
no production server, no PostgreSQL integration, no Google Sheets sync, and
no dashboard yet.

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
deskmate/zaza_server/ # Phase 3 development sync API (FastAPI; dev storage only)
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
python -m deskmate.zaza_server register-device --device-id laptop-01 --employee-id alice
```

This prints a long token starting with `zzd_`. Copy it; it is shown only
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
