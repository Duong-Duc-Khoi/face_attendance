"""
app/services/work_calendar.py
Logic nghiệp vụ lịch làm việc và tính trạng thái ngày công.
"""

import json
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.attendance import AttendanceLog
from app.models.calendar import WorkCalendar
from app.models.leave import LeaveRequest


# ── Helpers ──────────────────────────────────────────────────────

def _parse_time(t: str) -> tuple[int, int]:
    """'08:30' → (8, 30)"""
    h, m = t.split(":")
    return int(h), int(m)


def _default_work_days() -> set[int]:
    """Trả về set ISO weekday từ config. 1=Thứ 2 ... 7=CN"""
    raw = getattr(settings, "WORK_DAYS", "1,2,3,4,5")
    return {int(x.strip()) for x in raw.split(",")}


# ── Lấy thông tin 1 ngày ─────────────────────────────────────────

def get_calendar_day(d: date, db: Session) -> dict:
    """
    Trả về thông tin ngày làm việc của 1 ngày cụ thể.
    Ưu tiên: WorkCalendar override > mặc định config.
    """
    override: Optional[WorkCalendar] = db.query(WorkCalendar).filter_by(date=d).first()

    if override:
        return {
            "date":       d.isoformat(),
            "day_type":   override.day_type,
            "work_start": override.work_start or settings.WORK_START,
            "work_end":   override.work_end   or settings.WORK_END,
            "label":      override.label or "",
            "is_override": True,
        }

    # Mặc định
    iso_weekday = d.isoweekday()   # 1=Mon ... 7=Sun
    work_days   = _default_work_days()
    day_type    = "full" if iso_weekday in work_days else "off"

    return {
        "date":       d.isoformat(),
        "day_type":   day_type,
        "work_start": settings.WORK_START,
        "work_end":   settings.WORK_END,
        "label":      "",
        "is_override": False,
    }


def get_calendar_month(year: int, month: int, db: Session) -> list[dict]:
    """Trả về thông tin tất cả ngày trong tháng."""
    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    result = []
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        result.append(get_calendar_day(d, db))
    return result


# ── Tính trạng thái ngày công của 1 nhân viên ───────────────────

def get_day_status(emp_code: str, d: date, db: Session) -> dict:
    """
    Tính trạng thái ngày công realtime.

    Trả về dict:
      status: present | late | approved_leave | approved_leave_half |
              approved_remote | pending_leave | pending_remote |
              absent | day_off | holiday | overtime | future
      work_value: float — số công (1.0 / 0.5 / 0)
      label: str — nhãn hiển thị
      detail: str — chi tiết thêm (vd: "Muộn 12 phút")
    """
    today = date.today()

    # 1. Ngày tương lai
    if d > today:
        cal = get_calendar_day(d, db)
        return {"status": "future", "work_value": 0.0,
                "label": "Chưa đến", "detail": cal.get("label", ""),
                "day_type": cal["day_type"]}

    cal = get_calendar_day(d, db)
    day_type = cal["day_type"]

    # 2. Ngày nghỉ chính thức / ngày lễ
    if day_type in ("off", "holiday"):
        label_map = {"off": "Ngày nghỉ", "holiday": "Ngày lễ"}
        return {"status": day_type, "work_value": 0.0,
                "label": label_map[day_type],
                "detail": cal.get("label", ""),
                "day_type": day_type}

    # 3. Lấy log chấm công trong ngày
    day_start = datetime.combine(d, datetime.min.time())
    day_end   = datetime.combine(d, datetime.max.time())
    logs = db.query(AttendanceLog).filter(
        AttendanceLog.emp_code == emp_code,
        AttendanceLog.timestamp >= day_start,
        AttendanceLog.timestamp <= day_end,
    ).order_by(AttendanceLog.timestamp).all()

    check_in_log  = next((l for l in logs if l.check_type == "check_in"), None)
    check_out_log = next((l for l in logs if l.check_type == "check_out"), None)

    # 4. Lấy đơn nghỉ/remote có hiệu lực trong ngày
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

    work_start_h, work_start_m = _parse_time(cal["work_start"])
    late_threshold = getattr(settings, "LATE_THRESHOLD_MINUTES", 15)
    half_cutoff_h, half_cutoff_m = _parse_time(
        getattr(settings, "HALF_DAY_CUTOFF", "12:00")
    )

    # 5. Có check_in → present / late (điểm danh thắng tất cả)
    if check_in_log:
        ci = check_in_log.timestamp
        total_late = (ci.hour * 60 + ci.minute) - (work_start_h * 60 + work_start_m)

        # Với ngày half_am/half_pm từ lịch công ty
        if day_type == "half_am":
            # Chỉ cần vào buổi sáng
            status = "present"
            work_value = 0.5
            detail = ""
            if total_late > late_threshold:
                status = "late"
                detail = f"Muộn {total_late} phút"
            return {"status": status, "work_value": work_value,
                    "label": "Có mặt (½ ngày)", "detail": detail,
                    "day_type": day_type}

        if total_late > late_threshold:
            return {"status": "late", "work_value": 1.0,
                    "label": f"Muộn {total_late} phút",
                    "detail": f"Check-in {ci.strftime('%H:%M')}",
                    "day_type": day_type}

        # Có đơn remote approved nhưng vẫn vào → present bình thường
        return {"status": "present", "work_value": 1.0,
                "label": "Có mặt" + (" (Remote)" if active_remote else ""),
                "detail": "",
                "day_type": day_type}

    # 6. Không có check_in — xét đơn
    # 6a. Nghỉ phép approved
    if active_leave:
        req, half = active_leave
        if half in ("am", "pm"):
            return {"status": "approved_leave_half", "work_value": 0.0,
                    "label": f"Nghỉ phép ½ ngày ({'Sáng' if half=='am' else 'Chiều'})",
                    "detail": req.reason or "",
                    "day_type": day_type}
        return {"status": "approved_leave", "work_value": 0.0,
                "label": "Nghỉ phép", "detail": req.reason or "",
                "day_type": day_type}

    # 6b. Remote approved
    if active_remote:
        req, half = active_remote
        work_val = 0.5 if half in ("am", "pm") else 1.0
        return {"status": "approved_remote", "work_value": work_val,
                "label": "🏠 Remote" + (" ½ ngày" if half else ""),
                "detail": req.reason or "",
                "day_type": day_type}

    # 6c. Đơn pending leave → tính absent (chưa duyệt)
    # (nhưng nếu manager duyệt sau thì query lần sau sẽ thành approved_leave)
    if pending_leave:
        return {"status": "pending_leave", "work_value": 0.0,
                "label": "⏳ Chờ duyệt nghỉ",
                "detail": "Đơn chưa được duyệt",
                "day_type": day_type}

    # 6d. Đơn pending remote → absent (không điểm danh, chưa duyệt)
    if pending_remote:
        return {"status": "absent", "work_value": 0.0,
                "label": "Vắng mặt",
                "detail": "Đơn remote chưa được duyệt",
                "day_type": day_type}

    # 6e. Overtime — không phạt nếu không vào
    if day_type == "overtime":
        return {"status": "overtime_absent", "work_value": 0.0,
                "label": "Không tăng ca", "detail": cal.get("label", ""),
                "day_type": day_type}

    # 6f. Vắng không phép
    return {"status": "absent", "work_value": 0.0,
            "label": "Vắng mặt", "detail": "",
            "day_type": day_type}


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
            cal = get_calendar_day(d, db)
            if cal["day_type"] in ("off", "holiday"):
                continue
            total_work_days += 1
            st = get_day_status(emp_code, d, db)
            s = st["status"]
            if s == "present":
                present_days += 1.0
            elif s == "late":
                present_days += 1.0
                late_days += 1
            elif s in ("approved_leave", "approved_leave_half"):
                approved_leave += st["work_value"] if s == "approved_leave_half" else 1.0
                # approved_leave_half = 0 công, nhưng tính 0.5 ngày nghỉ phép
                approved_leave = approved_leave  # đã đúng
            elif s == "approved_remote":
                remote_days += st["work_value"]
            elif s == "absent":
                absent_days += 1.0

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
    today = date.today()
    _, days_in = monthrange(year, month)
    days = []
    for day in range(1, days_in + 1):
        d = date(year, month, day)
        cal = get_calendar_day(d, db)
        st  = get_day_status(emp_code, d, db) if d <= today else {
            "status": "future", "work_value": 0.0, "label": "Chưa đến", "detail": ""}
        days.append({**cal, **st, "day": day})
    return {"year": year, "month": month, "days": days}
