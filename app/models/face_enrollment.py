"""
Face enrollment session model.

Used when a manager starts a face registration/update from the dashboard and
the kiosk completes it with a short-lived ticket.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.models.base import Base


class FaceEnrollmentSession(Base):
    __tablename__ = "face_enrollment_sessions"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    action = Column(String(30), nullable=False, index=True)
    status = Column(String(20), default="pending", index=True)
    kiosk_id = Column(String(80), default="", index=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
