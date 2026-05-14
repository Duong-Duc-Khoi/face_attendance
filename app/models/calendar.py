"""
app/models/calendar.py
Model lịch làm việc — ghi đè ngày đặc biệt so với mặc định.
"""

from datetime import datetime
from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from app.models.base import Base


class WorkCalendar(Base):
    __tablename__ = "work_calendar"

    id         = Column(Integer, primary_key=True, index=True)
    branch_id  = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)
    date       = Column(Date, index=True, nullable=False)

    # full | half_am | half_pm | off | holiday | overtime | closed | special_open
    day_type   = Column(String(20), nullable=False)

    # Override giờ vào/ra so với config mặc định (nullable = dùng mặc định)
    work_start = Column(String(5), nullable=True)   # "08:00"
    work_end   = Column(String(5), nullable=True)   # "17:00"

    # Nhãn hiển thị: "Tết Nguyên Đán", "Làm bù thứ 7", ...
    label      = Column(String(200), default="")

    created_by = Column(String(150), default="")
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("branch_id", "date", name="uq_calendar_branch_date"),
    )
