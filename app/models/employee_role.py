"""
Restaurant operating roles for employees.

These roles describe what a person does in the restaurant. They are separate
from User.role, which controls system permissions.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.models.base import Base


class EmployeeRole(Base):
    __tablename__ = "employee_roles"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255), default="")
    sort_order = Column(Integer, default=0, index=True)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
