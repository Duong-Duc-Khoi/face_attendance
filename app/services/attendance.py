"""
app/services/attendance.py
Business logic chấm công: xử lý sự kiện, tính trạng thái, query helpers.
"""
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.attendance import (
    AttendanceAuditFinding,
    AttendanceEvidence,
    AttendanceEvent,
    AttendanceLog,
    AttendanceSession,
)
from app.models.shift import Shift, ShiftAssignment
from app.services.leave_policy import LEAVE_ASSIGNMENT_STATUS
from app.services.shift_service import (
    calc_status_for_shift,
    find_open_session_for_time,
    find_shift_assignment_for_checkin,
    find_shift_assignment_for_time,
    find_leave_assignment_for_time,
    rebuild_sessions_for_log_change,
    shift_window,
)
from app.services.employee_branch_history import branch_for_log, filter_logs_by_branch_ids
from app.services.notify import notify_missing_checkout
from app.services.attendance_period import (
    create_correction_audit,
    ensure_period_unlocked,
    log_snapshot,
)
from app.schemas.employee import JOB_ROLE_LABELS, normalize_job_role

LOW_CONFIDENCE_THRESHOLD = 0.70


def _employee_job_role(emp: Employee | None) -> str:
    if not emp:
        return ""
    return normalize_job_role(emp.job_role or emp.position or "")


def _employee_role_label(emp: Employee | None, fallback: str = "") -> str:
    role = _employee_job_role(emp)
    return JOB_ROLE_LABELS.get(role, role or fallback or "Chưa xác định")


def _branch_active(db, branch_id: int | None) -> bool:
    if branch_id is None:
        return True
    branch = db.query(Branch).filter_by(id=branch_id).first()
    return bool(branch and branch.is_active)


def _checkin_grace_minutes(shift: Shift | None = None) -> int:
    if shift and shift.late_threshold_minutes is not None:
        return int(shift.late_threshold_minutes)
    return settings.CHECKIN_GRACE_MINUTES


def _late_minutes_after_grace(check_time: datetime, shift_start: datetime, shift: Shift | None = None) -> int:
    raw_late = max(0, int((check_time - shift_start).total_seconds() / 60))
    return max(0, raw_late - _checkin_grace_minutes(shift))


def _overtime_requires_review(check_time: datetime, shift_end: datetime) -> tuple[bool, int]:
    overtime = max(0, int((check_time - shift_end).total_seconds() / 60))
    return overtime > settings.OVERTIME_APPROVAL_THRESHOLD_MINUTES, overtime


def _next_unscheduled_check_type(emp_code: str, now: datetime, db) -> str:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    unscheduled_count = (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.emp_code == emp_code,
              AttendanceLog.timestamp >= day_start,
              AttendanceLog.note.like("Ngoài phân ca%"),
          )
          .count()
    )
    return "check_out" if unscheduled_count % 2 == 1 else "check_in"


def _open_unscheduled_session(emp: Employee, now: datetime, db) -> AttendanceSession | None:
    candidate_dates = [now.date(), (now - timedelta(days=1)).date()]
    return (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.employee_id == emp.id,
              AttendanceSession.work_date.in_(candidate_dates),
              AttendanceSession.review_type == "unscheduled",
              AttendanceSession.review_status == "pending_review",
              AttendanceSession.status != "cancelled",
              AttendanceSession.check_in_at.isnot(None),
              AttendanceSession.check_out_at.is_(None),
          )
          .order_by(AttendanceSession.check_in_at.desc(), AttendanceSession.id.desc())
          .first()
    )


def _last_unmatched_unscheduled_checkin(emp_code: str, now: datetime, db) -> AttendanceLog | None:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.emp_code == emp_code,
              AttendanceLog.check_type == "check_in",
              AttendanceLog.timestamp >= day_start,
              AttendanceLog.note.like("Ngoài phân ca%"),
          )
          .order_by(AttendanceLog.timestamp.desc(), AttendanceLog.id.desc())
          .limit(20)
          .all()
    )
    for log in rows:
        event = _matching_event(db, log)
        if not event or not event.session_id:
            return log
    return None


def process_attendance(
    emp_code: str,
    confidence: float,
    capture_path: str = "",
    branch_id: int | None = None,
) -> dict | None:
    """
    Xử lý 1 sự kiện chấm công từ kết quả nhận diện.
    Trả về dict nếu ghi log thành công hoặc cần báo lỗi cho kiosk, None nếu bỏ qua im lặng.
    """
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()
        if not emp:
            return {
                "ok": False,
                "reason": "employee_not_found",
                "emp_code": emp_code,
                "name": "",
                "department": "",
                "confidence": round(confidence, 4),
                "message": "Không tìm thấy nhân viên",
                "voice_message": "Có lỗi. Không tìm thấy nhân viên.",
            }
        if branch_id is not None and emp.branch_id != branch_id:
            return {
                "ok": False,
                "reason": "branch_mismatch",
                "emp_code": emp_code,
                "name": emp.name,
                "department": emp.department,
                "position": emp.position,
                "job_role": _employee_job_role(emp),
                "role_label": _employee_role_label(emp, emp.department),
                "branch_id": emp.branch_id,
                "confidence": round(confidence, 4),
                "message": "Nhân viên không thuộc cửa hàng của kiosk này",
                "voice_message": "Không thể chấm công. Bạn không thuộc cửa hàng này.",
                "avatar_url": emp.avatar_url or "",
            }
        effective_branch_id = branch_id if branch_id is not None else emp.branch_id
        if not _branch_active(db, effective_branch_id):
            return {
                "ok": False,
                "reason": "branch_inactive",
                "emp_code": emp_code,
                "name": emp.name,
                "department": emp.department,
                "position": emp.position,
                "job_role": _employee_job_role(emp),
                "role_label": _employee_role_label(emp, emp.department),
                "branch_id": effective_branch_id,
                "confidence": round(confidence, 4),
                "message": "Cửa hàng đã ngừng hoạt động, không thể chấm công",
                "voice_message": "Cửa hàng đã ngừng hoạt động, không thể chấm công.",
                "avatar_url": emp.avatar_url or "",
            }

        now = datetime.now()

        # Cooldown — chống spam
        last_log = (
            db.query(AttendanceLog)
              .filter_by(emp_code=emp_code)
              .order_by(AttendanceLog.timestamp.desc())
              .first()
        )
        if last_log and (now - last_log.timestamp) < timedelta(minutes=settings.COOLDOWN_MINUTES):
            last_type_label = "check in" if last_log.check_type == "check_in" else "check out"
            remaining_seconds = max(
                0,
                int((timedelta(minutes=settings.COOLDOWN_MINUTES) - (now - last_log.timestamp)).total_seconds()),
            )
            remaining_minutes = max(1, (remaining_seconds + 59) // 60)
            return {
                "ok": False,
                "reason": "cooldown",
                "emp_code": emp_code,
                "name": emp.name,
                "department": emp.department,
                "position": emp.position,
                "job_role": _employee_job_role(emp),
                "role_label": _employee_role_label(emp, emp.department),
                "branch_id": emp.branch_id,
                "email": emp.email or "",
                "check_type": last_log.check_type,
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%d/%m/%Y"),
                "timestamp": now.isoformat(),
                "confidence": round(confidence, 4),
                "cooldown_minutes": settings.COOLDOWN_MINUTES,
                "remaining_seconds": remaining_seconds,
                "message": f"Bạn đã {last_type_label} trong {settings.COOLDOWN_MINUTES} phút trước",
                "voice_message": f"Bạn đã {last_type_label} trong {settings.COOLDOWN_MINUTES} phút trước. Vui lòng thử lại sau khoảng {remaining_minutes} phút.",
                "avatar_url": emp.avatar_url or "",
            }

        open_session, assignment, shift = find_open_session_for_time(emp_code, now, db)
        if not open_session:
            assignment, shift = find_shift_assignment_for_checkin(emp_code, now, db)
        if not assignment or not shift:
            leave_assignment, leave_shift = find_leave_assignment_for_time(emp_code, now, db)
            if leave_assignment and leave_shift:
                return {
                    "ok": False,
                    "reason": "leave_approved_shift",
                    "emp_code": emp_code,
                    "name": emp.name,
                    "department": emp.department,
                    "position": emp.position,
                    "job_role": _employee_job_role(emp),
                    "role_label": _employee_role_label(emp, emp.department),
                    "branch_id": leave_assignment.branch_id or emp.branch_id,
                    "confidence": round(confidence, 4),
                    "message": f"Ca {leave_shift.name} đã được duyệt nghỉ phép",
                    "voice_message": "Ca này đã được duyệt nghỉ phép, không thể chấm công.",
                    "avatar_url": emp.avatar_url or "",
                }
            unscheduled_session = _open_unscheduled_session(emp, now, db)
            check_type = "check_out" if unscheduled_session else _next_unscheduled_check_type(emp_code, now, db)
            status = "Ngoài phân ca - chưa có ca phân công, chờ quản lý kiểm tra/gắn ca"
            if check_type == "check_in":
                unscheduled_session = AttendanceSession(
                    employee_id=emp.id,
                    branch_id=emp.branch_id,
                    shift_assignment_id=None,
                    shift_id=None,
                    work_date=now.date(),
                    check_in_at=now,
                    status="open",
                    check_in_status="unscheduled",
                    source="face",
                    note=status,
                    review_type="unscheduled",
                    review_status="pending_review",
                )
                db.add(unscheduled_session)
                db.flush()
            elif not unscheduled_session:
                previous_checkin = _last_unmatched_unscheduled_checkin(emp_code, now, db)
                if previous_checkin:
                    unscheduled_session = AttendanceSession(
                        employee_id=emp.id,
                        branch_id=emp.branch_id,
                        shift_assignment_id=None,
                        shift_id=None,
                        work_date=previous_checkin.timestamp.date(),
                        check_in_at=previous_checkin.timestamp,
                        status="open",
                        check_in_status="unscheduled",
                        source="face",
                        note=status,
                        review_type="unscheduled",
                        review_status="pending_review",
                    )
                    db.add(unscheduled_session)
                    db.flush()
                    previous_event = _matching_event(db, previous_checkin)
                    if previous_event:
                        previous_event.session_id = unscheduled_session.id
                        previous_event.branch_id = emp.branch_id
                else:
                    check_type = "check_in"
                    unscheduled_session = AttendanceSession(
                        employee_id=emp.id,
                        branch_id=emp.branch_id,
                        shift_assignment_id=None,
                        shift_id=None,
                        work_date=now.date(),
                        check_in_at=now,
                        status="open",
                        check_in_status="unscheduled",
                        source="face",
                        note=status,
                        review_type="unscheduled",
                        review_status="pending_review",
                    )
                    db.add(unscheduled_session)
                    db.flush()
            if check_type == "check_out" and unscheduled_session:
                unscheduled_session.check_out_at = now
                unscheduled_session.status = "completed"
                unscheduled_session.check_out_status = "unscheduled"
                unscheduled_session.review_status = "pending_review"
                unscheduled_session.review_type = "unscheduled"
                if unscheduled_session.check_in_at:
                    unscheduled_session.worked_minutes = max(
                        0,
                        int((now - unscheduled_session.check_in_at).total_seconds() / 60),
                    )
                unscheduled_session.note = status
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
                session_id   = unscheduled_session.id if unscheduled_session else None,
                employee_id  = emp.id,
                branch_id    = emp.branch_id,
                event_type   = check_type,
                event_time   = now,
                confidence   = round(confidence, 4),
                capture_path = capture_path,
                source       = "face",
                device_id    = "",
                note         = status,
            )
            db.add(event)
            db.commit()

            return {
                "ok": True,
                "warning": True,
                "reason": "no_active_shift_assignment_logged",
                "id": log.id,
                "session_id": unscheduled_session.id if unscheduled_session else None,
                "event_id": event.id,
                "shift_id": None,
                "shift_name": "",
                "emp_code": emp_code,
                "name": emp.name,
                "department": emp.department,
                "position": emp.position,
                "job_role": _employee_job_role(emp),
                "role_label": _employee_role_label(emp, emp.department),
                "branch_id": emp.branch_id,
                "email": emp.email or "",
                "check_type": check_type,
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%d/%m/%Y"),
                "timestamp": now.isoformat(),
                "confidence": round(confidence, 4),
                "status": status,
                "message": "Đã ghi nhận lượt chấm công ngoài phân ca",
                "voice_message": "Đã ghi nhận chấm công, nhưng bạn chưa có ca được phân công. Vui lòng báo quản lý kiểm tra.",
                "avatar_url": emp.avatar_url or "",
            }
        session = open_session
        if assignment and not session:
            session = (
                db.query(AttendanceSession)
                  .filter_by(shift_assignment_id=assignment.id)
                  .first()
            )

        if session and session.check_in_at and session.check_out_at:
            return {
                "ok": False,
                "reason": "shift_completed",
                "emp_code": emp_code,
                "name": emp.name,
                "department": emp.department,
                "position": emp.position,
                "job_role": _employee_job_role(emp),
                "role_label": _employee_role_label(emp, emp.department),
                "branch_id": emp.branch_id,
                "email": emp.email or "",
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%d/%m/%Y"),
                "timestamp": now.isoformat(),
                "confidence": round(confidence, 4),
                "message": "Ca làm đã hoàn tất check in và check out",
                "voice_message": "Ca làm đã hoàn tất check in và check out.",
                "avatar_url": emp.avatar_url or "",
            }
        check_type = "check_out" if open_session or (session and session.check_in_at) else "check_in"

        status = ""
        check_out_status = ""
        if check_type == "check_in":
            status = calc_status_for_shift(now, emp_code, db)
        elif shift and assignment:
            _start, shift_end, _from, _until = shift_window(assignment.work_date, shift)
            early_leave = max(0, int((shift_end - now).total_seconds() / 60))
            overtime_needs_review, overtime = _overtime_requires_review(now, shift_end)
            if early_leave > 0:
                check_out_status = "early_leave"
                status = f"Về sớm {early_leave} phút ({shift.name})"
            elif overtime_needs_review:
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
                session.status = "open"
                session.check_in_status = "late" if status.startswith("Đi muộn") else "on_time"
                if session.review_type in ("absent", "missing_checkout"):
                    session.review_type = ""
                    session.review_status = "none"
                if shift and assignment:
                    shift_start, _shift_end, _from, _until = shift_window(assignment.work_date, shift)
                    session.late_minutes = _late_minutes_after_grace(now, shift_start, shift)
            else:
                session.check_out_at = now
                session.status = "completed"
                session.check_out_status = check_out_status or "normal"
                if shift and assignment:
                    shift_start, shift_end, _from, _until = shift_window(assignment.work_date, shift)
                    session.early_leave_minutes = max(0, int((shift_end - now).total_seconds() / 60))
                    overtime_needs_review, overtime = _overtime_requires_review(now, shift_end)
                    session.overtime_minutes = overtime if overtime_needs_review else 0
                    if overtime_needs_review:
                        session.review_type = "overtime"
                        session.review_status = "pending_review"
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
            device_id    = "",
            note         = status,
        )
        db.add(event)
        db.commit()

        return {
            "ok":         True,
            "id":         log.id,
            "session_id": session.id if session else None,
            "event_id":   event.id,
            "shift_id":   shift.id if shift else None,
            "shift_name": shift.name if shift else "",
            "emp_code":   emp_code,
            "name":       emp.name,
            "department": emp.department,
            "position":   emp.position,
            "job_role":   _employee_job_role(emp),
            "role_label": _employee_role_label(emp, emp.department),
            "branch_id":  emp.branch_id,
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
        return {
            "ok": False,
            "reason": "error",
            "emp_code": emp_code,
            "name": "",
            "department": "",
            "confidence": round(confidence, 4),
            "message": "Có lỗi khi xử lý chấm công",
            "voice_message": "Có lỗi khi xử lý chấm công.",
        }
    finally:
        db.close()


def _calc_status_for_employee_shift(emp_code: str, now: datetime, check_type: str, db) -> str:
    if check_type != "check_in":
        return ""
    assignment, shift = find_shift_assignment_for_time(emp_code, now, db)
    if not assignment or not shift:
        return "Chưa có ca phân công"
    shift_start, _shift_end, _from, _until = shift_window(assignment.work_date, shift)
    late_minutes = int((now - shift_start).total_seconds() / 60)
    threshold = shift.late_threshold_minutes if shift.late_threshold_minutes is not None else settings.CHECKIN_GRACE_MINUTES
    if late_minutes > threshold:
        return f"Đi muộn {late_minutes - threshold} phút ({shift.name})"
    return f"Đúng giờ ({shift.name})"


def get_logs_by_date(date_str: str, emp_code: str = None, branch_ids: list[int] | None = None) -> list:
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
        logs = q.order_by(AttendanceLog.timestamp.desc()).all()
        logs = filter_logs_by_branch_ids(db, logs, branch_ids)
        return [_log_to_dict(log, _matching_event(db, log)) for log in logs]
    finally:
        db.close()


def get_summary_today(branch_ids: list[int] | None = None) -> dict:
    db = SessionLocal()
    try:
        now   = datetime.now()
        today = now.date()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        assignments = (
            db.query(ShiftAssignment)
              .filter(
                  ShiftAssignment.work_date == today,
                  ShiftAssignment.status.notin_(["cancelled", LEAVE_ASSIGNMENT_STATUS]),
              )
              .all()
        )
        if branch_ids is not None:
            assignments = [a for a in assignments if a.branch_id in branch_ids]
        assignment_ids = [a.id for a in assignments]
        sessions = []
        if assignment_ids:
            sessions = (
                db.query(AttendanceSession)
                  .filter(AttendanceSession.shift_assignment_id.in_(assignment_ids))
                  .all()
            )
        session_by_assignment = {s.shift_assignment_id: s for s in sessions}
        shift_ids = {a.shift_id for a in assignments}
        shifts_by_id = {
            s.id: s
            for s in db.query(Shift).filter(Shift.id.in_(shift_ids)).all()
        } if shift_ids else {}
        checked_in = {
            a.id for a in assignments
            if session_by_assignment.get(a.id) and session_by_assignment[a.id].check_in_at
        }
        checked_out = {
            a.id for a in assignments
            if session_by_assignment.get(a.id) and session_by_assignment[a.id].check_out_at
        }
        checkin_due = set()
        for assignment in assignments:
            shift = shifts_by_id.get(assignment.shift_id)
            if not shift:
                checkin_due.add(assignment.id)
                continue
            shift_start, _shift_end, _checkin_from, _checkout_until = shift_window(assignment.work_date, shift)
            if now >= shift_start:
                checkin_due.add(assignment.id)
        logs = db.query(AttendanceLog).filter(AttendanceLog.timestamp >= start).all()
        logs = filter_logs_by_branch_ids(db, logs, branch_ids)
        review_q = db.query(AttendanceSession).filter(AttendanceSession.review_status == "pending_review")
        if branch_ids is not None:
            review_q = review_q.filter(AttendanceSession.branch_id.in_(branch_ids))
        pending_reviews = review_q.count()
        pending_absent = review_q.filter(AttendanceSession.review_type == "absent").count()
        pending_missing_checkout = review_q.filter(AttendanceSession.review_type == "missing_checkout").count()
        pending_overtime = review_q.filter(AttendanceSession.review_type == "overtime").count()
        pending_unscheduled = review_q.filter(AttendanceSession.review_type == "unscheduled").count()
        total_assigned = len(assignments)
        unique_emp = len({a.emp_code for a in assignments})

        return {
            "date":        now.strftime("%d/%m/%Y"),
            "total_emp":   total_assigned,
            "assigned_shifts": total_assigned,
            "scheduled_employees": unique_emp,
            "checked_in":  len(checked_in),
            "checked_out": len(checked_out),
            "absent":      max(0, len(checkin_due - checked_in)),
            "not_started": max(0, total_assigned - len(checkin_due)),
            "total_logs":  len(logs),
            "pending_attendance_reviews": pending_reviews,
            "pending_absent_reviews": pending_absent,
            "pending_missing_checkout_reviews": pending_missing_checkout,
            "pending_overtime_reviews": pending_overtime,
            "pending_unscheduled_reviews": pending_unscheduled,
        }
    finally:
        db.close()


def _capture_file_exists(capture_path: str) -> bool:
    if not capture_path:
        return False
    try:
        capture_root = Path(settings.CAPTURES_DIR).resolve()
        resolved = Path(capture_path).resolve()
        resolved.relative_to(capture_root)
        return resolved.is_file()
    except OSError:
        return False
    except ValueError:
        return False


def _matching_event(db, log: AttendanceLog) -> AttendanceEvent | None:
    if not log or not log.employee_id:
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


def _matching_evidence(db, log: AttendanceLog) -> AttendanceEvidence | None:
    return (
        db.query(AttendanceEvidence)
          .filter_by(log_id=log.id)
          .order_by(AttendanceEvidence.id.desc())
          .first()
    )


def _top_finding(db, log: AttendanceLog) -> AttendanceAuditFinding | None:
    return (
        db.query(AttendanceAuditFinding)
          .filter_by(log_id=log.id, review_status="pending_review")
          .order_by(AttendanceAuditFinding.risk_score.desc(), AttendanceAuditFinding.id.desc())
          .first()
    )


def _manual_review_finding(db, log: AttendanceLog) -> AttendanceAuditFinding | None:
    return (
        db.query(AttendanceAuditFinding)
          .filter_by(log_id=log.id, source="manual_review")
          .order_by(AttendanceAuditFinding.updated_at.desc(), AttendanceAuditFinding.id.desc())
          .first()
    )


def _log_to_dict(log: AttendanceLog, event: AttendanceEvent | None = None) -> dict:
    confidence = float(log.confidence or 0.0)
    evidence = None
    finding = None
    manual_review = None
    emp = None
    branch_id = None
    branch_name = ""
    try:
        db = SessionLocal()
        try:
            evidence = _matching_evidence(db, log)
            finding = _top_finding(db, log)
            manual_review = _manual_review_finding(db, log)
            if log.employee_id:
                emp = db.query(Employee).filter_by(id=log.employee_id).first()
            if not emp and log.emp_code:
                emp = db.query(Employee).filter_by(emp_code=log.emp_code).first()
            branch_id = branch_for_log(db, log, event)
            if branch_id:
                branch = db.query(Branch).filter_by(id=branch_id).first()
                branch_name = branch.name if branch else ""
        finally:
            db.close()
    except Exception:
        evidence = None
        finding = None
    image_path = log.capture_path or ""
    if not _capture_file_exists(image_path) and evidence:
        image_path = evidence.image_path or ""
    image_hash = ""
    if event and event.image_hash:
        image_hash = event.image_hash
    elif evidence and evidence.image_hash:
        image_hash = evidence.image_hash
    capture_available = _capture_file_exists(image_path)
    duplicate_log_count = 0
    if image_hash:
        try:
            db = SessionLocal()
            try:
                seen_logs = set()
                rows = (
                    db.query(AttendanceEvidence)
                      .filter(
                          AttendanceEvidence.image_hash == image_hash,
                          AttendanceEvidence.log_id != log.id,
                      )
                      .all()
                )
                for row in rows:
                    if row.log_id in seen_logs:
                        continue
                    if _capture_file_exists(row.image_path):
                        seen_logs.add(row.log_id)
                duplicate_log_count = len(seen_logs)
            finally:
                db.close()
        except Exception:
            duplicate_log_count = 0
    evidence_status = "missing"
    evidence_status_text = "Không có ảnh bằng chứng"
    if capture_available:
        if duplicate_log_count > 0:
            evidence_status = "duplicate"
            evidence_status_text = f"Ảnh này trùng với {duplicate_log_count} lượt chấm công khác"
        else:
            evidence_status = "ok"
            evidence_status_text = "Bằng chứng hợp lệ, chưa ghi nhận ảnh trùng"
    return {
        "id":          log.id,
        "emp_code":    log.emp_code,
        "name":        log.emp_name,
        "department":  log.department,
        "branch_id":   branch_id,
        "branch_name": branch_name,
        "position":    emp.position if emp else "",
        "job_role":    _employee_job_role(emp),
        "role_label":  _employee_role_label(emp, log.department),
        "check_type":  log.check_type,
        "time":        log.timestamp.strftime("%H:%M:%S"),
        "date":        log.timestamp.strftime("%d/%m/%Y"),
        "timestamp":   log.timestamp.isoformat(),
        "confidence":  log.confidence,
        "capture_available": capture_available,
        "capture_url": f"/api/attendance/{log.id}/capture" if capture_available else "",
        "image_hash": image_hash,
        "evidence_status": evidence_status,
        "evidence_status_text": evidence_status_text,
        "duplicate_image_count": duplicate_log_count,
        "evidence_id": evidence.id if evidence else None,
        "audit_finding_id": finding.id if finding else None,
        "audit_risk_score": finding.risk_score if finding else 0.0,
        "audit_risk_level": finding.risk_level if finding else "",
        "audit_review_status": finding.review_status if finding else "",
        "manual_review_id": manual_review.id if manual_review else None,
        "manual_review_status": manual_review.review_status if manual_review else "",
        "manual_reviewed_by": manual_review.reviewed_by if manual_review else "",
        "manual_reviewed_at": manual_review.reviewed_at.isoformat() if manual_review and manual_review.reviewed_at else "",
        "is_low_confidence": 0 < confidence < LOW_CONFIDENCE_THRESHOLD,
        "status":      log.note,
    }

def get_log_by_id(log_id: int) -> dict | None:
    """Lấy thông tin 1 log theo ID."""
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        return _log_to_dict(log, _matching_event(db, log)) if log else None
    finally:
        db.close()


def _parse_log_timestamp(timestamp_str: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(timestamp_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Không nhận dạng được định dạng thời gian: {timestamp_str}")


def _branch_for_attendance_log(db, log: AttendanceLog, event: AttendanceEvent | None = None) -> int | None:
    branch_id = branch_for_log(db, log, event)
    if branch_id:
        return branch_id
    if log.employee_id:
        emp = db.query(Employee).filter_by(id=log.employee_id).first()
        if emp:
            return emp.branch_id
    if log.emp_code:
        emp = db.query(Employee).filter_by(emp_code=log.emp_code).first()
        if emp:
            return emp.branch_id
    return None


def update_attendance_log(
    log_id: int,
    check_type: str | None = None,
    timestamp_str: str | None = None,
    note: str | None = None,
    updated_by: str = "",
    updated_by_id: int | None = None,
    reason: str = "",
) -> dict | None:
    """
    Chỉnh sửa 1 bản ghi điểm danh (dùng cho quản lý).
    - check_type: 'check_in' | 'check_out'
    - timestamp_str: ISO format hoặc 'YYYY-MM-DD HH:MM:SS'
    - note: ghi chú mới
    Trả về dict đã cập nhật, hoặc None nếu không tìm thấy log.
    """
    if not (reason or "").strip():
        raise ValueError("Vui lòng nhập lý do chỉnh sửa công")
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if not log:
            return None

        old_timestamp = log.timestamp
        old_check_type = log.check_type
        matched_event = _matching_event(db, log)
        branch_id = _branch_for_attendance_log(db, log, matched_event)
        new_ts = _parse_log_timestamp(timestamp_str) if timestamp_str else old_timestamp
        ensure_period_unlocked(db, branch_id, old_timestamp)
        if new_ts and new_ts.date() != old_timestamp.date():
            ensure_period_unlocked(db, branch_id, new_ts)
        before_data = log_snapshot(log)
        changes = []
        if check_type and check_type in ("check_in", "check_out"):
            changes.append(f"check_type: {log.check_type}→{check_type}")
            log.check_type = check_type
        elif check_type:
            raise ValueError("check_type phải là 'check_in' hoặc 'check_out'")

        if timestamp_str:
            changes.append(f"timestamp: {log.timestamp.strftime('%H:%M %d/%m/%Y')}→{new_ts.strftime('%H:%M %d/%m/%Y')}")
            log.timestamp = new_ts
            if log.check_type == "check_in":
                log.note = _calc_status_for_employee_shift(log.emp_code, new_ts, "check_in", db)

        if note is not None:
            if note != log.note:
                changes.append("note")
            log.note = note

        # Thêm vết chỉnh sửa vào note
        edit_trail = f"[Sửa bởi {updated_by} lúc {datetime.now().strftime('%H:%M %d/%m/%Y')}]"
        if changes:
            log.note = (log.note or "") + f" {edit_trail}"

        if matched_event and changes:
            matched_event.event_type = log.check_type
            matched_event.event_time = log.timestamp
            matched_event.note = log.note or matched_event.note
        affected_session_ids = []
        if changes or old_check_type != log.check_type or old_timestamp != log.timestamp:
            affected_session_ids = rebuild_sessions_for_log_change(log.emp_code, old_timestamp, log.timestamp, db)

        audit = create_correction_audit(
            db,
            action="update",
            reason=reason,
            log=log,
            before_data=before_data,
            after_data=log_snapshot(log),
            affected_session_ids=affected_session_ids,
            created_by=updated_by,
            created_by_id=updated_by_id,
            branch_id=branch_id,
            employee_id=log.employee_id,
            emp_code=log.emp_code,
        )

        db.commit()
        result = _log_to_dict(log)
        result["audit_id"] = audit.id
        return result

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def delete_attendance_log(
    log_id: int,
    deleted_by: str = "",
    deleted_by_id: int | None = None,
    reason: str = "",
) -> dict | None:
    """Xoá 1 bản ghi điểm danh và ghi audit. Trả về dict nếu thành công."""
    if not (reason or "").strip():
        raise ValueError("Vui lòng nhập lý do chỉnh sửa công")
    db = SessionLocal()
    try:
        log = db.query(AttendanceLog).filter_by(id=log_id).first()
        if not log:
            return None
        old_emp_code = log.emp_code
        old_timestamp = log.timestamp
        matched_event = _matching_event(db, log)
        branch_id = _branch_for_attendance_log(db, log, matched_event)
        ensure_period_unlocked(db, branch_id, old_timestamp)
        before_data = log_snapshot(log)
        if matched_event:
            matched_event.session_id = None
        db.delete(log)
        db.flush()
        affected_session_ids = rebuild_sessions_for_log_change(old_emp_code, old_timestamp, None, db)
        audit = create_correction_audit(
            db,
            action="delete",
            reason=reason,
            log=None,
            before_data=before_data,
            after_data={},
            affected_session_ids=affected_session_ids,
            created_by=deleted_by,
            created_by_id=deleted_by_id,
            branch_id=branch_id,
            employee_id=before_data.get("employee_id"),
            emp_code=old_emp_code,
        )
        db.commit()
        return {"deleted": True, "audit_id": audit.id}
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
    created_by_id: int | None = None,
    reason: str = "",
) -> dict | None:
    """
    Tạo thủ công 1 bản ghi điểm danh (quản lý thêm bù).
    Trả về dict nếu thành công, None nếu không tìm thấy nhân viên.
    """
    if not (reason or "").strip():
        raise ValueError("Vui lòng nhập lý do chỉnh sửa công")
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()
        if not emp:
            return None
        if not _branch_active(db, emp.branch_id):
            raise ValueError("Cửa hàng đã ngừng hoạt động, không thể tạo chấm công")

        if check_type not in ("check_in", "check_out"):
            raise ValueError("check_type phải là 'check_in' hoặc 'check_out'")
        ts = _parse_log_timestamp(timestamp_str)
        ensure_period_unlocked(db, emp.branch_id, ts)

        auto_note = _calc_status_for_employee_shift(emp_code, ts, check_type, db) if check_type == "check_in" else ""
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
        db.flush()
        affected_session_ids = rebuild_sessions_for_log_change(emp_code, None, ts, db)
        audit = create_correction_audit(
            db,
            action="create",
            reason=reason,
            log=log,
            before_data={},
            after_data=log_snapshot(log),
            affected_session_ids=affected_session_ids,
            created_by=created_by,
            created_by_id=created_by_id,
            branch_id=emp.branch_id,
            employee_id=emp.id,
            emp_code=emp_code,
        )
        db.commit()
        result = _log_to_dict(log)
        result["audit_id"] = audit.id
        return result

    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def update_capture_path(
    log_id: int,
    capture_path: str,
    event_id: int | None = None,
    image_hash: str = "",
) -> None:
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
            if event:
                event.capture_path = capture_path
                if image_hash:
                    event.image_hash = image_hash
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"  ✗ update_capture_path lỗi: {e}")
    finally:
        db.close()


def _auto_checkout_note(shift_name: str = "") -> str:
    suffix = f" ({shift_name})" if shift_name else ""
    return f"Tự động chấm ra theo giờ kết thúc ca{suffix} - nhân viên quên check out"


def auto_checkout_missing(auto_time: datetime = None) -> int:
    """
    Auto checkout thông minh theo ca:
    - Với session có shift_id: chỉ đóng khi now >= shift_end + auto_checkout_minutes.
    - Giờ check_out được ghi theo shift_end, không theo giờ job chạy, để không tính
      overtime khi không có bằng chứng chấm ra.
    Trả về số lượng session/log được đóng tự động.
    """
    db = SessionLocal()
    try:
        now         = auto_time or datetime.now()
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
            if not shift:
                continue
            _start, shift_end, _from, auto_until = shift_window(session.work_date, shift)
            checkout_at = shift_end
            note = _auto_checkout_note(shift.name)

            if now < auto_until:
                continue

            emp = db.query(Employee).filter_by(id=session.employee_id).first()
            session.status = "missing_checkout"
            session.check_out_at = checkout_at
            session.check_out_status = "auto"
            session.review_type = "missing_checkout"
            session.review_status = "pending_review"
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
            if emp and not session.manager_alert_sent_at:
                try:
                    notify_missing_checkout({
                        "emp_name": emp.name,
                        "emp_code": emp.emp_code,
                        "shift_name": shift.name,
                        "work_date": session.work_date.strftime("%d/%m/%Y"),
                        "checkout_at": checkout_at.strftime("%H:%M %d/%m/%Y"),
                    })
                    session.manager_alert_sent_at = now
                except Exception as exc:
                    print(f"  ✗ notify_missing_checkout lỗi: {exc}")
            count += 1

        db.commit()
        return count
    except Exception as e:
        db.rollback()
        print(f"  ✗ auto_checkout_missing lỗi: {e}")
        return 0
    finally:
        db.close()


def mark_absent_sessions(auto_time: datetime = None) -> int:
    """Tạo session vắng chờ xác nhận cho ca đã kết thúc mà chưa có check-in."""
    db = SessionLocal()
    try:
        now = auto_time or datetime.now()
        count = 0
        assignments = (
            db.query(ShiftAssignment)
              .filter(
                  ShiftAssignment.status.notin_(["cancelled", LEAVE_ASSIGNMENT_STATUS]),
                  ShiftAssignment.work_date <= now.date(),
              )
              .all()
        )
        for assignment in assignments:
            shift = db.query(Shift).filter_by(id=assignment.shift_id, is_active=True).first()
            if not shift:
                continue
            _start, shift_end, _from, _until = shift_window(assignment.work_date, shift)
            if now < shift_end:
                continue
            check_in_log = (
                db.query(AttendanceLog)
                  .filter(
                      AttendanceLog.emp_code == assignment.emp_code,
                      AttendanceLog.check_type == "check_in",
                      AttendanceLog.timestamp >= _from,
                      AttendanceLog.timestamp <= _until,
                  )
                  .first()
            )
            if check_in_log:
                continue
            existing = (
                db.query(AttendanceSession)
                  .filter_by(shift_assignment_id=assignment.id)
                  .first()
            )
            if existing:
                continue
            emp = db.query(Employee).filter_by(id=assignment.employee_id).first()
            if not emp:
                emp = db.query(Employee).filter_by(emp_code=assignment.emp_code).first()
            if not emp:
                continue
            effective_branch_id = assignment.branch_id or shift.branch_id or emp.branch_id
            if not _branch_active(db, effective_branch_id):
                continue
            db.add(AttendanceSession(
                employee_id=emp.id,
                branch_id=effective_branch_id,
                shift_assignment_id=assignment.id,
                shift_id=shift.id,
                work_date=assignment.work_date,
                status="absent",
                check_in_status="",
                check_out_status="",
                source="auto",
                note=f"Vắng ca {shift.name} - chờ quản lý xác nhận",
                review_type="absent",
                review_status="pending_review",
                break_minutes=shift.break_minutes or 0,
            ))
            count += 1
        db.commit()
        return count
    except Exception as e:
        db.rollback()
        print(f"  ✗ mark_absent_sessions lỗi: {e}")
        return 0
    finally:
        db.close()
