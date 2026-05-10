"""
app/models/calendar.py
Model lịch làm việc — ghi đè ngày đặc biệt so với mặc định.
"""

from datetime import datetime
from sqlalchemy import Column, Date, DateTime, Integer, String
from app.models.base import Base


class WorkCalendar(Base):
    __tablename__ = "work_calendar"

    id         = Column(Integer, primary_key=True, index=True)
    date       = Column(Date, unique=True, index=True, nullable=False)

    # full | half_am | half_pm | off | holiday | overtime
    day_type   = Column(String(20), nullable=False)

    # Override giờ vào/ra so với config mặc định (nullable = dùng mặc định)
    work_start = Column(String(5), nullable=True)   # "08:00"
    work_end   = Column(String(5), nullable=True)   # "17:00"

    # Nhãn hiển thị: "Tết Nguyên Đán", "Làm bù thứ 7", ...
    label      = Column(String(200), default="")

    created_by = Column(String(150), default="")
    created_at = Column(DateTime, default=datetime.now)
