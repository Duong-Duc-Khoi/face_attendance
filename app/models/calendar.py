"""
app/models/calendar.py
Model lịch làm việc — ghi đè ngày đặc biệt so với mặc định.
"""

from datetime import datetime
from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from app.models.base import Base


class WorkCalendar(Base):
    __tablename__ = "work_calendar"

    id         = Column(Integer, primary_key=True, index=True)
    branch_id  = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True)
    date       = Column(Date, index=True, nullable=False)

    # full | off | holiday
    day_type   = Column(String(20), nullable=False)

    # Nhãn hiển thị: "Tết Nguyên Đán", "Nghỉ bảo trì", ...
    label      = Column(String(200), default="")
    pay_multiplier = Column(Numeric(5, 2), default=1.0)
    salary_note = Column(String(255), default="")

    created_by = Column(String(150), default="")
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("branch_id", "date", name="uq_calendar_branch_date"),
    )


class WorkCalendarConfig(Base):
    __tablename__ = "work_calendar_configs"

    id         = Column(Integer, primary_key=True, index=True)
    branch_id  = Column(Integer, ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    work_days  = Column(String(30), nullable=False, default="1,2,3,4,5,6,7")

    created_by = Column(String(150), default="")
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
