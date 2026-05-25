"""
app/models/attendance.py
Models chấm công.

AttendanceSession/Event là schema mới cho nhà hàng:
  - 1 session = 1 nhân viên làm 1 ca
  - nhiều event = bằng chứng check-in/check-out/break/manual theo session

AttendanceLog được giữ lại để các màn hình/API cũ tiếp tục chạy trong giai
đoạn chuyển đổi.
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from app.models.base import Base


class AttendanceSession(Base):
    __tablename__ = "attendance_sessions"

    id                  = Column(Integer, primary_key=True, index=True)
    employee_id         = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    branch_id           = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)
    shift_assignment_id = Column(Integer, ForeignKey("shift_assignments.id"), nullable=True, index=True)
    shift_id            = Column(Integer, ForeignKey("shifts.id"), nullable=True, index=True)
    work_date           = Column(Date, nullable=False, index=True)

    check_in_at  = Column(DateTime, nullable=True, index=True)
    check_out_at = Column(DateTime, nullable=True, index=True)

    # open | completed | missing_checkout | absent | cancelled
    status = Column(String(30), default="open", index=True)

    # on_time | late | early | manual | auto
    check_in_status = Column(String(30), default="")
    # normal | early_leave | overtime | manual | auto
    check_out_status = Column(String(30), default="")

    late_minutes        = Column(Integer, default=0)
    early_leave_minutes = Column(Integer, default=0)
    overtime_minutes    = Column(Integer, default=0)
    worked_minutes      = Column(Integer, default=0)
    break_minutes       = Column(Integer, default=0)

    # face | manual | auto
    source        = Column(String(20), default="face")
    note          = Column(Text, default="")
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at    = Column(DateTime, default=datetime.now)
    updated_at    = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("shift_assignment_id", name="uq_attendance_session_assignment"),
    )


class AttendanceEvent(Base):
    __tablename__ = "attendance_events"

    id          = Column(Integer, primary_key=True, index=True)
    session_id  = Column(Integer, ForeignKey("attendance_sessions.id"), nullable=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    branch_id   = Column(Integer, ForeignKey("branches.id"), nullable=True, index=True)

    # check_in | check_out | break_start | break_end | manual_edit | auto_checkout
    event_type = Column(String(30), nullable=False, index=True)
    event_time = Column(DateTime, default=datetime.now, index=True)
    confidence = Column(Float, default=0.0)

    capture_path = Column(String(255), default="")
    face_bbox    = Column(Text, default="")   # JSON string [x1,y1,x2,y2] nếu cần đối soát
    image_hash   = Column(String(64), default="")
    device_id    = Column(String(64), default="")

    # face | manual | auto
    source        = Column(String(20), default="face")
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    note          = Column(Text, default="")
    created_at    = Column(DateTime, default=datetime.now)


class AttendanceEvidence(Base):
    __tablename__ = "attendance_evidence"

    id          = Column(Integer, primary_key=True, index=True)
    log_id      = Column(Integer, ForeignKey("attendance_logs.id"), nullable=False, index=True)
    event_id    = Column(Integer, ForeignKey("attendance_events.id"), nullable=True, index=True)
    session_id  = Column(Integer, ForeignKey("attendance_sessions.id"), nullable=True, index=True)
    employee_id = Column(Integer, index=True)
    emp_code    = Column(String(20), default="", index=True)

    image_path = Column(String(255), default="")
    image_hash = Column(String(64), default="")

    captured_at      = Column(DateTime, default=datetime.now, index=True)
    files_available  = Column(Boolean, default=True, index=True)
    deleted_at       = Column(DateTime, nullable=True)
    created_at       = Column(DateTime, default=datetime.now)


class AttendanceAuditRun(Base):
    __tablename__ = "attendance_audit_runs"

    id          = Column(Integer, primary_key=True, index=True)
    run_type    = Column(String(30), default="daily", index=True)
    from_date   = Column(Date, nullable=True, index=True)
    to_date     = Column(Date, nullable=True, index=True)
    emp_code    = Column(String(20), default="", index=True)
    status      = Column(String(20), default="completed", index=True)
    source      = Column(String(30), default="heuristic")
    summary     = Column(Text, default="")
    warnings    = Column(Text, default="[]")
    total_findings  = Column(Integer, default=0)
    high_count      = Column(Integer, default=0)
    medium_count    = Column(Integer, default=0)
    low_count       = Column(Integer, default=0)
    created_by      = Column(String(150), default="")
    created_at      = Column(DateTime, default=datetime.now, index=True)
    completed_at    = Column(DateTime, nullable=True)


class AttendanceAuditFinding(Base):
    __tablename__ = "attendance_audit_findings"

    id            = Column(Integer, primary_key=True, index=True)
    audit_run_id  = Column(Integer, ForeignKey("attendance_audit_runs.id"), nullable=True, index=True)
    log_id        = Column(Integer, ForeignKey("attendance_logs.id"), nullable=False, index=True)
    event_id      = Column(Integer, ForeignKey("attendance_events.id"), nullable=True, index=True)
    evidence_id   = Column(Integer, ForeignKey("attendance_evidence.id"), nullable=True, index=True)
    employee_id   = Column(Integer, index=True)
    emp_code      = Column(String(20), default="", index=True)
    emp_name      = Column(String(100), default="")

    risk_score = Column(Float, default=0.0, index=True)
    risk_level = Column(String(20), default="low", index=True)
    reasons    = Column(Text, default="[]")
    metrics    = Column(Text, default="{}")
    source     = Column(String(30), default="heuristic", index=True)

    # pending_review | reviewed | dismissed | confirmed
    review_status = Column(String(30), default="pending_review", index=True)
    reviewer_note = Column(Text, default="")
    reviewed_by   = Column(String(150), default="")
    reviewed_at   = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.now, index=True)
    updated_at    = Column(DateTime, default=datetime.now, onupdate=datetime.now)


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
