"""face enrollment sessions

Revision ID: 20260610_0002
Revises: 20260610_0001
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa


revision = "20260610_0002"
down_revision = "20260610_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "face_enrollment_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("kiosk_id", sa.String(length=80), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_face_enrollment_sessions_action", "face_enrollment_sessions", ["action"])
    op.create_index("ix_face_enrollment_sessions_branch_id", "face_enrollment_sessions", ["branch_id"])
    op.create_index("ix_face_enrollment_sessions_created_by_id", "face_enrollment_sessions", ["created_by_id"])
    op.create_index("ix_face_enrollment_sessions_employee_id", "face_enrollment_sessions", ["employee_id"])
    op.create_index("ix_face_enrollment_sessions_expires_at", "face_enrollment_sessions", ["expires_at"])
    op.create_index("ix_face_enrollment_sessions_id", "face_enrollment_sessions", ["id"])
    op.create_index("ix_face_enrollment_sessions_kiosk_id", "face_enrollment_sessions", ["kiosk_id"])
    op.create_index("ix_face_enrollment_sessions_status", "face_enrollment_sessions", ["status"])
    op.create_index("ix_face_enrollment_sessions_token_hash", "face_enrollment_sessions", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_face_enrollment_sessions_token_hash", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_status", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_kiosk_id", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_id", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_expires_at", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_employee_id", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_created_by_id", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_branch_id", table_name="face_enrollment_sessions")
    op.drop_index("ix_face_enrollment_sessions_action", table_name="face_enrollment_sessions")
    op.drop_table("face_enrollment_sessions")
