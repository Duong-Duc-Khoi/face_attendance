"""
app/models/attendance.py
SQLAlchemy model cho log chấm công.
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, Float, Integer, String, Text
from app.models.base import Base


class AttendanceLog(Base):
    __tablename__ = "attendance_logs"

    id           = Column(Integer, primary_key=True, index=True)
    employee_id  = Column(Integer, index=True)
    emp_code     = Column(String(20),  index=True)
    emp_name     = Column(String(100), default="")
    department   = Column(String(100), default="")
    check_type   = Column(String(20))                # "check_in" | "check_out"
    timestamp    = Column(DateTime, default=datetime.now, index=True)
    confidence   = Column(Float,   default=0.0)
    capture_path = Column(String(255), default="")
    note         = Column(Text, default="")
