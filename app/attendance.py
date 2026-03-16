from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.database import SessionLocal, AttendanceLog, Employee

# Cấu hình
COOLDOWN_MINUTES  = 5     # Chặn chấm công lại trong vòng N phút
WORK_START        = "08:30"  # Giờ bắt đầu làm (HH:MM)
LATE_THRESHOLD    = 15    # Trễ quá N phút = đi muộn


def process_attendance(emp_code: str, confidence: float,
                       capture_path: str = "") -> dict | None:
    """
    Xử lý sự kiện chấm công từ kết quả nhận diện.

    Trả về dict nếu có sự kiện mới, None nếu trong cooldown hoặc lỗi.
    """
    db = SessionLocal()
    try:
        # 1. Kiểm tra nhân viên tồn tại và đang active
        emp = db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()
        if not emp:
            return None

        now = datetime.now()

        # 2. Kiểm tra cooldown — chống chấm công liên tục
        last_log = (
            db.query(AttendanceLog)
              .filter_by(emp_code=emp_code)
              .order_by(AttendanceLog.timestamp.desc())
              .first()
        )
        if last_log:
            diff = now - last_log.timestamp
            if diff < timedelta(minutes=COOLDOWN_MINUTES):
                return None   # Còn trong cooldown, bỏ qua

        # 3. Xác định check_in hay check_out dựa trên số lần hôm nay
        today_start = datetime(now.year, now.month, now.day, 0, 0, 0)
        today_logs  = (
            db.query(AttendanceLog)
              .filter_by(emp_code=emp_code)
              .filter(AttendanceLog.timestamp >= today_start)
              .all()
        )
        check_type = "check_out" if (len(today_logs) % 2 == 1) else "check_in"

        # 4. Tính trạng thái (đúng giờ / đi muộn) cho check_in
        status = _calc_status(now, check_type)

        # 5. Lưu log
        log = AttendanceLog(
            employee_id  = emp.id,
            emp_code     = emp_code,
            emp_name     = emp.name,
            department   = emp.department,
            check_type   = check_type,
            timestamp    = now,
            confidence   = round(confidence, 4),
            capture_path = capture_path,
            note         = status,
        )
        db.add(log)
        db.commit()

        return {
            "id":          log.id,
            "emp_code":    emp_code,
            "name":        emp.name,
            "department":  emp.department,
            "position":    emp.position,
            "check_type":  check_type,
            "time":        now.strftime("%H:%M:%S"),
            "date":        now.strftime("%d/%m/%Y"),
            "timestamp":   now.isoformat(),
            "confidence":  round(confidence, 4),
            "status":      status,
            "avatar_url":  emp.avatar_url or "",
        }

    except Exception as e:
        db.rollback()
        print(f"  ✗ Lỗi process_attendance: {e}")
        return None
    finally:
        db.close()


def _calc_status(now: datetime, check_type: str) -> str:
    """Tính trạng thái đi muộn / đúng giờ"""
    if check_type != "check_in":
        return ""
    h, m    = map(int, WORK_START.split(":"))
    work_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta   = (now - work_dt).total_seconds() / 60
    if delta > LATE_THRESHOLD:
        return f"Đi muộn {int(delta)} phút"
    return "Đúng giờ"


# ──────────────────────────────────────────
# Query helpers dùng cho API routes
# ──────────────────────────────────────────

def get_logs_by_date(date_str: str, emp_code: str = None) -> list:
    """Lấy log chấm công theo ngày (YYYY-MM-DD)"""
    db = SessionLocal()
    try:
        dt    = datetime.strptime(date_str, "%Y-%m-%d")
        start = dt.replace(hour=0, minute=0, second=0)
        end   = dt.replace(hour=23, minute=59, second=59)
        q     = db.query(AttendanceLog).filter(
            AttendanceLog.timestamp >= start,
            AttendanceLog.timestamp <= end
        )
        if emp_code:
            q = q.filter_by(emp_code=emp_code)
        logs = q.order_by(AttendanceLog.timestamp.desc()).all()
        return [_log_to_dict(l) for l in logs]
    finally:
        db.close()


def get_summary_today() -> dict:
    """Thống kê hôm nay"""
    db = SessionLocal()
    try:
        now   = datetime.now()
        start = now.replace(hour=0, minute=0, second=0)
        logs  = db.query(AttendanceLog).filter(
            AttendanceLog.timestamp >= start
        ).all()

        checked_in  = {l.emp_code for l in logs if l.check_type == "check_in"}
        checked_out = {l.emp_code for l in logs if l.check_type == "check_out"}
        total_emp   = db.query(Employee).filter_by(is_active=True).count()

        return {
            "date":          now.strftime("%d/%m/%Y"),
            "total_emp":     total_emp,
            "checked_in":    len(checked_in),
            "checked_out":   len(checked_out),
            "absent":        total_emp - len(checked_in),
            "total_logs":    len(logs),
        }
    finally:
        db.close()


def _log_to_dict(log: AttendanceLog) -> dict:
    return {
        "id":           log.id,
        "emp_code":     log.emp_code,
        "name":         log.emp_name,
        "department":   log.department,
        "check_type":   log.check_type,
        "time":         log.timestamp.strftime("%H:%M:%S"),
        "date":         log.timestamp.strftime("%d/%m/%Y"),
        "timestamp":    log.timestamp.isoformat(),
        "confidence":   log.confidence,
        "status":       log.note,
    }
