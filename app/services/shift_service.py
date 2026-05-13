"""
app/services/shift_service.py
Business logic cho ca làm việc.
"""

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment


# ── Helpers ──────────────────────────────────────────────────────

def _shift_to_dict(s: Shift) -> dict:
    return {
        "id":                    s.id,
        "branch_id":             s.branch_id,
        "name":                  s.name,
        "code":                  s.code,
        "work_start":            s.work_start,
        "work_end":              s.work_end,
        "late_threshold_minutes": s.late_threshold_minutes,
        "early_checkin_minutes":  s.early_checkin_minutes,
        "auto_checkout_minutes":  s.auto_checkout_minutes,
        "break_minutes":          s.break_minutes,
        "is_overnight":           s.is_overnight,
        "is_active":             s.is_active,
        "note":                  s.note or "",
        "created_at":            s.created_at.isoformat() if s.created_at else None,
    }


def _assignment_to_dict(a: ShiftAssignment, shift: Optional[Shift] = None) -> dict:
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
    }
    if shift:
        d["shift"] = _shift_to_dict(shift)
    return d


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
    for field in (
        "branch_id", "name", "work_start", "work_end",
        "late_threshold_minutes", "early_checkin_minutes",
        "auto_checkout_minutes", "break_minutes", "is_overnight",
        "note", "is_active",
    ):
        if field in data:
            setattr(s, field, data[field])
    if "work_start" in data or "work_end" in data:
        s.is_overnight = _is_overnight(s.work_start, s.work_end)
    db.commit()
    db.refresh(s)
    return _shift_to_dict(s)


def delete_shift(shift_id: int, db: Session) -> bool:
    s = db.query(Shift).filter_by(id=shift_id).first()
    if not s:
        return False
    # Soft delete: chỉ deactivate, giữ lịch sử assignment
    s.is_active = False
    db.commit()
    return True


# ── CRUD Phân công ca ────────────────────────────────────────────

def assign_shift(emp_code: str, shift_id: int, work_date: date,
                 assigned_by: str = "", note: str = "", db: Session = None) -> dict:
    """
    Phân công ca cho nhân viên vào ngày cụ thể.
    Nếu đã có cùng ca trong ngày → cập nhật (upsert).
    Nhà hàng có thể phân nhiều ca khác nhau cho cùng một nhân viên trong ngày.
    """
    emp = db.query(Employee).filter_by(emp_code=emp_code).first()
    shift = db.query(Shift).filter_by(id=shift_id).first()
    if not shift:
        raise ValueError(f"Không tìm thấy ca #{shift_id}")

    existing = (
        db.query(ShiftAssignment)
          .filter_by(emp_code=emp_code, work_date=work_date, shift_id=shift_id)
          .first()
    )
    if existing:
        existing.employee_id = emp.id if emp else existing.employee_id
        existing.branch_id   = shift.branch_id or (emp.branch_id if emp else existing.branch_id)
        existing.assigned_by = assigned_by
        existing.note        = note
        existing.status      = "scheduled"
        db.commit()
        db.refresh(existing)
        a = existing
    else:
        a = ShiftAssignment(
            employee_id = emp.id if emp else None,
            branch_id   = shift.branch_id or (emp.branch_id if emp else None),
            emp_code    = emp_code,
            shift_id    = shift_id,
            work_date   = work_date,
            status      = "scheduled",
            assigned_by = assigned_by,
            note        = note,
        )
        db.add(a)
        db.commit()
        db.refresh(a)

    return _assignment_to_dict(a, shift)


def bulk_assign_shift(emp_codes: list[str], shift_id: int,
                      dates: list[date], assigned_by: str = "",
                      db: Session = None) -> int:
    """
    Phân công ca hàng loạt: nhiều nhân viên × nhiều ngày.
    Trả về số assignment đã tạo/cập nhật.
    """
    count = 0
    for emp_code in emp_codes:
        for d in dates:
            assign_shift(emp_code, shift_id, d, assigned_by=assigned_by, db=db)
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
        result.append(_assignment_to_dict(a, shift))
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
        result.append(_assignment_to_dict(a, shift))
    return result


def delete_assignment(assignment_id: int, db: Session) -> bool:
    a = db.query(ShiftAssignment).filter_by(id=assignment_id).first()
    if not a:
        return False
    db.delete(a)
    db.commit()
    return True


# ── Core: Lấy ca của nhân viên cho 1 ngày ───────────────────────

def get_shift_for_employee(emp_code: str, work_date: date, db: Session) -> dict:
    """
    Trả về thông tin ca làm việc của 1 nhân viên trong 1 ngày.
    
    Thứ tự ưu tiên:
    1. ShiftAssignment cụ thể cho ngày đó
    2. Fallback về config WORK_START / WORK_END trong .env
    
    Luôn trả về dict với work_start, work_end, late_threshold_minutes.
    """
    assignment = (
        db.query(ShiftAssignment)
          .filter_by(emp_code=emp_code, work_date=work_date)
          .filter(ShiftAssignment.status != "cancelled")
          .order_by(ShiftAssignment.id)
          .first()
    )

    if assignment:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if shift:
            return {
                "source":      "assignment",
                "shift_id":    shift.id,
                "shift_name":  shift.name,
                "shift_code":  shift.code,
                "work_start":  shift.work_start,
                "work_end":    shift.work_end,
                "late_threshold_minutes": shift.late_threshold_minutes,
            }

    # Fallback
    return {
        "source":      "default",
        "shift_id":    None,
        "shift_name":  "Mặc định",
        "shift_code":  "default",
        "work_start":  settings.WORK_START,
        "work_end":    settings.WORK_END,
        "late_threshold_minutes": settings.LATE_THRESHOLD_MINUTES,
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


def find_shift_assignment_for_time(emp_code: str, moment: datetime, db: Session) -> tuple[Optional[ShiftAssignment], Optional[Shift]]:
    """
    Tìm ca phù hợp nhất tại thời điểm chấm công.

    Xét cả hôm nay và hôm qua để bắt ca qua ngày. Ưu tiên ca có moment nằm trong
    cửa sổ check-in/check-out; nếu nhiều ca cùng hợp lệ thì chọn ca có start gần
    moment nhất.
    """
    candidate_dates = [moment.date(), moment.date() - timedelta(days=1)]
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date.in_(candidate_dates),
              ShiftAssignment.status != "cancelled",
          )
          .all()
    )

    best: tuple[float, ShiftAssignment, Shift] | None = None
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if not shift:
            continue
        start, _end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
        if checkin_from <= moment <= checkout_until:
            score = abs((moment - start).total_seconds())
            if best is None or score < best[0]:
                best = (score, assignment, shift)

    if best:
        return best[1], best[2]
    return None, None


# ── Tính trạng thái check_in so với ca ──────────────────────────

def calc_status_for_shift(check_time: datetime, emp_code: str, db: Session) -> str:
    """
    Tính trạng thái check_in (đúng giờ / đi muộn N phút)
    dựa trên ca được phân công của nhân viên hôm đó.
    
    Dùng thay thế cho _calc_status() cũ trong attendance.py.
    """
    assignment, shift = find_shift_assignment_for_time(emp_code, check_time, db)
    if shift and assignment:
        work_dt, _end, _from, _until = shift_window(assignment.work_date, shift)
        threshold = shift.late_threshold_minutes
        shift_name = shift.name
    else:
        shift_info = get_shift_for_employee(emp_code, check_time.date(), db)
        h, m = map(int, shift_info["work_start"].split(":"))
        work_dt = check_time.replace(hour=h, minute=m, second=0, microsecond=0)
        threshold = shift_info["late_threshold_minutes"]
        shift_name = shift_info["shift_name"]
    late_minutes = int((check_time - work_dt).total_seconds() / 60)

    if late_minutes > threshold:
        return f"Đi muộn {late_minutes} phút ({shift_name})"
    return f"Đúng giờ ({shift_name})"


# ── Seed dữ liệu mẫu (gọi 1 lần khi init) ───────────────────────

def seed_default_shifts(db: Session):
    """Tạo các ca mặc định nếu bảng shifts còn trống."""
    if db.query(func.count(Shift.id)).scalar() > 0:
        return

    defaults = [
        {"name": "Ca sáng", "code": "morning", "work_start": "06:00", "work_end": "11:00", "late_threshold_minutes": 10, "break_minutes": 0},
        {"name": "Ca trưa", "code": "lunch",   "work_start": "10:00", "work_end": "15:00", "late_threshold_minutes": 10, "break_minutes": 30},
        {"name": "Ca tối",  "code": "evening", "work_start": "16:00", "work_end": "22:00", "late_threshold_minutes": 10, "break_minutes": 30},
        {"name": "Ca đêm",  "code": "night",   "work_start": "22:00", "work_end": "06:00", "late_threshold_minutes": 10, "break_minutes": 30, "is_overnight": True},
    ]
    for d in defaults:
        db.add(Shift(**d))
    db.commit()
