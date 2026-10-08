"""Phase 7 — the manager web dashboard (server-rendered, under /manager).

PostgreSQL is the only data source (never Google Sheets). Historical figures
are the stored Phase 5 summaries; current status comes from devices and the
latest activity periods. Managers sign in with Argon2id-hashed accounts and
server-side sessions; every change needs the session's CSRF token.

- ``config.py``    environment settings
- ``security.py``  passwords, tokens, usernames, security headers
- ``models.py``    plain data
- ``queries.py``   PostgreSQL (and in-memory test) data access
- ``auth.py``      accounts, login, sessions
- ``service.py``   period filters, KPIs, current status, schedules
- ``routes.py``    FastAPI routes and templates (``templates/``, ``static/``)

Requires the optional extra ``zaza-dashboard`` (Jinja2, argon2-cffi). No Google
library is imported. Details: ARCHITECTURE.md §6.
"""
