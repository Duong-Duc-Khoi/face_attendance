"""Branch membership history for employees."""

from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base


class EmployeeBranchHistory(Base):
    __tablename__ = "employee_branch_history"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    emp_code = Column(String(20), nullable=False, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True)
    effective_from = Column(Date, nullable=False, index=True, default=date(1970, 1, 1))
    effective_to = Column(Date, nullable=True, index=True)
    changed_by = Column(String(150), default="")
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
