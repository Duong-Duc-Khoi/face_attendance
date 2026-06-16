"""allow missing check-in attendance session reviews

Revision ID: 20260616_0006
Revises: 20260610_0005
Create Date: 2026-06-16
"""
from alembic import op


revision = "20260616_0006"
down_revision = "20260610_0005"
branch_labels = None
depends_on = None


REVIEW_TYPE_NEW = "review_type IN ('', 'absent', 'missing_checkout', 'missing_checkin', 'overtime', 'unscheduled')"
REVIEW_TYPE_OLD = "review_type IN ('', 'absent', 'missing_checkout', 'overtime', 'unscheduled')"


def _drop_constraint(name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE attendance_sessions DROP CONSTRAINT IF EXISTS {name}")
    else:
        op.drop_constraint(name, "attendance_sessions", type_="check")


def upgrade() -> None:
    _drop_constraint("ck_attendance_sessions_review_type")
    op.create_check_constraint(
        "ck_attendance_sessions_review_type",
        "attendance_sessions",
        REVIEW_TYPE_NEW,
    )


def downgrade() -> None:
    _drop_constraint("ck_attendance_sessions_review_type")
    op.create_check_constraint(
        "ck_attendance_sessions_review_type",
        "attendance_sessions",
        REVIEW_TYPE_OLD,
    )
