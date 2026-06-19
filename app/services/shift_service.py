"""
app/services/shift_service.py
Business logic cho ca làm việc.
"""

import json
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.attendance import AttendanceEvent, AttendanceLog, AttendanceSession
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.schemas.employee import normalize_job_role, normalize_job_roles, normalize_text
from app.services.attendance_period import ensure_period_unlocked
from app.services.leave_policy import (
    LEAVE_ASSIGNMENT_STATUS,
    PROTECTED_ASSIGNMENT_STATUSES,
    ensure_can_assign_employee,
    protected_assignment_message,
)


# ── Helpers ──────────────────────────────────────────────────────

VIETNAM_TZ = timezone(timedelta(hours=7))
PAST_ASSIGNMENT_LOCK_ERROR = "Không thể chỉnh lịch phân ca trước tuần hiện tại"
INACTIVE_BRANCH_ERROR = "Cửa hàng đã ngừng hoạt động"
ATTENDANCE_LOCK_ERROR = "Phân ca đã có chấm công, không thể xoá hoặc đổi lịch. Hãy xử lý ở màn Chấm công trước."
SHIFT_IN_USE_ERROR = "Ca đang được dùng trong lịch phân ca hoặc chấm công, không thể tắt"
CONSECUTIVE_SHIFT_GAP_MINUTES = 30


def consecutive_shift_gap_minutes(max_gap_minutes: int | None = None) -> int:
    if max_gap_minutes is None:
        max_gap_minutes = getattr(settings, "CONSECUTIVE_SHIFT_GAP_MINUTES", CONSECUTIVE_SHIFT_GAP_MINUTES)
    try:
        return max(0, int(max_gap_minutes))
    except (TypeError, ValueError):
        return CONSECUTIVE_SHIFT_GAP_MINUTES


def current_assignment_edit_start(today: date | None = None) -> date:
    """Return Monday of the current Vietnam-time week."""
    local_today = today or datetime.now(VIETNAM_TZ).date()
    return local_today - timedelta(days=local_today.weekday())


def ensure_assignment_editable_date(work_date: date | str) -> date:
    if isinstance(work_date, str):
        work_date = date.fromisoformat(work_date)
    if work_date < current_assignment_edit_start():
        raise ValueError(PAST_ASSIGNMENT_LOCK_ERROR)
    return work_date

def _shift_to_dict(s: Shift) -> dict:
    return {
        "id":                    s.id,
        "branch_id":             s.branch_id,
        "name":                  s.name,
        "code":                  s.code,
        "work_start":            s.work_start,
        "work_end":              s.work_end,
        "required_position":      s.required_position or "",
        "late_threshold_minutes": s.late_threshold_minutes,
        "early_checkin_minutes":  s.early_checkin_minutes,
        "auto_checkout_minutes":  s.auto_checkout_minutes,
        "break_minutes":          s.break_minutes,
        "is_overnight":           s.is_overnight,
        "is_active":             s.is_active,
        "note":                  s.note or "",
        "created_at":            s.created_at.isoformat() if s.created_at else None,
    }


def _assignment_attendance_lock_info(
    a: ShiftAssignment,
    db: Session,
    shift: Optional[Shift] = None,
) -> tuple[bool, str]:
    shift = shift or db.query(Shift).filter_by(id=a.shift_id).first()
    session_with_attendance = (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.shift_assignment_id == a.id,
              (AttendanceSession.check_in_at.isnot(None)) | (AttendanceSession.check_out_at.isnot(None)),
          )
          .first()
    )
    if session_with_attendance:
        return True, "Đã có chấm công"
    if not shift:
        return False, ""

    _start, _end, checkin_from, checkout_until = shift_window(a.work_date, shift)
    logs = (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.emp_code == a.emp_code,
              AttendanceLog.timestamp >= checkin_from,
              AttendanceLog.timestamp <= checkout_until,
          )
          .order_by(AttendanceLog.timestamp.asc(), AttendanceLog.id.asc())
          .all()
    )
    if any(_log_available_for_assignment(log, a, db) for log in logs):
        return True, "Đã có chấm công"

    employee_id = a.employee_id
    if not employee_id:
        emp = db.query(Employee).filter_by(emp_code=a.emp_code).first()
        employee_id = emp.id if emp else None
    if employee_id:
        events = (
            db.query(AttendanceEvent)
              .filter(
                  AttendanceEvent.employee_id == employee_id,
                  AttendanceEvent.event_type.in_(["check_in", "check_out", "auto_checkout"]),
                  AttendanceEvent.event_time >= checkin_from,
                  AttendanceEvent.event_time <= checkout_until,
              )
              .order_by(AttendanceEvent.event_time.asc(), AttendanceEvent.id.asc())
              .all()
        )
        if any(_event_available_for_assignment(event, a, db) for event in events):
            return True, "Đã có chấm công"

    return False, ""


def _assignment_attendance_review_info(a: ShiftAssignment, db: Session) -> dict:
    session = (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.shift_assignment_id == a.id,
              AttendanceSession.status != "cancelled",
          )
          .order_by(
              (AttendanceSession.review_status == "pending_review").desc(),
              AttendanceSession.id.desc(),
          )
          .first()
    )
    if not session:
        return {}
    info = {
        "session_id": session.id,
        "status": session.status or "",
        "review_type": session.review_type or "",
        "review_status": session.review_status or "none",
        "review_note": session.review_note or "",
    }
    if session.status == "absent" or session.review_type == "absent":
        info["is_absent"] = True
        if session.review_status == "pending_review":
            info["label"] = "Vắng - chờ duyệt"
        elif session.review_status == "rejected":
            info["label"] = "Vắng - cần xem lại"
        elif session.review_status == "approved":
            info["label"] = "Vắng đã chốt"
        else:
            info["label"] = "Vắng"
    return info


def _session_linked_to_other_assignment(session: AttendanceSession | None, assignment: ShiftAssignment) -> bool:
    return bool(
        session
        and session.shift_assignment_id
        and assignment.id
        and int(session.shift_assignment_id) != int(assignment.id)
    )


def _session_linked_to_assignment(session: AttendanceSession | None, assignment: ShiftAssignment) -> bool:
    return bool(
        session
        and session.shift_assignment_id
        and assignment.id
        and int(session.shift_assignment_id) == int(assignment.id)
    )


def _event_session(event: AttendanceEvent | None, db: Session) -> AttendanceSession | None:
    if not event or not event.session_id:
        return None
    return db.query(AttendanceSession).filter_by(id=event.session_id).first()


def _event_available_for_assignment(event: AttendanceEvent | None, assignment: ShiftAssignment, db: Session) -> bool:
    linked_session = _event_session(event, db)
    if _session_linked_to_other_assignment(linked_session, assignment):
        return False
    if _session_linked_to_assignment(linked_session, assignment):
        return True
    timestamp_session = _session_for_event_timestamp(event, db)
    if _session_linked_to_other_assignment(timestamp_session, assignment):
        return False
    if _session_linked_to_assignment(timestamp_session, assignment):
        return True
    return False


def _event_for_log(log: AttendanceLog, db: Session) -> AttendanceEvent | None:
    if not log.employee_id:
        return None
    return (
        db.query(AttendanceEvent)
          .filter(
              AttendanceEvent.employee_id == log.employee_id,
              AttendanceEvent.event_type == log.check_type,
              AttendanceEvent.event_time == log.timestamp,
          )
          .order_by(AttendanceEvent.id.desc())
          .first()
    )


def _session_for_log_timestamp(log: AttendanceLog, db: Session) -> AttendanceSession | None:
    if not log.employee_id:
        return None
    q = db.query(AttendanceSession).filter(AttendanceSession.employee_id == log.employee_id)
    if log.check_type == "check_in":
        return q.filter(AttendanceSession.check_in_at == log.timestamp).order_by(AttendanceSession.id.desc()).first()
    if log.check_type == "check_out":
        return q.filter(AttendanceSession.check_out_at == log.timestamp).order_by(AttendanceSession.id.desc()).first()
    return None


def _session_for_event_timestamp(event: AttendanceEvent | None, db: Session) -> AttendanceSession | None:
    if not event or not event.employee_id:
        return None
    q = db.query(AttendanceSession).filter(AttendanceSession.employee_id == event.employee_id)
    if event.event_type == "check_in":
        return q.filter(AttendanceSession.check_in_at == event.event_time).order_by(AttendanceSession.id.desc()).first()
    if event.event_type in ("check_out", "auto_checkout"):
        return q.filter(AttendanceSession.check_out_at == event.event_time).order_by(AttendanceSession.id.desc()).first()
    return None


def _log_available_for_assignment(log: AttendanceLog, assignment: ShiftAssignment, db: Session) -> bool:
    event = _event_for_log(log, db)
    if event and not _event_available_for_assignment(event, assignment, db):
        return False
    timestamp_session = _session_for_log_timestamp(log, db)
    if _session_linked_to_other_assignment(timestamp_session, assignment):
        return False
    if (event and _event_available_for_assignment(event, assignment, db)) or _session_linked_to_assignment(timestamp_session, assignment):
        return True
    if _is_auto_checkout_log(log):
        return bool(
            timestamp_session
            and timestamp_session.shift_assignment_id
            and assignment.id
            and int(timestamp_session.shift_assignment_id) == int(assignment.id)
        )
    if log.check_type == "check_in":
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if shift:
            start, end, _checkin_from, _checkout_until = shift_window(assignment.work_date, shift)
            if log.timestamp >= end and not _session_linked_to_assignment(timestamp_session, assignment):
                return False
            if log.timestamp >= start:
                better_assignment, _better_shift = find_shift_assignment_for_checkin(assignment.emp_code, log.timestamp, db)
                if better_assignment and better_assignment.id != assignment.id:
                    return False
    return True


def log_available_for_assignment(log: AttendanceLog, assignment: ShiftAssignment, db: Session) -> bool:
    return _log_available_for_assignment(log, assignment, db)


def _assignment_has_attendance(a: ShiftAssignment, db: Session, shift: Optional[Shift] = None) -> bool:
    locked, _reason = _assignment_attendance_lock_info(a, db, shift)
    return locked


def _ensure_assignment_not_attendance_locked(
    a: ShiftAssignment,
    db: Session,
    shift: Optional[Shift] = None,
) -> None:
    if _assignment_has_attendance(a, db, shift):
        raise ValueError(ATTENDANCE_LOCK_ERROR)


def _assignment_to_dict(a: ShiftAssignment, shift: Optional[Shift] = None, db: Session | None = None) -> dict:
    attendance_locked = False
    attendance_lock_reason = ""
    if db is not None:
        attendance_locked, attendance_lock_reason = _assignment_attendance_lock_info(a, db, shift)
    attendance_review = _assignment_attendance_review_info(a, db) if db is not None else {}
    d = {
        "id":          a.id,
        "employee_id": a.employee_id,
        "branch_id":   a.branch_id,
        "emp_code":    a.emp_code,
        "shift_id":    a.shift_id,
        "work_date":   a.work_date.isoformat(),
        "status":      a.status,
        "note":        a.note or "",
        "assigned_by": a.assigned_by or "",
        "assigned_by_id": a.assigned_by_id,
        "attendance_locked": attendance_locked,
        "attendance_lock_reason": attendance_lock_reason,
        "attendance_review": attendance_review,
    }
    if shift:
        d["shift"] = _shift_to_dict(shift)
    return d


def _employee_role_matches_shift(emp: Employee | None, shift: Shift) -> bool:
    required = normalize_text(normalize_job_role(shift.required_position or ""))
    if not required or not emp:
        return True
    try:
        multi_roles = json.loads(emp.job_roles or "[]")
    except Exception:
        multi_roles = []
    employee_roles = {
        normalize_text(normalize_job_role(emp.job_role or "")),
        normalize_text(normalize_job_role(emp.position or "")),
        *[normalize_text(role) for role in normalize_job_roles(multi_roles)],
    }
    employee_roles.discard("")
    return required in employee_roles


def _ensure_assignable_workday(work_date: date, db: Session, branch_id: int | None = None) -> None:
    from app.services.work_calendar import get_calendar_day

    if branch_id is not None:
        branch = db.query(Branch).filter_by(id=branch_id).first()
        if not branch:
            raise ValueError("Không tìm thấy cửa hàng")
        if not branch.is_active:
            raise ValueError(INACTIVE_BRANCH_ERROR)
    cal = get_calendar_day(work_date, db, branch_id)
    if cal.get("day_type") == "off":
        label = cal.get("label") or "ngày nghỉ/đóng cửa"
        raise ValueError(f"Không thể xếp ca vào {label}")


def _protected_assignment_action_error(status: str | None, action: str) -> ValueError:
    return ValueError(protected_assignment_message(status, action))


# ── CRUD Ca làm việc ─────────────────────────────────────────────

def list_shifts(db: Session, active_only: bool = False) -> list[dict]:
    q = db.query(Shift)
    if active_only:
        q = q.filter_by(is_active=True)
    return [_shift_to_dict(s) for s in q.order_by(Shift.work_start).all()]


def get_shift(shift_id: int, db: Session) -> Optional[dict]:
    s = db.query(Shift).filter_by(id=shift_id).first()
    return _shift_to_dict(s) if s else None


def _slugify_shift_code(name: str) -> str:
    """Sinh mã ca nội bộ từ tên ca: 'Ca tối VIP' -> 'ca-toi-vip'."""
    source = (name or "").replace("đ", "d").replace("Đ", "D")
    raw = unicodedata.normalize("NFKD", source)
    ascii_text = raw.encode("ascii", "ignore").decode("ascii").lower()
    code = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return code or "ca"


def _unique_shift_code(value: str, branch_id: int | None, db: Session) -> str:
    base = _slugify_shift_code(value)
    code = base
    i = 2
    while db.query(Shift).filter_by(branch_id=branch_id, code=code).first():
        code = f"{base}-{i}"
        i += 1
    return code


def create_shift(data: dict, db: Session) -> dict:
    branch_id = data.get("branch_id")
    code = _unique_shift_code(data.get("code") or data["name"], branch_id, db)
    s = Shift(
        branch_id  = branch_id,
        name       = data["name"],
        code       = code,
        work_start = data["work_start"],
        work_end   = data["work_end"],
        required_position      = data.get("required_position", ""),
        late_threshold_minutes = data.get("late_threshold_minutes", 15),
        early_checkin_minutes  = data.get("early_checkin_minutes", 30),
        auto_checkout_minutes  = data.get("auto_checkout_minutes", 180),
        break_minutes          = data.get("break_minutes", 0),
        is_overnight           = data.get("is_overnight") if data.get("is_overnight") is not None else _is_overnight(data["work_start"], data["work_end"]),
        note       = data.get("note", ""),
        is_active  = True,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _shift_to_dict(s)


def update_shift(shift_id: int, data: dict, db: Session) -> Optional[dict]:
    s = db.query(Shift).filter_by(id=shift_id).first()
    if not s:
        return None
    if data.get("is_active") is False and s.is_active:
        _ensure_shift_can_deactivate(shift_id, db)
    for field in (
        "branch_id", "name", "work_start", "work_end",
        "required_position",
        "late_threshold_minutes", "early_checkin_minutes",
        "auto_checkout_minutes", "break_minutes", "is_overnight",
        "note", "is_active",
    ):
        if field in data:
            setattr(s, field, data[field])
    if ("work_start" in data or "work_end" in data) and "is_overnight" not in data:
        s.is_overnight = _is_overnight(s.work_start, s.work_end)
    db.commit()
    db.refresh(s)
    return _shift_to_dict(s)


def _ensure_shift_can_deactivate(shift_id: int, db: Session) -> None:
    today = datetime.now(VIETNAM_TZ).date()
    active_assignment = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.shift_id == shift_id,
              ShiftAssignment.status != "cancelled",
              ShiftAssignment.work_date >= today,
          )
          .first()
    )
    if active_assignment:
        raise ValueError(SHIFT_IN_USE_ERROR)
    active_session = (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.shift_id == shift_id,
              AttendanceSession.status.in_(["open", "missing_checkout", "missing_checkin"]),
          )
          .first()
    )
    pending_session = (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.shift_id == shift_id,
              AttendanceSession.review_status == "pending_review",
          )
          .first()
    )
    if active_session or pending_session:
        raise ValueError(SHIFT_IN_USE_ERROR)


def delete_shift(shift_id: int, db: Session) -> bool:
    s = db.query(Shift).filter_by(id=shift_id).first()
    if not s:
        return False
    _ensure_shift_can_deactivate(shift_id, db)
    # Soft delete: chỉ deactivate, giữ lịch sử assignment
    s.is_active = False
    db.commit()
    return True


# ── CRUD Phân công ca ────────────────────────────────────────────

def assign_shift(emp_code: str, shift_id: int, work_date: date,
                 assigned_by: str = "", note: str = "", db: Session = None,
                 commit: bool = True) -> dict:
    """
    Phân công ca cho nhân viên vào ngày cụ thể.
    Nếu đã có cùng ca trong ngày → cập nhật (upsert).
    Nhà hàng có thể phân nhiều ca khác nhau cho cùng một nhân viên trong ngày.
    """
    work_date = ensure_assignment_editable_date(work_date)
    emp = db.query(Employee).filter_by(emp_code=emp_code).first()
    shift = db.query(Shift).filter_by(id=shift_id).first()
    if not shift:
        raise ValueError(f"Không tìm thấy ca #{shift_id}")
    if not shift.is_active:
        raise ValueError(f"Ca #{shift_id} đã tắt, không thể phân công")
    assignment_branch_id = shift.branch_id or (emp.branch_id if emp else None)
    ensure_period_unlocked(db, assignment_branch_id, work_date)
    _ensure_assignable_workday(work_date, db, assignment_branch_id)
    existing = (
        db.query(ShiftAssignment)
          .filter_by(emp_code=emp_code, work_date=work_date, shift_id=shift_id)
          .first()
    )
    ensure_can_assign_employee(db, emp_code, work_date, existing)
    if emp and not _employee_role_matches_shift(emp, shift):
        role = emp.job_role or emp.position or "chưa xác định"
        raise ValueError(f"Nhân viên {emp_code} có vai trò '{role}' không phù hợp với ca yêu cầu '{shift.required_position}'")

    if existing:
        existing.employee_id = emp.id if emp else existing.employee_id
        existing.branch_id   = assignment_branch_id or existing.branch_id
        existing.assigned_by = assigned_by
        existing.note        = note
        existing.status      = "scheduled"
        if commit:
            db.commit()
            db.refresh(existing)
        else:
            db.flush()
        a = existing
    else:
        a = ShiftAssignment(
            employee_id = emp.id if emp else None,
            branch_id   = assignment_branch_id,
            emp_code    = emp_code,
            shift_id    = shift_id,
            work_date   = work_date,
            status      = "scheduled",
            assigned_by = assigned_by,
            note        = note,
        )
        db.add(a)
        if commit:
            db.commit()
            db.refresh(a)
        else:
            db.flush()

    _reconcile_assignment_attendance(a, shift, emp, db)
    if commit:
        db.commit()
        db.refresh(a)
    else:
        db.flush()
    return _assignment_to_dict(a, shift, db)


def bulk_assign_shift(emp_codes: list[str], shift_id: int,
                      dates: list[date], assigned_by: str = "",
                      note: str = "", db: Session = None) -> int:
    """
    Phân công ca hàng loạt: nhiều nhân viên × nhiều ngày.
    Trả về số assignment đã tạo/cập nhật.
    """
    count = 0
    for emp_code in emp_codes:
        for d in dates:
            assign_shift(emp_code, shift_id, d, assigned_by=assigned_by, note=note, db=db)
            count += 1
    return count


def get_assignments_by_emp(emp_code: str, from_date: date, to_date: date,
                           db: Session) -> list[dict]:
    """Lấy lịch ca của 1 nhân viên trong khoảng thời gian."""
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date >= from_date,
              ShiftAssignment.work_date <= to_date,
              ShiftAssignment.status != "cancelled",
          )
          .order_by(ShiftAssignment.work_date, ShiftAssignment.shift_id)
          .all()
    )
    result = []
    for a in rows:
        shift = db.query(Shift).filter_by(id=a.shift_id).first()
        result.append(_assignment_to_dict(a, shift, db))
    return result


def get_assignments_by_date(work_date: date, db: Session) -> list[dict]:
    """Lấy tất cả phân công ca trong 1 ngày (dùng cho manager xem lịch)."""
    rows = (
        db.query(ShiftAssignment)
          .filter_by(work_date=work_date)
          .filter(ShiftAssignment.status != "cancelled")
          .order_by(ShiftAssignment.emp_code)
          .all()
    )
    result = []
    for a in rows:
        shift = db.query(Shift).filter_by(id=a.shift_id).first()
        result.append(_assignment_to_dict(a, shift, db))
    return result


def get_assignments_by_range(from_date: date, to_date: date, db: Session) -> list[dict]:
    """Lấy tất cả phân công ca trong một khoảng ngày."""
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.work_date >= from_date,
              ShiftAssignment.work_date <= to_date,
              ShiftAssignment.status != "cancelled",
          )
          .order_by(ShiftAssignment.work_date, ShiftAssignment.shift_id, ShiftAssignment.emp_code)
          .all()
    )
    result = []
    for a in rows:
        shift = db.query(Shift).filter_by(id=a.shift_id).first()
        result.append(_assignment_to_dict(a, shift, db))
    return result


def update_assignment(assignment_id: int, data: dict, assigned_by: str = "", db: Session = None) -> Optional[dict]:
    a = db.query(ShiftAssignment).filter_by(id=assignment_id).first()
    if not a:
        return None
    if a.status in PROTECTED_ASSIGNMENT_STATUSES:
        raise _protected_assignment_action_error(a.status, "sửa bằng phân ca")

    new_shift_id = data.get("shift_id", a.shift_id)
    new_work_date = data.get("work_date", a.work_date)
    if isinstance(new_work_date, str):
        new_work_date = date.fromisoformat(new_work_date)
    new_status = data.get("status", a.status)
    schedule_changed = (
        int(new_shift_id) != int(a.shift_id)
        or new_work_date != a.work_date
        or new_status != a.status
    )
    if not schedule_changed and set(data).issubset({"note"}):
        if "note" in data:
            a.note = data.get("note") or ""
            a.assigned_by = assigned_by or a.assigned_by
            db.commit()
            db.refresh(a)
        shift = db.query(Shift).filter_by(id=a.shift_id).first()
        return _assignment_to_dict(a, shift, db)
    if schedule_changed:
        _ensure_assignment_not_attendance_locked(a, db)
    ensure_assignment_editable_date(a.work_date)
    new_work_date = ensure_assignment_editable_date(new_work_date)
    shift = db.query(Shift).filter_by(id=new_shift_id).first()
    if not shift:
        raise ValueError(f"Không tìm thấy ca #{new_shift_id}")
    if not shift.is_active:
        raise ValueError(f"Ca #{new_shift_id} đã tắt, không thể phân công")

    emp = db.query(Employee).filter_by(emp_code=a.emp_code).first()
    new_branch_id = shift.branch_id or (emp.branch_id if emp else a.branch_id)
    ensure_period_unlocked(db, a.branch_id, a.work_date)
    ensure_period_unlocked(db, new_branch_id, new_work_date)
    _ensure_assignable_workday(new_work_date, db, new_branch_id)
    ensure_can_assign_employee(db, a.emp_code, new_work_date, a)
    if emp and not _employee_role_matches_shift(emp, shift):
        role = emp.job_role or emp.position or "chưa xác định"
        raise ValueError(f"Nhân viên {a.emp_code} có vai trò '{role}' không phù hợp với ca yêu cầu '{shift.required_position}'")

    duplicate = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.id != a.id,
              ShiftAssignment.emp_code == a.emp_code,
              ShiftAssignment.work_date == new_work_date,
              ShiftAssignment.shift_id == new_shift_id,
              ShiftAssignment.status != "cancelled",
          )
          .first()
    )
    if duplicate:
        raise ValueError("Nhân viên đã có ca này trong ngày đã chọn")

    a.shift_id = new_shift_id
    a.work_date = new_work_date
    a.employee_id = emp.id if emp else a.employee_id
    a.branch_id = new_branch_id
    a.assigned_by = assigned_by or a.assigned_by
    if "note" in data:
        a.note = data.get("note") or ""
    if "status" in data:
        a.status = data.get("status") or "scheduled"

    db.commit()
    db.refresh(a)
    if schedule_changed:
        _reconcile_assignment_attendance(a, shift, emp, db)
        db.commit()
        db.refresh(a)
    return _assignment_to_dict(a, shift, db)


def delete_assignment(assignment_id: int, db: Session) -> bool:
    a = db.query(ShiftAssignment).filter_by(id=assignment_id).first()
    if not a:
        return False
    if a.status in PROTECTED_ASSIGNMENT_STATUSES:
        raise _protected_assignment_action_error(a.status, "xoá khỏi lịch phân ca")
    shift = db.query(Shift).filter_by(id=a.shift_id).first()
    _ensure_assignment_not_attendance_locked(a, db, shift)
    ensure_assignment_editable_date(a.work_date)
    if a.branch_id is not None:
        branch = db.query(Branch).filter_by(id=a.branch_id).first()
        if not branch:
            raise ValueError("Không tìm thấy cửa hàng")
        if not branch.is_active:
            raise ValueError(INACTIVE_BRANCH_ERROR)
    ensure_period_unlocked(db, a.branch_id, a.work_date)
    # Giữ bản ghi để attendance_sessions còn tham chiếu được lịch sử ca.
    # Các query lịch đã lọc status != "cancelled", nên thao tác này vẫn ẩn
    # phân công khỏi UI mà không phá khóa ngoại.
    a.status = "cancelled"
    db.commit()
    return True


# ── Core: Lấy ca của nhân viên cho 1 ngày ───────────────────────

def get_shift_for_employee(emp_code: str, work_date: date, db: Session) -> dict:
    """
    Trả về thông tin ca làm việc của 1 nhân viên trong 1 ngày.
    
    Chỉ trả về ca khi có ShiftAssignment cụ thể cho ngày đó.
    """
    assignments = (
        db.query(ShiftAssignment)
          .filter_by(emp_code=emp_code, work_date=work_date)
          .filter(ShiftAssignment.status != "cancelled")
          .order_by(ShiftAssignment.id)
          .all()
    )

    shift_rows = []
    for assignment in assignments:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if shift:
            shift_rows.append({
                "assignment_id": assignment.id,
                "shift_id":      shift.id,
                "shift_name":    shift.name,
                "shift_code":    shift.code,
                "work_start":    shift.work_start,
                "work_end":      shift.work_end,
                "late_threshold_minutes": shift.late_threshold_minutes,
                "note":          assignment.note or "",
            })

    if shift_rows:
        first = shift_rows[0]
        return {
            "source":      "assignment",
            "shift_id":    first["shift_id"],
            "shift_name":  first["shift_name"],
            "shift_code":  first["shift_code"],
            "work_start":  first["work_start"],
            "work_end":    first["work_end"],
            "late_threshold_minutes": first["late_threshold_minutes"],
            "shifts":      shift_rows,
        }

    return {
        "source":      "none",
        "shift_id":    None,
        "shift_name":  "",
        "shift_code":  "",
        "work_start":  "",
        "work_end":    "",
        "late_threshold_minutes": 0,
        "shifts":      [],
    }


def _parse_time(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(hour=h, minute=m)


def _is_overnight(work_start: str, work_end: str) -> bool:
    return _parse_time(work_end) <= _parse_time(work_start)


def shift_window(work_date: date, shift: Shift) -> tuple[datetime, datetime, datetime, datetime]:
    """Trả về (start, end, checkin_from, checkout_until) cho một ca."""
    start_t = _parse_time(shift.work_start)
    end_t   = _parse_time(shift.work_end)
    start   = datetime.combine(work_date, start_t)
    end     = datetime.combine(work_date, end_t)
    if shift.is_overnight or end <= start:
        end += timedelta(days=1)
    checkin_from   = start - timedelta(minutes=shift.early_checkin_minutes or 0)
    checkout_until = end + timedelta(minutes=shift.auto_checkout_minutes or 0)
    return start, end, checkin_from, checkout_until


def missing_checkin_checkout_window(work_date: date, shift: Shift) -> tuple[datetime, datetime]:
    """Cửa sổ nhận lượt chấm đầu tiên là checkout vì thiếu check-in."""
    shift_start, shift_end, _checkin_from, checkout_until = shift_window(work_date, shift)
    minutes = max(0, int(settings.MISSING_CHECKIN_CHECKOUT_WINDOW_MINUTES or 0))
    checkout_from = max(shift_start, shift_end - timedelta(minutes=minutes))
    return checkout_from, checkout_until


def is_missing_checkin_checkout_time(work_date: date, shift: Shift, moment: datetime) -> bool:
    checkout_from, checkout_until = missing_checkin_checkout_window(work_date, shift)
    return checkout_from <= moment <= checkout_until


def _assignment_shift_rows(
    emp_code: str,
    candidate_dates: list[date],
    db: Session,
) -> list[tuple[ShiftAssignment, Shift, datetime, datetime, datetime, datetime]]:
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date.in_(candidate_dates),
              ShiftAssignment.status.notin_(["cancelled", LEAVE_ASSIGNMENT_STATUS]),
          )
          .all()
    )
    result: list[tuple[ShiftAssignment, Shift, datetime, datetime, datetime, datetime]] = []
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if not shift:
            continue
        start, end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
        result.append((assignment, shift, start, end, checkin_from, checkout_until))
    result.sort(key=lambda item: (item[2], item[0].id or 0))
    return result


def consecutive_shift_chain_for_assignment(
    assignment: ShiftAssignment,
    shift: Shift,
    db: Session,
    max_gap_minutes: int | None = None,
) -> list[tuple[ShiftAssignment, Shift]]:
    """Return the contiguous shift chain that contains `assignment`."""
    if not assignment or not shift:
        return []
    candidate_dates = [
        assignment.work_date - timedelta(days=1),
        assignment.work_date,
        assignment.work_date + timedelta(days=1),
    ]
    rows = _assignment_shift_rows(assignment.emp_code, candidate_dates, db)
    if not rows:
        return []
    target_index = next((idx for idx, row in enumerate(rows) if row[0].id == assignment.id), None)
    if target_index is None:
        return []

    max_gap = timedelta(minutes=consecutive_shift_gap_minutes(max_gap_minutes))
    start_index = target_index
    while start_index > 0:
        prev_row = rows[start_index - 1]
        current_row = rows[start_index]
        gap = current_row[2] - prev_row[3]
        if gap < timedelta(0) or gap > max_gap:
            break
        start_index -= 1

    end_index = target_index
    while end_index + 1 < len(rows):
        current_row = rows[end_index]
        next_row = rows[end_index + 1]
        gap = next_row[2] - current_row[3]
        if gap < timedelta(0) or gap > max_gap:
            break
        end_index += 1

    return [(row[0], row[1]) for row in rows[start_index:end_index + 1]]


def assignment_is_first_in_consecutive_chain(
    assignment: ShiftAssignment,
    shift: Shift,
    db: Session,
    max_gap_minutes: int | None = None,
) -> bool:
    chain = consecutive_shift_chain_for_assignment(assignment, shift, db, max_gap_minutes)
    return bool(chain and chain[0][0].id == assignment.id)


def assignment_is_last_in_consecutive_chain(
    assignment: ShiftAssignment,
    shift: Shift,
    db: Session,
    max_gap_minutes: int | None = None,
) -> bool:
    chain = consecutive_shift_chain_for_assignment(assignment, shift, db, max_gap_minutes)
    return bool(chain and chain[-1][0].id == assignment.id)


def _session_note_for_reconcile(session: AttendanceSession) -> str:
    if session.check_in_at and session.check_out_at:
        return "Đồng bộ từ log chấm công sau khi bổ sung ca"
    if session.check_in_at:
        return "Đồng bộ check-in từ log chấm công sau khi bổ sung ca"
    if session.check_out_at:
        return "Đồng bộ checkout cuối ca, thiếu check-in - chờ quản lý xác nhận"
    return session.note or ""


def _is_auto_checkout_log(log: AttendanceLog | None) -> bool:
    return bool(log and log.check_type == "check_out" and (log.note or "").startswith("Tự động chấm ra"))


def _clear_session_attendance(session: AttendanceSession) -> None:
    session.check_in_at = None
    session.check_out_at = None
    session.check_in_status = ""
    session.check_out_status = ""
    session.late_minutes = 0
    session.early_leave_minutes = 0
    session.overtime_minutes = 0
    session.worked_minutes = 0
    if session.status not in ("absent", "cancelled", "missing_checkout"):
        session.status = "open"
    if session.review_type not in ("absent", "missing_checkout"):
        session.review_type = ""
        session.review_status = "none"


def _unlink_reconciled_events(session: AttendanceSession, db: Session) -> None:
    if not session.id:
        return
    events = (
        db.query(AttendanceEvent)
          .filter(
              AttendanceEvent.session_id == session.id,
              AttendanceEvent.event_type.in_(["check_in", "check_out"]),
          )
          .all()
    )
    for event in events:
        event.session_id = None


def _link_or_create_reconciled_event(
    log: AttendanceLog,
    session: AttendanceSession,
    emp: Employee,
    db: Session,
) -> None:
    event = (
        db.query(AttendanceEvent)
          .filter(
              AttendanceEvent.employee_id == emp.id,
              AttendanceEvent.event_type == log.check_type,
              AttendanceEvent.event_time == log.timestamp,
          )
          .first()
    )
    if event:
        event.session_id = session.id
        event.branch_id = session.branch_id
        if not event.capture_path:
            event.capture_path = log.capture_path or ""
        if not event.note:
            event.note = session.note or log.note or ""
        return

    db.add(AttendanceEvent(
        session_id=session.id,
        employee_id=emp.id,
        branch_id=session.branch_id,
        event_type=log.check_type,
        event_time=log.timestamp,
        confidence=log.confidence or 0.0,
        capture_path=log.capture_path or "",
        source="reconciled",
        note=session.note or log.note or "",
    ))


def _reconcile_assignment_attendance(
    assignment: ShiftAssignment,
    shift: Shift,
    emp: Employee | None,
    db: Session,
) -> None:
    """
    Khi quản lý bổ sung ca sau khi nhân viên đã chấm công, chuyển log cũ vào
    session của ca đó để báo công và auto-checkout xử lý tiếp như ca bình thường.
    """
    rebuild_session_for_assignment(assignment, db, shift=shift, emp=emp)


def rebuild_session_for_assignment(
    assignment: ShiftAssignment,
    db: Session,
    shift: Shift | None = None,
    emp: Employee | None = None,
) -> AttendanceSession | None:
    """
    Rebuild AttendanceSession từ AttendanceLog trong cửa sổ ca.

    Quy tắc tính session:
    - check-in là log check_in đầu tiên trong cửa sổ.
    - check-out là log check_out cuối cùng sau check-in.
    - Không có check-in nhưng có check-out ở cuối ca thì tạo phiên missing_checkin
      chờ quản lý xác nhận giờ vào.
    """
    if assignment.status in ("cancelled", LEAVE_ASSIGNMENT_STATUS):
        return None
    shift = shift or db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
    if not shift:
        return None
    emp = emp or (
        db.query(Employee).filter_by(id=assignment.employee_id).first()
        if assignment.employee_id else None
    )
    if not emp:
        emp = db.query(Employee).filter_by(emp_code=assignment.emp_code).first()
    if not emp:
        return None

    shift_start, shift_end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
    logs = (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.emp_code == assignment.emp_code,
              AttendanceLog.timestamp >= checkin_from,
              AttendanceLog.timestamp <= checkout_until,
          )
          .order_by(AttendanceLog.timestamp.asc(), AttendanceLog.id.asc())
          .all()
    )
    logs = [log for log in logs if _log_available_for_assignment(log, assignment, db)]
    check_in_log = next((log for log in logs if log.check_type == "check_in"), None)
    check_out_candidates = [
        log for log in logs
        if check_in_log
        and log.check_type == "check_out"
        and log.timestamp >= check_in_log.timestamp
    ]
    check_out_log = check_out_candidates[-1] if check_out_candidates else None
    checkout_only_candidates = [
        log for log in logs
        if not check_in_log
        and log.check_type == "check_out"
        and is_missing_checkin_checkout_time(assignment.work_date, shift, log.timestamp)
    ]
    checkout_only_log = checkout_only_candidates[-1] if checkout_only_candidates else None

    session = (
        db.query(AttendanceSession)
          .filter_by(shift_assignment_id=assignment.id)
          .first()
    )
    if not check_in_log:
        if not session and checkout_only_log:
            session = AttendanceSession(
                employee_id=emp.id,
                branch_id=assignment.branch_id or shift.branch_id or emp.branch_id,
                shift_assignment_id=assignment.id,
                shift_id=shift.id,
                work_date=assignment.work_date,
                status="missing_checkin",
                source="reconciled",
                break_minutes=shift.break_minutes or 0,
            )
            db.add(session)
            db.flush()
        if not session:
            return None
        previous_review_type = session.review_type or ""
        previous_review_status = session.review_status or "none"
        _unlink_reconciled_events(session, db)
        _clear_session_attendance(session)
        session.employee_id = emp.id
        session.branch_id = assignment.branch_id or shift.branch_id or emp.branch_id
        session.shift_id = shift.id
        session.work_date = assignment.work_date
        session.break_minutes = shift.break_minutes or 0
        if checkout_only_log:
            session.check_out_at = checkout_only_log.timestamp
            session.status = "missing_checkin"
            session.check_in_status = ""
            session.check_out_status = "normal"
            session.early_leave_minutes = max(0, int((shift_end - checkout_only_log.timestamp).total_seconds() / 60))
            raw_overtime = max(0, int((checkout_only_log.timestamp - shift_end).total_seconds() / 60))
            session.overtime_minutes = raw_overtime if raw_overtime > settings.OVERTIME_APPROVAL_THRESHOLD_MINUTES else 0
            session.worked_minutes = 0
            session.review_type = "missing_checkin"
            if previous_review_type == "missing_checkin" and previous_review_status == "rejected":
                session.review_status = previous_review_status
            else:
                session.review_status = "pending_review"
            session.note = _session_note_for_reconcile(session)
            _link_or_create_reconciled_event(checkout_only_log, session, emp, db)
        return session

    if not session:
        session = AttendanceSession(
            employee_id=emp.id,
            branch_id=assignment.branch_id or shift.branch_id or emp.branch_id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=assignment.work_date,
            status="open",
            source="reconciled",
            break_minutes=shift.break_minutes or 0,
        )
        db.add(session)
        db.flush()

    previous_review_type = session.review_type or ""
    previous_review_status = session.review_status or "none"
    _unlink_reconciled_events(session, db)
    session.employee_id = emp.id
    session.branch_id = assignment.branch_id or shift.branch_id or emp.branch_id
    session.shift_id = shift.id
    session.work_date = assignment.work_date
    session.break_minutes = shift.break_minutes or 0
    session.source = session.source or "reconciled"
    session.review_type = ""
    session.review_status = "none"
    session.review_note = ""
    session.check_in_at = check_in_log.timestamp
    raw_late_minutes = max(0, int((check_in_log.timestamp - shift_start).total_seconds() / 60))
    grace_minutes = shift.late_threshold_minutes if shift.late_threshold_minutes is not None else settings.CHECKIN_GRACE_MINUTES
    session.late_minutes = max(0, raw_late_minutes - grace_minutes)
    session.check_in_status = "late" if raw_late_minutes > grace_minutes else "on_time"

    if check_out_log:
        session.check_out_at = check_out_log.timestamp
        session.early_leave_minutes = max(0, int((shift_end - check_out_log.timestamp).total_seconds() / 60))
        raw_overtime = max(0, int((check_out_log.timestamp - shift_end).total_seconds() / 60))
        session.overtime_minutes = raw_overtime if raw_overtime > settings.OVERTIME_APPROVAL_THRESHOLD_MINUTES else 0
        if _is_auto_checkout_log(check_out_log):
            session.status = "missing_checkout"
            session.check_out_status = "auto"
            session.review_type = "missing_checkout"
            if previous_review_type == "missing_checkout" and previous_review_status in ("approved", "rejected"):
                session.review_status = previous_review_status
                if previous_review_status == "approved":
                    session.status = "completed"
            else:
                session.review_status = "pending_review"
            session.early_leave_minutes = 0
            session.overtime_minutes = 0
        else:
            session.status = "completed"
            if session.early_leave_minutes > 0:
                session.check_out_status = "early_leave"
            elif session.overtime_minutes > 0:
                session.check_out_status = "overtime"
                session.review_type = "overtime"
                session.review_status = "pending_review"
            else:
                session.check_out_status = "normal"
        gross_minutes = int((session.check_out_at - session.check_in_at).total_seconds() / 60)
        session.worked_minutes = max(0, gross_minutes)
    else:
        session.check_out_at = None
        session.check_out_status = ""
        session.early_leave_minutes = 0
        session.overtime_minutes = 0
        session.worked_minutes = 0
        session.status = "open"

    session.note = session.note or _session_note_for_reconcile(session)
    _link_or_create_reconciled_event(check_in_log, session, emp, db)
    if check_out_log:
        _link_or_create_reconciled_event(check_out_log, session, emp, db)
    return session


def _assignments_for_log_timestamp(
    emp_code: str,
    timestamp: datetime | None,
    db: Session,
) -> list[tuple[ShiftAssignment, Shift]]:
    if not emp_code or not timestamp:
        return []
    candidate_dates = [timestamp.date(), timestamp.date() - timedelta(days=1)]
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date.in_(candidate_dates),
              ShiftAssignment.status.notin_(["cancelled", LEAVE_ASSIGNMENT_STATUS]),
          )
          .all()
    )
    result: list[tuple[ShiftAssignment, Shift]] = []
    seen: set[int] = set()
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if not shift:
            continue
        _start, _end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
        if checkin_from <= timestamp <= checkout_until and assignment.id not in seen:
            seen.add(assignment.id)
            result.append((assignment, shift))
    return result


def rebuild_sessions_for_log_change(
    emp_code: str,
    old_timestamp: datetime | None,
    new_timestamp: datetime | None,
    db: Session,
) -> list[int]:
    """Rebuild các session bị ảnh hưởng khi log thủ công được tạo/sửa/xóa."""
    targets: dict[int, tuple[ShiftAssignment, Shift]] = {}
    for timestamp in (old_timestamp, new_timestamp):
        for assignment, shift in _assignments_for_log_timestamp(emp_code, timestamp, db):
            targets[assignment.id] = (assignment, shift)
    session_ids: list[int] = []
    for assignment, shift in targets.values():
        session = rebuild_session_for_assignment(assignment, db, shift=shift)
        if session and session.id:
            session_ids.append(session.id)
    return session_ids


def find_open_session_for_time(
    emp_code: str,
    moment: datetime,
    db: Session,
) -> tuple[Optional[AttendanceSession], Optional[ShiftAssignment], Optional[Shift]]:
    """Ưu tiên session đang mở để lần chấm tiếp theo là checkout đúng ca."""
    candidate_dates = [moment.date(), moment.date() - timedelta(days=1)]
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date.in_(candidate_dates),
              ShiftAssignment.status.notin_(["cancelled", LEAVE_ASSIGNMENT_STATUS]),
          )
          .all()
    )

    best: tuple[datetime, ShiftAssignment, Shift, AttendanceSession] | None = None
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if not shift:
            continue
        _start, _end, _checkin_from, checkout_until = shift_window(assignment.work_date, shift)
        chain = consecutive_shift_chain_for_assignment(assignment, shift, db)
        if chain:
            _last_assignment, last_shift = chain[-1]
            _last_start, _last_end, _last_from, last_checkout_until = shift_window(_last_assignment.work_date, last_shift)
            checkout_until = max(checkout_until, last_checkout_until)
        session = (
            db.query(AttendanceSession)
              .filter(
                  AttendanceSession.shift_assignment_id == assignment.id,
                  AttendanceSession.check_in_at.isnot(None),
                  AttendanceSession.check_out_at.is_(None),
                  AttendanceSession.status != "cancelled",
              )
              .first()
        )
        if not session or not session.check_in_at:
            continue
        if session.check_in_at <= moment <= checkout_until:
            if best is None or session.check_in_at > best[0]:
                best = (session.check_in_at, assignment, shift, session)

    if best:
        return best[3], best[1], best[2]
    return None, None, None


def find_shift_assignment_for_checkin(emp_code: str, moment: datetime, db: Session) -> tuple[Optional[ShiftAssignment], Optional[Shift]]:
    """
    Tìm ca phù hợp để check-in.

    Xét cả hôm nay và hôm qua để bắt ca qua ngày. Session đã có check-in không
    được chọn lại, vì checkout được xử lý qua find_open_session_for_time().
    """
    candidate_dates = [moment.date(), moment.date() - timedelta(days=1)]
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date.in_(candidate_dates),
              ShiftAssignment.status.notin_(["cancelled", LEAVE_ASSIGNMENT_STATUS]),
          )
          .all()
    )

    best: tuple[tuple[int, float], ShiftAssignment, Shift] | None = None
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if not shift:
            continue
        start, end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
        if not (checkin_from <= moment <= checkout_until):
            continue
        session = (
            db.query(AttendanceSession)
              .filter_by(shift_assignment_id=assignment.id)
              .first()
        )
        if session and session.check_in_at:
            continue
        score = (0 if start <= moment <= end else 1, abs((moment - start).total_seconds()))
        if best is None or score < best[0]:
            best = (score, assignment, shift)

    if best:
        return best[1], best[2]
    return None, None


def find_shift_assignment_for_time(emp_code: str, moment: datetime, db: Session) -> tuple[Optional[ShiftAssignment], Optional[Shift]]:
    """
    Backward compatible wrapper: ưu tiên session đang mở, sau đó mới tìm ca check-in.
    """
    _session, assignment, shift = find_open_session_for_time(emp_code, moment, db)
    if assignment and shift:
        return assignment, shift
    return find_shift_assignment_for_checkin(emp_code, moment, db)


def find_leave_assignment_for_time(emp_code: str, moment: datetime, db: Session) -> tuple[Optional[ShiftAssignment], Optional[Shift]]:
    candidate_dates = [moment.date(), moment.date() - timedelta(days=1)]
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date.in_(candidate_dates),
              ShiftAssignment.status == LEAVE_ASSIGNMENT_STATUS,
          )
          .all()
    )
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if not shift:
            continue
        _start, _end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
        if checkin_from <= moment <= checkout_until:
            return assignment, shift
    return None, None


# ── Tính trạng thái check_in so với ca ──────────────────────────

def calc_status_for_shift(check_time: datetime, emp_code: str, db: Session) -> str:
    """
    Tính trạng thái check_in (đúng giờ / đi muộn N phút)
    dựa trên ca được phân công của nhân viên hôm đó.
    
    Dùng thay thế cho _calc_status() cũ trong attendance.py.
    """
    assignment, shift = find_shift_assignment_for_checkin(emp_code, check_time, db)
    if shift and assignment:
        work_dt, _end, _from, _until = shift_window(assignment.work_date, shift)
        threshold = shift.late_threshold_minutes if shift.late_threshold_minutes is not None else settings.CHECKIN_GRACE_MINUTES
        shift_name = shift.name
    else:
        return "Chưa có ca phân công"
    late_minutes = int((check_time - work_dt).total_seconds() / 60)

    if late_minutes > threshold:
        return f"Đi muộn {late_minutes - threshold} phút ({shift_name})"
    return f"Đúng giờ ({shift_name})"


# ── Seed dữ liệu mẫu (gọi 1 lần khi init) ───────────────────────

def seed_default_shifts(db: Session):
    """Tạo các ca mặc định nếu bảng shifts còn trống."""
    if db.query(func.count(Shift.id)).scalar() > 0:
        return

    defaults = [
        {"name": "Ca sáng", "code": "morning", "work_start": "06:00", "work_end": "11:00", "late_threshold_minutes": settings.CHECKIN_GRACE_MINUTES, "break_minutes": 0},
        {"name": "Ca trưa", "code": "lunch",   "work_start": "10:00", "work_end": "15:00", "late_threshold_minutes": settings.CHECKIN_GRACE_MINUTES, "break_minutes": 30},
        {"name": "Ca tối",  "code": "evening", "work_start": "16:00", "work_end": "22:00", "late_threshold_minutes": settings.CHECKIN_GRACE_MINUTES, "break_minutes": 30},
        {"name": "Ca đêm",  "code": "night",   "work_start": "22:00", "work_end": "06:00", "late_threshold_minutes": settings.CHECKIN_GRACE_MINUTES, "break_minutes": 30, "is_overnight": True},
    ]
    for d in defaults:
        db.add(Shift(**d))
    db.commit()
