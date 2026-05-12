"""
app/services/attendance.py
Business logic chấm công: xử lý sự kiện, tính trạng thái, query helpers.
"""
from sqlalchemy import or_
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.employee import Employee
from app.models.attendance import AttendanceLog


def process_attendance(emp_code: str, confidence: float, capture_path: str = "") -> dict | None:
    """
    Xử lý 1 sự kiện chấm công từ kết quả nhận diện.
    Trả về dict nếu ghi log thành công, None nếu cooldown / lỗi.
    """
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()
        if not emp:
            return None

        now = datetime.now()

        # Cooldown — chống spam
        last_log = (
            db.query(AttendanceLog)
              .filter_by(emp_code=emp_code)
              .order_by(AttendanceLog.timestamp.desc())
              .first()
        )
        if last_log and (now - last_log.timestamp) < timedelta(minutes=settings.COOLDOWN_MINUTES):
            return None

        # check_in / check_out — dựa vào số log chẵn/lẻ trong ngày
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = (
            db.query(AttendanceLog)
              .filter_by(emp_code=emp_code)
              .filter(AttendanceLog.timestamp >= today_start)
              .count()
        )
        check_type = "check_out" if (today_count % 2 == 1) else "check_in"
        status     = _calc_status(now, check_type)

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
            "id":         log.id,
            "emp_code":   emp_code,
            "name":       emp.name,
            "department": emp.department,
            "position":   emp.position,
            "email":      emp.email or "",
            "check_type": check_type,
            "time":       now.strftime("%H:%M:%S"),
            "date":       now.strftime("%d/%m/%Y"),
            "timestamp":  now.isoformat(),
            "confidence": round(confidence, 4),
            "status":     status,
            "avatar_url": emp.avatar_url or "",
        }

    except Exception as e:
        db.rollback()
        print(f"  ✗ process_attendance lỗi: {e}")
        return None
    finally:
        db.close()


def _calc_status(now: datetime, check_type: str) -> str:
    if check_type != "check_in":
        return ""
    h, m    = map(int, settings.WORK_START.split(":"))
    work_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta   = (now - work_dt).total_seconds() / 60
    return f"Đi muộn {int(delta)} phút" if delta > settings.LATE_THRESHOLD else "Đúng giờ"


# ── Query helpers ────────────────────────────────────────────────
def get_logs_by_date(date_str: str, emp_code: str = None) -> list:
    db = SessionLocal()
    try:
        dt    = datetime.strptime(date_str, "%Y-%m-%d")
        start = dt.replace(hour=0, minute=0, second=0)
        end   = dt.replace(hour=23, minute=59, second=59)
        q     = db.query(AttendanceLog).filter(
            AttendanceLog.timestamp >= start,
            AttendanceLog.timestamp <= end,
        )
        if emp_code:
            q = q.filter_by(emp_code=emp_code)
        return [_log_to_dict(l) for l in q.order_by(AttendanceLog.timestamp.desc()).all()]
    finally:
        db.close()


def get_summary_today() -> dict:
    db = SessionLocal()
    try:
        now   = datetime.now()
        start = now.replace(hour=0, minute=0, second=0)
        logs  = db.query(AttendanceLog).filter(AttendanceLog.timestamp >= start).all()
        checked_in  = {l.emp_code for l in logs if l.check_type == "check_in"}
        checked_out = {l.emp_code for l in logs if l.check_type == "check_out"}
        total_emp = db.query(Employee).filter(
            or_(
                Employee.is_active == True,
                Employee.deactivated_at >= start,
            )
        ).count()

        return {
            "date":        now.strftime("%d/%m/%Y"),
            "total_emp":   total_emp,
            "checked_in":  len(checked_in),
            "checked_out": len(checked_out),
            "absent":      max(0, total_emp - len(checked_in)),
            "total_logs":  len(logs),
        }
    finally:
        db.close()


def _log_to_dict(log: AttendanceLog) -> dict:
    return {
        "id":          log.id,
        "emp_code":    log.emp_code,
        "name":        log.emp_name,
        "department":  log.department,
        "check_type":  log.check_type,
        "time":        log.timestamp.strftime("%H:%M:%S"),
        "date":        log.timestamp.strftime("%d/%m/%Y"),
        "timestamp":   log.timestamp.isoformat(),
        "confidence":  log.confidence,
        "status":      log.note,
    }

def get_log_by_id(log_id: int) -> dict | None:
    """Lấy thông tin 1 log theo ID."""
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        return _log_to_dict(log) if log else None
    finally:
        db.close()


def update_attendance_log(
    log_id: int,
    check_type: str | None = None,
    timestamp_str: str | None = None,
    note: str | None = None,
    updated_by: str = "",
) -> dict | None:
    """
    Chỉnh sửa 1 bản ghi điểm danh (dùng cho quản lý).
    - check_type: 'check_in' | 'check_out'
    - timestamp_str: ISO format hoặc 'YYYY-MM-DD HH:MM:SS'
    - note: ghi chú mới
    Trả về dict đã cập nhật, hoặc None nếu không tìm thấy log.
    """
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if not log:
            return None

        changes = []
        if check_type and check_type in ("check_in", "check_out"):
            changes.append(f"check_type: {log.check_type}→{check_type}")
            log.check_type = check_type

        if timestamp_str:
            try:
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
                    try:
                        new_ts = datetime.strptime(timestamp_str, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError(f"Không nhận dạng được định dạng thời gian: {timestamp_str}")
                changes.append(f"timestamp: {log.timestamp.strftime('%H:%M %d/%m/%Y')}→{new_ts.strftime('%H:%M %d/%m/%Y')}")
                log.timestamp = new_ts
                # Tính lại status nếu là check_in
                if log.check_type == "check_in":
                    log.note = _calc_status(new_ts, "check_in")
            except ValueError as e:
                raise ValueError(str(e))

        if note is not None:
            log.note = note

        # Thêm vết chỉnh sửa vào note
        edit_trail = f"[Sửa bởi {updated_by} lúc {datetime.now().strftime('%H:%M %d/%m/%Y')}]"
        if changes:
            log.note = (log.note or "") + f" {edit_trail}"

        db.commit()
        return _log_to_dict(log)

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def delete_attendance_log(log_id: int) -> bool:
    """Xoá 1 bản ghi điểm danh. Trả về True nếu thành công."""
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if not log:
            return False
        db.delete(log)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def create_manual_attendance_log(
    emp_code: str,
    check_type: str,
    timestamp_str: str,
    note: str = "",
    created_by: str = "",
) -> dict | None:
    """
    Tạo thủ công 1 bản ghi điểm danh (quản lý thêm bù).
    Trả về dict nếu thành công, None nếu không tìm thấy nhân viên.
    """
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()
        if not emp:
            return None

        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                ts = datetime.strptime(timestamp_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Không nhận dạng được định dạng thời gian: {timestamp_str}")

        auto_note = _calc_status(ts, check_type) if check_type == "check_in" else ""
        trail = f"[Tạo thủ công bởi {created_by} lúc {datetime.now().strftime('%H:%M %d/%m/%Y')}]"
        final_note = f"{note} {trail}".strip() if note else trail

        log = AttendanceLog(
            employee_id  = emp.id,
            emp_code     = emp_code,
            emp_name     = emp.name,
            department   = emp.department,
            check_type   = check_type,
            timestamp    = ts,
            confidence   = 0.0,
            capture_path = "",
            note         = f"{auto_note} {final_note}".strip(),
        )
        db.add(log)
        db.commit()
        return _log_to_dict(log)

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def update_capture_path(log_id: int, capture_path: str) -> None:
    """Cập nhật đường dẫn ảnh sau khi capture xong."""
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if log:
            log.capture_path = capture_path
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"  ✗ update_capture_path lỗi: {e}")
    finally:
        db.close()
def auto_checkout_missing(auto_time: datetime = None) -> int:
    """
    Quét tất cả nhân viên có log lẻ (check_in chưa có check_out) trong ngày.
    Tự tạo log check_out với note = "Tự động - không check out".
    Trả về số lượng log được tạo.
    """
    db = SessionLocal()
    try:
        now         = auto_time or datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Lấy tất cả emp_code có log hôm nay
        emp_codes = (
            db.query(AttendanceLog.emp_code)
              .filter(AttendanceLog.timestamp >= today_start)
              .distinct()
              .all()
        )

        count = 0
        for (emp_code,) in emp_codes:
            today_logs = (
                db.query(AttendanceLog)
                  .filter_by(emp_code=emp_code)
                  .filter(AttendanceLog.timestamp >= today_start)
                  .order_by(AttendanceLog.timestamp.asc())
                  .all()
            )
            # Số log lẻ → có check_in chưa có check_out
            if len(today_logs) % 2 == 1:
                emp = db.query(Employee).filter_by(emp_code=emp_code).first()
                log = AttendanceLog(
                    employee_id  = emp.id if emp else today_logs[-1].employee_id,
                    emp_code     = emp_code,
                    emp_name     = today_logs[-1].emp_name,
                    department   = today_logs[-1].department,
                    check_type   = "check_out",
                    timestamp    = now,
                    confidence   = 0.0,
                    capture_path = "",
                    note         = "Tự động - không check out",
                )
                db.add(log)
                count += 1

        db.commit()
        return count
    except Exception as e:
        db.rollback()
        print(f"  ✗ auto_checkout_missing lỗi: {e}")
        return 0
    finally:
        db.close()