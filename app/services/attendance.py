"""
app/services/attendance.py
Business logic chấm công: xử lý sự kiện, tính trạng thái, query helpers.
"""
from sqlalchemy import or_
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.employee import Employee
from app.models.attendance import AttendanceEvent, AttendanceLog, AttendanceSession
from app.models.shift import Shift
from app.services.shift_service import (
    calc_status_for_shift,
    find_shift_assignment_for_time,
    shift_window,
)

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

        assignment, shift = find_shift_assignment_for_time(emp_code, now, db)
        session = None
        if assignment:
            session = (
                db.query(AttendanceSession)
                  .filter_by(shift_assignment_id=assignment.id)
                  .first()
            )

        # Nhà hàng: ưu tiên check-in/check-out theo session của ca được phân công.
        # Fallback về logic cũ nếu chưa có phân ca để hệ thống vẫn dùng được.
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if assignment:
            if session and session.check_in_at and session.check_out_at:
                return None
            check_type = "check_out" if session and session.check_in_at else "check_in"
        else:
            today_count = (
                db.query(AttendanceLog)
                  .filter_by(emp_code=emp_code)
                  .filter(AttendanceLog.timestamp >= today_start)
                  .count()
            )
            check_type = "check_out" if (today_count % 2 == 1) else "check_in"

        status = ""
        check_out_status = ""
        if check_type == "check_in":
            status = calc_status_for_shift(now, emp_code, db)
        elif shift and assignment:
            _start, shift_end, _from, _until = shift_window(assignment.work_date, shift)
            early_leave = max(0, int((shift_end - now).total_seconds() / 60))
            overtime = max(0, int((now - shift_end).total_seconds() / 60))
            if early_leave > 0:
                check_out_status = "early_leave"
                status = f"Về sớm {early_leave} phút ({shift.name})"
            elif overtime > 0:
                check_out_status = "overtime"
                status = f"Tăng ca {overtime} phút ({shift.name})"
            else:
                check_out_status = "normal"

        if assignment and not session:
            session = AttendanceSession(
                employee_id         = emp.id,
                branch_id           = assignment.branch_id or emp.branch_id,
                shift_assignment_id = assignment.id,
                shift_id            = assignment.shift_id,
                work_date           = assignment.work_date,
                status              = "open",
                source              = "face",
                break_minutes       = shift.break_minutes if shift else 0,
            )
            db.add(session)
            db.flush()

        if session:
            if check_type == "check_in":
                session.check_in_at = now
                session.check_in_status = "late" if status.startswith("Đi muộn") else "on_time"
                if shift and assignment:
                    shift_start, _shift_end, _from, _until = shift_window(assignment.work_date, shift)
                    session.late_minutes = max(0, int((now - shift_start).total_seconds() / 60) - (shift.late_threshold_minutes or 0))
            else:
                session.check_out_at = now
                session.status = "completed"
                session.check_out_status = check_out_status or "normal"
                if shift and assignment:
                    shift_start, shift_end, _from, _until = shift_window(assignment.work_date, shift)
                    session.early_leave_minutes = max(0, int((shift_end - now).total_seconds() / 60))
                    session.overtime_minutes = max(0, int((now - shift_end).total_seconds() / 60))
                if session.check_in_at:
                    gross_minutes = int((now - session.check_in_at).total_seconds() / 60)
                    session.worked_minutes = max(0, gross_minutes - (session.break_minutes or 0))
            session.note = status or session.note

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
        db.flush()

        event = AttendanceEvent(
            session_id   = session.id if session else None,
            employee_id  = emp.id,
            branch_id    = (session.branch_id if session else emp.branch_id),
            event_type   = check_type,
            event_time   = now,
            confidence   = round(confidence, 4),
            capture_path = capture_path,
            source       = "face",
            note         = status,
        )
        db.add(event)
        db.commit()

        return {
            "id":         log.id,
            "session_id": session.id if session else None,
            "event_id":   event.id,
            "shift_id":   shift.id if shift else None,
            "shift_name": shift.name if shift else "",
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
        "capture_path": log.capture_path,
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


def update_capture_path(log_id: int, capture_path: str, event_id: int | None = None) -> None:
    """Cập nhật đường dẫn ảnh sau khi capture xong."""
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if log:
            log.capture_path = capture_path
            if event_id:
                event = db.query(AttendanceEvent).filter_by(id=event_id).first()
            else:
                event = (
                    db.query(AttendanceEvent)
                      .filter_by(employee_id=log.employee_id, event_type=log.check_type)
                      .order_by(AttendanceEvent.event_time.desc())
                      .first()
                )
            if event and not event.capture_path:
                event.capture_path = capture_path
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"  ✗ update_capture_path lỗi: {e}")
    finally:
        db.close()


def _fallback_shift_end(work_date, now: datetime) -> tuple[datetime, datetime]:
    h, m = map(int, settings.WORK_END.split(":"))
    shift_end = datetime.combine(work_date, datetime.min.time()).replace(hour=h, minute=m)
    auto_until = shift_end + timedelta(minutes=180)
    return shift_end, auto_until


def _auto_checkout_note(shift_name: str = "") -> str:
    suffix = f" ({shift_name})" if shift_name else ""
    return f"Tự động chấm ra theo giờ kết thúc ca{suffix} - nhân viên quên check out"


def auto_checkout_missing(auto_time: datetime = None) -> int:
    """
    Auto checkout thông minh theo ca:
    - Với session có shift_id: chỉ đóng khi now >= shift_end + auto_checkout_minutes.
    - Giờ check_out được ghi theo shift_end, không theo giờ job chạy, để không tính
      overtime khi không có bằng chứng chấm ra.
    - Fallback cho log cũ không có session dùng WORK_END + 180 phút.
    Trả về số lượng session/log được đóng tự động.
    """
    db = SessionLocal()
    try:
        now         = auto_time or datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        closed_session_codes: set[str] = set()
        count = 0

        open_sessions = (
            db.query(AttendanceSession)
              .filter(
                  AttendanceSession.status == "open",
                  AttendanceSession.check_in_at.isnot(None),
                  AttendanceSession.check_out_at.is_(None),
                  AttendanceSession.work_date <= now.date(),
              )
              .all()
        )
        for session in open_sessions:
            shift = db.query(Shift).filter_by(id=session.shift_id).first() if session.shift_id else None
            if shift:
                _start, shift_end, _from, auto_until = shift_window(session.work_date, shift)
                checkout_at = shift_end
                note = _auto_checkout_note(shift.name)
            else:
                checkout_at, auto_until = _fallback_shift_end(session.work_date, now)
                note = _auto_checkout_note()

            if now < auto_until:
                continue

            emp = db.query(Employee).filter_by(id=session.employee_id).first()
            session.status = "missing_checkout"
            session.check_out_at = checkout_at
            session.check_out_status = "auto"
            session.early_leave_minutes = 0
            session.overtime_minutes = 0
            if session.check_in_at:
                gross_minutes = int((checkout_at - session.check_in_at).total_seconds() / 60)
                session.worked_minutes = max(0, gross_minutes - (session.break_minutes or 0))
            session.note = note

            if emp:
                db.add(AttendanceLog(
                    employee_id=emp.id,
                    emp_code=emp.emp_code,
                    emp_name=emp.name,
                    department=emp.department,
                    check_type="check_out",
                    timestamp=checkout_at,
                    confidence=0.0,
                    capture_path="",
                    note=note,
                ))
                closed_session_codes.add(emp.emp_code)

            db.add(AttendanceEvent(
                session_id=session.id,
                employee_id=session.employee_id,
                branch_id=session.branch_id,
                event_type="auto_checkout",
                event_time=now,
                confidence=0.0,
                source="auto",
                note=note,
            ))
            count += 1

        # Fallback cho dữ liệu/log cũ chưa tạo AttendanceSession.
        emp_codes = (
            db.query(AttendanceLog.emp_code)
              .filter(AttendanceLog.timestamp >= today_start)
              .distinct()
              .all()
        )

        for (emp_code,) in emp_codes:
            if emp_code in closed_session_codes:
                continue
            today_logs = (
                db.query(AttendanceLog)
                  .filter_by(emp_code=emp_code)
                  .filter(AttendanceLog.timestamp >= today_start)
                  .order_by(AttendanceLog.timestamp.asc())
                  .all()
            )
            # Số log lẻ → có check_in chưa có check_out
            if len(today_logs) % 2 == 1:
                checkout_at, auto_until = _fallback_shift_end(now.date(), now)
                if now < auto_until:
                    continue
                emp = db.query(Employee).filter_by(emp_code=emp_code).first()
                note = _auto_checkout_note()
                log = AttendanceLog(
                    employee_id  = emp.id if emp else today_logs[-1].employee_id,
                    emp_code     = emp_code,
                    emp_name     = today_logs[-1].emp_name,
                    department   = today_logs[-1].department,
                    check_type   = "check_out",
                    timestamp    = checkout_at,
                    confidence   = 0.0,
                    capture_path = "",
                    note         = note,
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
