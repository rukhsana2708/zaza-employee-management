"""PostgreSQL persistence for the ZaZa central server (Phase 4).

- :class:`PostgresRepository` implements
  :class:`deskmate.zaza_server.repository.CentralRepository` on typed,
  constrained tables, with a small psycopg connection pool.
- :mod:`.migrate` manages the schema with Alembic; revisions live in
  ``migrations/versions``.

Requires the ``postgres`` extra: ``pip install -e .[zaza-postgres]``.
"""

from .repository import PostgresRepository

__all__ = ["PostgresRepository"]
