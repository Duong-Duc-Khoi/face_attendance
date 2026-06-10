"""attendance audit and period locks

Revision ID: 20260610_0001
Revises:
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa


revision = "20260610_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attendance_correction_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("log_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("emp_code", sa.String(length=20), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_data", sa.Text(), nullable=True),
        sa.Column("after_data", sa.Text(), nullable=True),
        sa.Column("affected_session_ids", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=150), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["log_id"], ["attendance_logs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["attendance_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attendance_correction_audits_action", "attendance_correction_audits", ["action"])
    op.create_index("ix_attendance_correction_audits_branch_id", "attendance_correction_audits", ["branch_id"])
    op.create_index("ix_attendance_correction_audits_created_at", "attendance_correction_audits", ["created_at"])
    op.create_index("ix_attendance_correction_audits_created_by_id", "attendance_correction_audits", ["created_by_id"])
    op.create_index("ix_attendance_correction_audits_emp_code", "attendance_correction_audits", ["emp_code"])
    op.create_index("ix_attendance_correction_audits_employee_id", "attendance_correction_audits", ["employee_id"])
    op.create_index("ix_attendance_correction_audits_id", "attendance_correction_audits", ["id"])
    op.create_index("ix_attendance_correction_audits_log_id", "attendance_correction_audits", ["log_id"])
    op.create_index("ix_attendance_correction_audits_session_id", "attendance_correction_audits", ["session_id"])
    op.create_index("ix_attendance_correction_audits_status", "attendance_correction_audits", ["status"])

    op.create_table(
        "attendance_period_locks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("locked_by", sa.String(length=150), nullable=True),
        sa.Column("locked_by_id", sa.Integer(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("unlocked_by", sa.String(length=150), nullable=True),
        sa.Column("unlocked_by_id", sa.Integer(), nullable=True),
        sa.Column("unlocked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["locked_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["unlocked_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attendance_period_locks_branch_id", "attendance_period_locks", ["branch_id"])
    op.create_index("ix_attendance_period_locks_from_date", "attendance_period_locks", ["from_date"])
    op.create_index("ix_attendance_period_locks_id", "attendance_period_locks", ["id"])
    op.create_index("ix_attendance_period_locks_is_active", "attendance_period_locks", ["is_active"])
    op.create_index("ix_attendance_period_locks_locked_at", "attendance_period_locks", ["locked_at"])
    op.create_index("ix_attendance_period_locks_locked_by_id", "attendance_period_locks", ["locked_by_id"])
    op.create_index("ix_attendance_period_locks_to_date", "attendance_period_locks", ["to_date"])
    op.create_index("ix_attendance_period_locks_unlocked_by_id", "attendance_period_locks", ["unlocked_by_id"])


def downgrade() -> None:
    op.drop_index("ix_attendance_period_locks_unlocked_by_id", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_to_date", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_locked_by_id", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_locked_at", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_is_active", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_id", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_from_date", table_name="attendance_period_locks")
    op.drop_index("ix_attendance_period_locks_branch_id", table_name="attendance_period_locks")
    op.drop_table("attendance_period_locks")

    op.drop_index("ix_attendance_correction_audits_status", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_session_id", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_log_id", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_id", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_employee_id", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_emp_code", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_created_by_id", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_created_at", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_branch_id", table_name="attendance_correction_audits")
    op.drop_index("ix_attendance_correction_audits_action", table_name="attendance_correction_audits")
    op.drop_table("attendance_correction_audits")
