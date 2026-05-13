"""
app/models/employee.py
SQLAlchemy model cho nhân viên.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String
from app.models.base import Base


class Employee(Base):
    __tablename__ = "employees"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True, unique=True, index=True)
    branch_id    = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)
    emp_code     = Column(String(20),  unique=True, index=True, nullable=False)
    name         = Column(String(100), nullable=False)
    full_name    = Column(String(100), default="")
    department   = Column(String(100), default="")
    position     = Column(String(100), default="")
    email        = Column(String(150), default="")
    phone        = Column(String(20),  default="")
    face_path    = Column(String(255), default="")
    avatar_url   = Column(String(255), default="")
    hire_date    = Column(Date, nullable=True)
    status       = Column(String(20), default="active", index=True)  # active | inactive | terminated
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.now)
    updated_at   = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    deactivated_at = Column(DateTime, nullable=True, default=None)
