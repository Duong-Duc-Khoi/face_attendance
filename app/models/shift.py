"""
app/models/shift.py
Model ca làm việc và phân công ca cho nhân viên.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from app.models.base import Base


class Shift(Base):
    """
    Định nghĩa một ca làm việc (ca sáng, ca chiều, ca tối, ...).
    Mỗi ca có giờ bắt đầu/kết thúc và ngưỡng đi muộn riêng.
    """
    __tablename__ = "shifts"

    id          = Column(Integer, primary_key=True, index=True)
    branch_id   = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)
    name        = Column(String(100), nullable=False)        # "Ca sáng", "Ca chiều"
    code        = Column(String(20),  index=True, nullable=False)  # "morning", "afternoon"
    work_start  = Column(String(5),   nullable=False)        # "08:00"
    work_end    = Column(String(5),   nullable=False)        # "12:00"
    required_position = Column(String(100), default="")      # VD: "Đầu bếp", "Phục vụ"
    late_threshold_minutes = Column(Integer, default=15)     # phút trễ cho phép
    early_checkin_minutes  = Column(Integer, default=30)     # cho phép vào sớm trước ca
    auto_checkout_minutes  = Column(Integer, default=180)    # cửa sổ ra sau ca, dùng auto/match ca
    break_minutes          = Column(Integer, default=0)      # nghỉ giữa ca không tính công
    is_overnight           = Column(Boolean, default=False)  # ca qua ngày, vd 22:00-06:00
    is_active   = Column(Boolean, default=True)
    note        = Column(String(255), default="")
    created_at  = Column(DateTime, default=datetime.now)
    updated_at  = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("branch_id", "code", name="uq_shift_branch_code"),
    )


class ShiftAssignment(Base):
    """
    Phân công ca cho nhân viên theo ngày cụ thể.
    Một nhân viên có thể có ca khác nhau mỗi ngày.
    Nếu không có assignment cho ngày đó → fallback về Shift mặc định của phòng ban
    hoặc cấu hình WORK_START/WORK_END trong .env.
    """
    __tablename__ = "shift_assignments"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    branch_id   = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)
    emp_code    = Column(String(20), index=True, nullable=False)
    shift_id    = Column(Integer, ForeignKey("shifts.id"), index=True, nullable=False)
    work_date   = Column(Date, index=True, nullable=False)
    status      = Column(String(20), default="scheduled", index=True)  # scheduled | swapped | cancelled
    note        = Column(String(255), default="")
    assigned_by = Column(String(150), default="")
    assigned_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at  = Column(DateTime, default=datetime.now)
    updated_at  = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        # Nhà hàng cho phép một nhân viên có nhiều ca trong ngày,
        # nhưng không phân cùng một ca lặp lại trong cùng ngày.
        UniqueConstraint("emp_code", "work_date", "shift_id", name="uq_emp_date_shift"),
    )


class ShiftPlanDraft(Base):
    """
    Bản nháp phân ca do AI/thuật toán đề xuất. Quản lý xem và duyệt trước khi
    đẩy sang shift_assignments thật.
    """
    __tablename__ = "shift_plan_drafts"

    id          = Column(Integer, primary_key=True, index=True)
    branch_id   = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)
    from_date   = Column(Date, nullable=False, index=True)
    to_date     = Column(Date, nullable=False, index=True)
    status      = Column(String(20), default="draft", index=True)  # draft | applied
    source      = Column(String(20), default="heuristic")          # openai | heuristic
    prompt      = Column(Text, default="")
    summary     = Column(Text, default="")
    warnings    = Column(Text, default="[]")
    created_by  = Column(String(150), default="")
    applied_by  = Column(String(150), default="")
    created_at  = Column(DateTime, default=datetime.now)
    applied_at  = Column(DateTime, nullable=True)


class ShiftPlanDraftAssignment(Base):
    __tablename__ = "shift_plan_draft_assignments"

    id          = Column(Integer, primary_key=True, index=True)
    draft_id    = Column(Integer, ForeignKey("shift_plan_drafts.id"), nullable=False, index=True)
    emp_code    = Column(String(20), index=True, nullable=False)
    shift_id    = Column(Integer, ForeignKey("shifts.id"), index=True, nullable=False)
    work_date   = Column(Date, index=True, nullable=False)
    reason      = Column(Text, default="")
    validation_status = Column(String(20), default="valid")
    created_at  = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("draft_id", "emp_code", "work_date", "shift_id", name="uq_shift_plan_draft_item"),
    )
