"""allow unscheduled attendance session status values

Revision ID: 20260610_0004
Revises: 20260610_0003
Create Date: 2026-06-16
"""
from alembic import op


revision = "20260610_0004"
down_revision = "20260610_0003"
branch_labels = None
depends_on = None


CHECK_IN_NEW = "check_in_status IN ('', 'on_time', 'late', 'early', 'manual', 'auto', 'unscheduled')"
CHECK_IN_OLD = "check_in_status IN ('', 'on_time', 'late', 'early', 'manual', 'auto')"
CHECK_OUT_NEW = "check_out_status IN ('', 'normal', 'early_leave', 'overtime', 'manual', 'auto', 'unscheduled')"
CHECK_OUT_OLD = "check_out_status IN ('', 'normal', 'early_leave', 'overtime', 'manual', 'auto')"
REVIEW_TYPE_NEW = "review_type IN ('', 'absent', 'missing_checkout', 'overtime', 'unscheduled')"
REVIEW_TYPE_OLD = "review_type IN ('', 'absent', 'missing_checkout', 'overtime')"


def _drop_constraint(name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE attendance_sessions DROP CONSTRAINT IF EXISTS {name}")
    else:
        op.drop_constraint(name, "attendance_sessions", type_="check")


def upgrade() -> None:
    _drop_constraint("ck_attendance_sessions_check_in_status")
    _drop_constraint("ck_attendance_sessions_check_out_status")
    _drop_constraint("ck_attendance_sessions_review_type")
    op.create_check_constraint(
        "ck_attendance_sessions_check_in_status",
        "attendance_sessions",
        CHECK_IN_NEW,
    )
    op.create_check_constraint(
        "ck_attendance_sessions_check_out_status",
        "attendance_sessions",
        CHECK_OUT_NEW,
    )
    op.create_check_constraint(
        "ck_attendance_sessions_review_type",
        "attendance_sessions",
        REVIEW_TYPE_NEW,
    )


def downgrade() -> None:
    _drop_constraint("ck_attendance_sessions_check_in_status")
    _drop_constraint("ck_attendance_sessions_check_out_status")
    _drop_constraint("ck_attendance_sessions_review_type")
    op.create_check_constraint(
        "ck_attendance_sessions_check_in_status",
        "attendance_sessions",
        CHECK_IN_OLD,
    )
    op.create_check_constraint(
        "ck_attendance_sessions_check_out_status",
        "attendance_sessions",
        CHECK_OUT_OLD,
    )
    op.create_check_constraint(
        "ck_attendance_sessions_review_type",
        "attendance_sessions",
        REVIEW_TYPE_OLD,
    )
