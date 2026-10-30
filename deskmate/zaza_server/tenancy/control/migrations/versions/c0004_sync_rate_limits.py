"""Sync API rate limiting (control plane c0004).

Revision ID: c0004_sync_rate_limits
Revises: c0003_device_enrollment

One additive change: the shared counters in ``login_attempt_counters`` gain the bucket ``sync_device``, the per-device
fixed-window counter of the sync API limiter (120 requests per aligned 60 s window, keyed by a 64-hex digest of the
organization and device; see docs/zaza/SYNC_RATE_LIMIT_HSTS_DESIGN.md). Nothing else changes: no new table, no data
migration, the existing ``identity``, ``global``, ``enrollment_code`` and ``enrollment_global`` buckets stay valid.

The pattern is the one ``c0003`` used for the enrollment buckets: drop and re-add the CHECK constraint in one transaction.
The table holds only short-lived rows, so the brief lock is harmless.

Downgrade removes the new bucket's rows first, then restores the c0003 constraint.
"""

from __future__ import annotations

from alembic import op

revision = "c0004_sync_rate_limits"
down_revision = "c0003_device_enrollment"
branch_labels = None
depends_on = None

PREVIOUS_BUCKETS = ("identity", "global", "enrollment_code", "enrollment_global")
NEW_BUCKETS = ("sync_device",)


def _list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("ALTER TABLE login_attempt_counters DROP CONSTRAINT login_attempt_counters_bucket_valid")
    op.execute("ALTER TABLE login_attempt_counters ADD CONSTRAINT login_attempt_counters_bucket_valid CHECK "
               f"(bucket IN ({_list(PREVIOUS_BUCKETS + NEW_BUCKETS)}))")


def downgrade() -> None:
    op.execute("DELETE FROM login_attempt_counters WHERE bucket = 'sync_device'")
    op.execute("ALTER TABLE login_attempt_counters DROP CONSTRAINT login_attempt_counters_bucket_valid")
    op.execute("ALTER TABLE login_attempt_counters ADD CONSTRAINT login_attempt_counters_bucket_valid CHECK "
               f"(bucket IN ({_list(PREVIOUS_BUCKETS)}))")
