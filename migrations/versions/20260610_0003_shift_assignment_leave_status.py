"""allow leave approved shift assignments

Revision ID: 20260610_0003
Revises: 20260610_0002
Create Date: 2026-06-12
"""
from alembic import op


revision = "20260610_0003"
down_revision = "20260610_0002"
branch_labels = None
depends_on = None


NEW_STATUS_CHECK = "status IN ('scheduled', 'swapped', 'cancelled', 'leave_approved')"
OLD_STATUS_CHECK = "status IN ('scheduled', 'swapped', 'cancelled')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE shift_assignments DROP CONSTRAINT IF EXISTS ck_shift_assignments_status")
    else:
        op.drop_constraint("ck_shift_assignments_status", "shift_assignments", type_="check")
    op.create_check_constraint(
        "ck_shift_assignments_status",
        "shift_assignments",
        NEW_STATUS_CHECK,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE shift_assignments DROP CONSTRAINT IF EXISTS ck_shift_assignments_status")
    else:
        op.drop_constraint("ck_shift_assignments_status", "shift_assignments", type_="check")
    op.create_check_constraint(
        "ck_shift_assignments_status",
        "shift_assignments",
        OLD_STATUS_CHECK,
    )
