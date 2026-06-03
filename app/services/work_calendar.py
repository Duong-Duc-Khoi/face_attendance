"""
app/services/work_calendar.py
Logic nghiệp vụ lịch làm việc và tính trạng thái ngày công.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.attendance import AttendanceLog, AttendanceSession
from app.models.calendar import WorkCalendar, WorkCalendarConfig
from app.models.employee import Employee
from app.models.leave import LeaveRequest
from app.models.shift import Shift, ShiftAssignment
from app.services.employee_branch_history import branch_for_employee_on
from app.services.shift_service import shift_window


def _parse_work_days(raw: str | None) -> set[int]:
    """Trả về set ISO weekday từ chuỗi config. 1=Thứ 2 ... 7=CN"""
    result = set()
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            day = int(item)
        except ValueError:
            continue
        if 1 <= day <= 7:
            result.add(day)
    return result or {1, 2, 3, 4, 5, 6, 7}


def _default_work_days(db: Session, branch_id: int | None = None) -> set[int]:
    """Ưu tiên lịch mở cửa mặc định theo chi nhánh, fallback về cấu hình toàn chuỗi."""
    if branch_id is not None:
        cfg = db.query(WorkCalendarConfig).filter_by(branch_id=branch_id).first()
        if cfg and cfg.work_days:
            return _parse_work_days(cfg.work_days)
    return _parse_work_days(getattr(settings, "WORK_DAYS", "1,2,3,4,5,6,7"))


# ── Lấy thông tin 1 ngày ─────────────────────────────────────────

def get_calendar_day(d: date, db: Session, branch_id: int | None = None) -> dict:
    """
    Trả về thông tin ngày làm việc của 1 ngày cụ thể.
    Ưu tiên: WorkCalendar override > mặc định config.
    """
    override: Optional[WorkCalendar] = None
    if branch_id is not None:
        override = db.query(WorkCalendar).filter_by(date=d, branch_id=branch_id).first()
    if not override:
        override = db.query(WorkCalendar).filter_by(date=d, branch_id=None).first()

    if override:
        return {
            "date":       d.isoformat(),
            "branch_id":   override.branch_id,
            "day_type":   override.day_type,
            "label":      override.label or "",
            "pay_multiplier": float(override.pay_multiplier or 1.0),
            "salary_note": override.salary_note or "",
            "is_override": True,
        }

    # Mặc định
    iso_weekday = d.isoweekday()   # 1=Mon ... 7=Sun
    work_days   = _default_work_days(db, branch_id)
    day_type    = "full" if iso_weekday in work_days else "off"

    return {
        "date":       d.isoformat(),
        "branch_id":   branch_id,
        "day_type":   day_type,
        "label":      "",
        "pay_multiplier": 1.0,
        "salary_note": "",
        "is_override": False,
    }


def get_calendar_month(year: int, month: int, db: Session, branch_id: int | None = None) -> list[dict]:
    """Trả về thông tin tất cả ngày trong tháng."""
    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    result = []
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        result.append(get_calendar_day(d, db, branch_id))
    return result


def _employee_branch_id(emp_code: str, db: Session, on_date: date | None = None) -> int | None:
    emp = db.query(Employee).filter_by(emp_code=emp_code).first()
    if on_date:
        historical_branch = branch_for_employee_on(
            db,
            employee_id=emp.id if emp else None,
            emp_code=emp_code,
            at=on_date,
        )
        if historical_branch:
            return historical_branch
    return emp.branch_id if emp else None


def _assigned_shift_rows(emp_code: str, d: date, db: Session) -> list[tuple[ShiftAssignment, Shift]]:
    rows = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.emp_code == emp_code,
              ShiftAssignment.work_date == d,
              ShiftAssignment.status != "cancelled",
          )
          .order_by(ShiftAssignment.id)
          .all()
    )
    result = []
    for assignment in rows:
        shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
        if shift:
            result.append((assignment, shift))
    return result


def _session_or_logs_for_assignment(
    emp_code: str,
    assignment: ShiftAssignment,
    shift: Shift,
    db: Session,
) -> tuple[datetime | None, datetime | None]:
    session = (
        db.query(AttendanceSession)
          .filter_by(shift_assignment_id=assignment.id)
          .first()
    )
    if session:
        return session.check_in_at, session.check_out_at

    _start, _end, checkin_from, checkout_until = shift_window(assignment.work_date, shift)
    logs = (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.emp_code == emp_code,
              AttendanceLog.timestamp >= checkin_from,
              AttendanceLog.timestamp <= checkout_until,
          )
          .order_by(AttendanceLog.timestamp.asc())
          .all()
    )
    check_in = next((log.timestamp for log in logs if log.check_type == "check_in"), None)
    check_out_candidates = [
        log.timestamp for log in logs
        if log.check_type == "check_out" and (not check_in or log.timestamp >= check_in)
    ]
    return check_in, (check_out_candidates[-1] if check_out_candidates else None)


# ── Tính trạng thái ngày công của 1 nhân viên ───────────────────

def get_day_status(emp_code: str, d: date, db: Session) -> dict:
    """
    Tính trạng thái ngày công realtime.

    Trả về dict:
      status: present | late | approved_leave | approved_leave_half |
              approved_remote | pending_leave | pending_remote |
              absent | day_off | holiday | future
      work_value: float — số công (1.0 / 0.5 / 0)
      label: str — nhãn hiển thị
      detail: str — chi tiết thêm (vd: "Muộn 12 phút")
    """
    today = date.today()
    assigned_shifts = _assigned_shift_rows(emp_code, d, db)
    total_shifts = len(assigned_shifts)
    branch_id = assigned_shifts[0][0].branch_id if assigned_shifts else None
    if branch_id is None:
        branch_id = _employee_branch_id(emp_code, db, d)

    # 1. Ngày tương lai
    if d > today:
        cal = get_calendar_day(d, db, branch_id)
        return {"status": "future", "work_value": 0.0,
                "label": "Chưa đến", "detail": cal.get("label", ""),
                "day_type": "scheduled" if total_shifts else cal["day_type"],
                "total_shifts": total_shifts}

    cal = get_calendar_day(d, db, branch_id)
    day_type = "scheduled" if total_shifts else cal["day_type"]

    # 2. Lấy đơn nghỉ/remote có hiệu lực trong ngày
    date_str = d.isoformat()
    leave_requests = db.query(LeaveRequest).filter(
        LeaveRequest.emp_code == emp_code,
        LeaveRequest.status.in_(["approved", "pending"]),
    ).all()

    active_leave  = None   # approved leave
    active_remote = None   # approved remote
    pending_leave = None
    pending_remote = None

    for req in leave_requests:
        dates_info = req.get_dates()
        day_entry = next((x for x in dates_info if x["date"] == date_str), None)
        if not day_entry:
            continue
        half = day_entry.get("half")  # None | "am" | "pm"

        if req.status == "approved":
            if req.request_type == "leave":
                active_leave = (req, half)
            elif req.request_type == "remote":
                active_remote = (req, half)
        elif req.status == "pending":
            if req.request_type == "leave":
                pending_leave = (req, half)
            elif req.request_type == "remote":
                pending_remote = (req, half)

    if not total_shifts:
        if active_leave:
            req, _half = active_leave
            return {"status": "approved_leave", "work_value": 0.0,
                    "label": "Nghỉ phép", "detail": req.reason or "",
                    "day_type": day_type, "total_shifts": 0}
        label_map = {"holiday": "Ngày lễ", "off": "Không có ca"}
        return {"status": "day_off", "work_value": 0.0,
                "label": label_map.get(cal["day_type"], "Không có ca"),
                "detail": cal.get("label", ""),
                "day_type": day_type,
                "total_shifts": 0}

    shift_states = []
    for assignment, shift in assigned_shifts:
        check_in_at, check_out_at = _session_or_logs_for_assignment(emp_code, assignment, shift, db)
        shift_start, _shift_end, _from, _until = shift_window(assignment.work_date, shift)
        raw_late = int((check_in_at - shift_start).total_seconds() / 60) if check_in_at else 0
        is_late = bool(check_in_at and raw_late > (shift.late_threshold_minutes or 0))
        shift_states.append({
            "assignment": assignment,
            "shift": shift,
            "check_in_at": check_in_at,
            "check_out_at": check_out_at,
            "late_minutes": raw_late if is_late else 0,
            "is_late": is_late,
        })

    checked_in = [s for s in shift_states if s["check_in_at"]]
    late_items = [s for s in checked_in if s["is_late"]]
    if checked_in:
        if late_items:
            max_late = max(s["late_minutes"] for s in late_items)
            return {"status": "late", "work_value": len(checked_in),
                    "label": f"Muộn {max_late} phút",
                    "detail": f"{len(checked_in)}/{total_shifts} ca đã vào",
                    "day_type": day_type,
                    "total_shifts": total_shifts}

        return {"status": "present", "work_value": len(checked_in),
                "label": "Có mặt" + (" (Remote)" if active_remote else ""),
                "detail": f"{len(checked_in)}/{total_shifts} ca đã vào",
                "day_type": day_type,
                "total_shifts": total_shifts}

    # 3. Không có check_in — xét đơn
    if active_leave:
        req, half = active_leave
        if half in ("am", "pm"):
            return {"status": "approved_leave_half", "work_value": 0.0,
                    "label": f"Nghỉ phép ½ ngày ({'Sáng' if half=='am' else 'Chiều'})",
                    "detail": req.reason or "",
                    "day_type": day_type,
                    "total_shifts": total_shifts}
        return {"status": "approved_leave", "work_value": 0.0,
                "label": "Nghỉ phép", "detail": req.reason or "",
                "day_type": day_type,
                "total_shifts": total_shifts}

    # 6b. Remote approved
    if active_remote:
        req, half = active_remote
        work_val = 0.5 if half in ("am", "pm") else total_shifts
        return {"status": "approved_remote", "work_value": work_val,
                "label": "🏠 Remote" + (" ½ ngày" if half else ""),
                "detail": req.reason or "",
                "day_type": day_type,
                "total_shifts": total_shifts}

    # 6c. Đơn pending leave → tính absent (chưa duyệt)
    # (nhưng nếu manager duyệt sau thì query lần sau sẽ thành approved_leave)
    if pending_leave:
        return {"status": "pending_leave", "work_value": 0.0,
                "label": "⏳ Chờ duyệt nghỉ",
                "detail": "Đơn chưa được duyệt",
                "day_type": day_type,
                "total_shifts": total_shifts}

    # 6d. Đơn pending remote → absent (không điểm danh, chưa duyệt)
    if pending_remote:
        return {"status": "absent", "work_value": 0.0,
                "label": "Vắng mặt",
                "detail": "Đơn remote chưa được duyệt",
                "day_type": day_type,
                "total_shifts": total_shifts}

    return {"status": "absent", "work_value": 0.0,
            "label": "Chưa vào ca", "detail": f"0/{total_shifts} ca đã vào",
            "day_type": day_type,
            "total_shifts": total_shifts}


# ── Thống kê nhân viên ───────────────────────────────────────────

def get_employee_stats(emp_code: str, year: int, db: Session) -> dict:
    """Tổng hợp thống kê năm cho 1 nhân viên."""
    from calendar import monthrange

    today = date.today()
    total_work_days   = 0
    present_days      = 0.0
    late_days         = 0
    approved_leave    = 0.0
    absent_days       = 0.0
    remote_days       = 0.0

    for month in range(1, 13):
        _, days_in = monthrange(year, month)
        for day in range(1, days_in + 1):
            d = date(year, month, day)
            if d > today:
                break
            st = get_day_status(emp_code, d, db)
            shift_count = st.get("total_shifts", 0)
            if not shift_count:
                continue
            total_work_days += shift_count
            s = st["status"]
            if s == "present":
                present_days += st.get("work_value", 0.0)
            elif s == "late":
                present_days += st.get("work_value", 1.0)
                late_days += 1
            elif s in ("approved_leave", "approved_leave_half"):
                approved_leave += 0.5 if s == "approved_leave_half" else shift_count
            elif s == "approved_remote":
                remote_days += st["work_value"]
            elif s == "absent":
                absent_days += shift_count

    return {
        "emp_code":        emp_code,
        "year":            year,
        "total_work_days": total_work_days,
        "present_days":    round(present_days, 1),
        "late_days":       late_days,
        "approved_leave":  round(approved_leave, 1),
        "absent_days":     round(absent_days, 1),
        "remote_days":     round(remote_days, 1),
        "total_absent":    round(approved_leave + absent_days, 1),
    }


def get_employee_stats_month(emp_code: str, year: int, month: int, db: Session) -> dict:
    """Thống kê 1 tháng chi tiết từng ngày."""
    from calendar import monthrange
    _, days_in = monthrange(year, month)
    days = []
    for day in range(1, days_in + 1):
        d = date(year, month, day)
        branch_id = _employee_branch_id(emp_code, db, d)
        cal = get_calendar_day(d, db, branch_id)
        st = get_day_status(emp_code, d, db)
        days.append({**cal, **st, "day": day})
    return {"year": year, "month": month, "days": days}
