"""
app/models/leave.py
Model đơn xin nghỉ / remote work.
"""

import json
from datetime import datetime
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text
from app.models.base import Base


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id           = Column(Integer, primary_key=True, index=True)
    employee_id  = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    emp_code     = Column(String(20),  index=True, nullable=False)
    emp_name     = Column(String(100), default="")
    department   = Column(String(100), default="")
    emp_email    = Column(String(150), default="")

    # "leave" = nghỉ phép | "remote" = làm remote
    request_type = Column(String(20), default="leave")

    # JSON list: [{"date":"2026-05-12","half":null},{"date":"2026-05-13","half":"am"}]
    # half: null = cả ngày | "am" = buổi sáng | "pm" = buổi chiều
    dates_json   = Column(Text, nullable=False)

    reason       = Column(Text, default="")

    # pending | approved | rejected | cancelled
    status       = Column(String(20), default="pending", index=True)

    submitted_at = Column(DateTime, default=datetime.now, index=True)
    reviewed_at  = Column(DateTime, nullable=True)
    reviewed_by  = Column(String(150), nullable=True)   # email người duyệt
    reviewed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    note         = Column(Text, default="")             # ghi chú khi duyệt/từ chối
    created_at   = Column(DateTime, default=datetime.now)
    updated_at   = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # ── Helpers ──────────────────────────────────────────────────
    def get_dates(self) -> list[dict]:
        """Trả về list [{"date": "2026-05-12", "half": null}, ...]"""
        try:
            return json.loads(self.dates_json)
        except Exception:
            return []

    def set_dates(self, dates: list[dict]):
        self.dates_json = json.dumps(dates, ensure_ascii=False)

    def date_strings(self) -> list[str]:
        """Trả về list ngày thuần ["2026-05-12", ...]"""
        return [d["date"] for d in self.get_dates()]

    def total_days(self) -> float:
        """Tổng ngày công bị ảnh hưởng (nửa ngày = 0.5)"""
        total = 0.0
        for d in self.get_dates():
            total += 0.5 if d.get("half") in ("am", "pm") else 1.0
        return total


class LeaveRequestDay(Base):
    """
    Bảng chuẩn hóa các ngày xin nghỉ.
    Giữ dates_json trong LeaveRequest để tương thích UI/API cũ, nhưng dữ liệu mới
    có thể ghi thêm vào bảng này để query/report dễ hơn.
    """
    __tablename__ = "leave_request_days"

    id               = Column(Integer, primary_key=True, index=True)
    leave_request_id = Column(Integer, ForeignKey("leave_requests.id"), nullable=False, index=True)
    date             = Column(Date, nullable=False, index=True)
    half_day         = Column(String(10), nullable=True)  # null | am | pm
