"""ZaZa central sync API.

FastAPI app (``app.py``) → :class:`SyncService` (``service.py``) →
:class:`CentralRepository` (``repository.py``), with three backends behind
the same interface:

- in-memory (tests) and SQLite (local development) in ``repository.py``;
- PostgreSQL (production, Phase 4) in ``postgres/``, selected with
  ``ZAZA_SERVER_BACKEND=postgres`` (see ``config.py`` and ``backends.py``).

The wire protocol (``deskmate.zaza.sync.protocol``) is the same for all of
them. Not deployed yet: production deployment is Phase 10, and plain HTTP
is accepted only for local development.
"""
