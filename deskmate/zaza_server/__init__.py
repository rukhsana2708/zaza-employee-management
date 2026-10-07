"""ZaZa central sync API — Phase 3 development server.

FastAPI app (``app.py``) → :class:`SyncService` (``service.py``) →
:class:`CentralRepository` (``repository.py``). Phase 3 ships an in-memory
repository (tests) and a SQLite development repository; Phase 4 adds a
PostgreSQL implementation of the same interface without changing the wire
protocol (``deskmate.zaza.sync.protocol``).

Not for production yet: no PostgreSQL, no deployment, HTTP allowed only for
local development.
"""
