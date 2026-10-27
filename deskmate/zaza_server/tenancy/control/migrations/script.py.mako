"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

CONTROL-PLANE migration (separate chain from the tenant schema). Revision ids
start with ``c``. Rules: forward-only data preservation, named constraints,
TIMESTAMPTZ for every instant, no customer activity data, no secrets.
"""

from __future__ import annotations

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
