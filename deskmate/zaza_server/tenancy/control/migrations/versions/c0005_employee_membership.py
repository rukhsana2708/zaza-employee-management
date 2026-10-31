"""Phase A: employee self-service membership. No workforce data in control."""

from alembic import op

revision = "c0005_employee_membership"
down_revision = "c0004_sync_rate_limits"
branch_labels = None
depends_on = None

# Monitoring-only predecessor support, explicitly reviewed (not transitive).
compatible_previous_revisions = {
    "c0004_sync_rate_limits": frozenset({"off", "shadow", "enforce"}),
    "c0003_device_enrollment": frozenset({"off"}),
}


def upgrade():
    op.execute(
        "ALTER TABLE organization_memberships DROP CONSTRAINT organization_memberships_role_valid"
    )
    op.execute(
        "ALTER TABLE organization_memberships ADD CONSTRAINT organization_memberships_role_valid "
        "CHECK (role IN ('OWNER', 'ADMIN', 'MANAGER', 'VIEWER', 'EMPLOYEE'))"
    )


def downgrade():
    # Refuse rather than deleting/reclassifying members to satisfy the old CHECK.
    op.execute(
        "ALTER TABLE organization_memberships DROP CONSTRAINT organization_memberships_role_valid"
    )
    op.execute(
        "ALTER TABLE organization_memberships ADD CONSTRAINT organization_memberships_role_valid "
        "CHECK (role IN ('OWNER', 'ADMIN', 'MANAGER', 'VIEWER'))"
    )
