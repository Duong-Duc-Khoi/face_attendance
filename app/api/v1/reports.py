"""
app/api/v1/reports.py
Endpoints báo cáo, thống kê và xuất Excel.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import Optional

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.services.branch_scope import ensure_branch_access, selected_branch_ids
from app.models.attendance import (
    AttendanceAuditFinding,
    AttendanceEvent,
    AttendanceEvidence,
    AttendanceLog,
    AttendancePeriodLock,
    AttendanceSession,
)
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.leave import LeaveRequest
from app.models.shift import Shift, ShiftAssignment
from app.schemas.employee import JOB_ROLE_LABELS, normalize_job_role
from app.services.leave_policy import LEAVE_ASSIGNMENT_STATUS, UNSCHEDULED_APPROVED_ASSIGNMENT_STATUS, ensure_can_assign_employee
from app.services.attendance_period import (
    create_period_lock,
    ensure_period_unlocked,
    list_period_locks,
    period_lock_to_dict,
    unlock_period,
)
from app.services.attendance_policy import (
    confirmed_absent,
    leave_credit_for_assignment,
    missing_checkin_recorded,
    missing_checkout_recorded,
    payable_work_minutes,
    recorded_overtime_minutes,
    review_status_label,
    session_counts_as_work,
    session_needs_review,
    session_status_label,
)
from app.services.attendance import (
    get_logs_by_date, get_summary_today,
    get_log_by_id, update_attendance_log,
    delete_attendance_log, create_manual_attendance_log,
    create_absent_session_manual_logs,
)
from app.services.shift_service import _employee_role_matches_shift, _ensure_assignable_workday, shift_window
from app.services.employee_branch_history import branch_for_log, filter_logs_by_branch_ids
from app.services.attendance_audit import (
    get_audit_run,
    list_audit_findings,
    run_attendance_audit,
    update_audit_finding_review,
)
from app.services.integration_settings import list_ai_provider_settings


router = APIRouter(prefix="/api", tags=["reports"])

_bearer_opt = HTTPBearer(auto_error=False)


def _optional_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer_opt),
    db: Session = Depends(get_db),
):
    """Dependency tuỳ chọn: trả về user nếu có token, None nếu không có."""
    if creds is None:
        return None
    try:
        from app.core.security import decode_access_token
        from app.models.user import User
        payload = decode_access_token(creds.credentials)
        return db.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
    except Exception:
        return None


def _require_manager_or_admin(current_user):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Chỉ quản lý (manager/admin) mới được xem bằng chứng chấm công")


def _employee_branch_for_log(db: Session, log: AttendanceLog) -> int | None:
    return branch_for_log(db, log)


def _employee_for_user(db: Session, current_user) -> Employee | None:
    emp = db.query(Employee).filter_by(user_id=current_user.id, is_active=True).first()
    if emp:
        return emp
    return db.query(Employee).filter_by(email=current_user.email, is_active=True).first()


def _branch_ids_for_user(db: Session, current_user, branch_id: int | None = None) -> list[int] | None:
    if current_user.role in ("admin", "manager"):
        return selected_branch_ids(db, current_user, branch_id)
    emp = _employee_for_user(db, current_user)
    if not emp or not emp.branch_id:
        raise HTTPException(status_code=403, detail="Tài khoản chưa được gắn với nhân viên/cửa hàng")
    if branch_id is not None and branch_id != emp.branch_id:
        raise HTTPException(status_code=403, detail="Bạn chỉ được xem dữ liệu của mình")
    return [emp.branch_id]


def _manager_branch_ids(db: Session, current_user, branch_id: int | None = None) -> list[int] | None:
    _require_manager_or_admin(current_user)
    return selected_branch_ids(db, current_user, branch_id)


def _ensure_log_scope(db: Session, current_user, log: AttendanceLog) -> None:
    ensure_branch_access(db, current_user, _employee_branch_for_log(db, log))


def _ensure_session_scope(db: Session, current_user, session: AttendanceSession) -> None:
    ensure_branch_access(db, current_user, session.branch_id)


def _filter_logs_for_branch_ids(db: Session, logs: list[AttendanceLog], branch_ids: list[int] | None) -> list[AttendanceLog]:
    return filter_logs_by_branch_ids(db, logs, branch_ids)


def _filter_log_dicts_for_branch_ids(logs: list[dict], branch_ids: list[int] | None) -> list[dict]:
    if branch_ids is None:
        return logs
    return [row for row in logs if row.get("branch_id") in branch_ids]


def _safe_capture_file(raw_path: str, missing_detail: str) -> Path:
    if not raw_path:
        raise HTTPException(status_code=404, detail=missing_detail)
    capture_root = Path(settings.CAPTURES_DIR).resolve()
    capture_path = Path(raw_path).resolve()
    try:
        capture_path.relative_to(capture_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Đường dẫn ảnh bằng chứng không hợp lệ")

    if not capture_path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file ảnh bằng chứng")
    return capture_path


def _employee_report_context(logs: list[AttendanceLog], db: Session) -> tuple[dict[int, Employee], dict[str, Employee], dict[int, str]]:
    employee_ids = {log.employee_id for log in logs if log.employee_id}
    emp_codes = {log.emp_code for log in logs if log.emp_code}
    employees = []
    if employee_ids or emp_codes:
        q = db.query(Employee)
        if employee_ids and emp_codes:
            from sqlalchemy import or_
            q = q.filter(or_(Employee.id.in_(list(employee_ids)), Employee.emp_code.in_(list(emp_codes))))
        elif employee_ids:
            q = q.filter(Employee.id.in_(list(employee_ids)))
        else:
            q = q.filter(Employee.emp_code.in_(list(emp_codes)))
        employees = q.all()
    by_id = {e.id: e for e in employees}
    by_code = {e.emp_code: e for e in employees}
    branch_ids = {branch_for_log(db, log) for log in logs}
    branch_ids.update({e.branch_id for e in employees if e.branch_id})
    branch_ids.discard(None)
    branches = {
        b.id: b.name
        for b in db.query(Branch).filter(Branch.id.in_(list(branch_ids))).all()
    } if branch_ids else {}
    return by_id, by_code, branches


def _employee_for_log(log: AttendanceLog, by_id: dict[int, Employee], by_code: dict[str, Employee]) -> Employee | None:
    return by_id.get(log.employee_id) or by_code.get(log.emp_code)


def _role_label_for_log(log: AttendanceLog, emp: Employee | None) -> str:
    role = normalize_job_role((emp.job_role if emp else "") or (emp.position if emp else ""))
    return JOB_ROLE_LABELS.get(role, role or log.department or "Chưa xác định")


def _branch_label_for_log(log: AttendanceLog, branches: dict[int, str], db: Session) -> str:
    branch_id = branch_for_log(db, log)
    if branch_id:
        return branches.get(branch_id, f"Chi nhánh #{branch_id}")
    return "Chưa xác định"


def _parse_export_range(from_date: str, to_date: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
        end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, microsecond=999999)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ngày không hợp lệ")
    if end < start:
        raise HTTPException(status_code=400, detail="Ngày kết thúc phải sau ngày bắt đầu")
    return start, end


def _hours(minutes: int | None) -> float:
    return round(max(0, int(minutes or 0)) / 60, 2)


def _dt_text(value: datetime | None) -> str:
    return value.strftime("%H:%M %d/%m/%Y") if value else ""


def _employment_label(value: str | None) -> str:
    return {
        "full_time": "Full-time",
        "part_time": "Part-time",
        "casual": "Thời vụ",
    }.get(value or "full_time", "Full-time")


def _review_label(value: str | None) -> str:
    return review_status_label(value)


def _session_status_label(session: AttendanceSession | None) -> str:
    return session_status_label(session)


def _session_counts_as_work(session: AttendanceSession | None) -> bool:
    return session_counts_as_work(session)


def _session_needs_review(session: AttendanceSession | None) -> bool:
    return session_needs_review(session)


def _session_ot_buckets(session: AttendanceSession | None) -> tuple[int, int]:
    return recorded_overtime_minutes(session), 0


def _write_table_sheet(ws, title: str, headers: list[str], rows: list[list], widths: list[int], styles) -> None:
    Alignment, Border, Font, PatternFill, _Side, get_column_letter = styles
    ws.title = title
    thin = _Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1A365D")
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    for r_idx, row in enumerate(rows, 2):
        for c_idx, val in enumerate(row, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width


def _employee_context_for_assignments(assignments: list[ShiftAssignment], db: Session) -> tuple[dict[int, Employee], dict[str, Employee]]:
    employee_ids = {a.employee_id for a in assignments if a.employee_id}
    emp_codes = {a.emp_code for a in assignments if a.emp_code}
    employees = []
    if employee_ids or emp_codes:
        q = db.query(Employee)
        if employee_ids and emp_codes:
            from sqlalchemy import or_
            q = q.filter(or_(Employee.id.in_(list(employee_ids)), Employee.emp_code.in_(list(emp_codes))))
        elif employee_ids:
            q = q.filter(Employee.id.in_(list(employee_ids)))
        else:
            q = q.filter(Employee.emp_code.in_(list(emp_codes)))
        employees = q.all()
    return {e.id: e for e in employees}, {e.emp_code: e for e in employees}


def _branch_labels(branch_ids: set[int | None], db: Session) -> dict[int, str]:
    ids = {bid for bid in branch_ids if bid}
    if not ids:
        return {}
    return {b.id: b.name for b in db.query(Branch).filter(Branch.id.in_(list(ids))).all()}


def _log_linked_to_shift_session(db: Session, log: AttendanceLog) -> bool:
    from sqlalchemy import or_

    event = (
        db.query(AttendanceEvent)
          .filter(
              AttendanceEvent.employee_id == log.employee_id,
              AttendanceEvent.event_type == log.check_type,
              AttendanceEvent.event_time == log.timestamp,
          )
          .order_by(AttendanceEvent.id.desc())
          .first()
    )
    if event and event.session_id:
        session = db.query(AttendanceSession).filter_by(id=event.session_id).first()
        if session and session.shift_assignment_id:
            return True

    session = (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.employee_id == log.employee_id,
              or_(
                  AttendanceSession.check_in_at == log.timestamp,
                  AttendanceSession.check_out_at == log.timestamp,
              ),
          )
          .order_by(AttendanceSession.id.desc())
          .first()
    )
    return bool(session and session.shift_assignment_id)


def _capture_file_for_log(log: AttendanceLog) -> Path:
    return _safe_capture_file(log.capture_path, "Bản ghi này chưa có ảnh bằng chứng")


def _evidence_image_file_for_log(log_id: int, db: Session) -> Path:
    evidence = (
        db.query(AttendanceEvidence)
          .filter_by(log_id=log_id, files_available=True)
          .order_by(AttendanceEvidence.id.desc())
          .first()
    )
    if not evidence:
        raise HTTPException(status_code=404, detail="Bản ghi này chưa có bằng chứng")
    if not evidence.image_path:
        raise HTTPException(status_code=404, detail="Không có file bằng chứng phù hợp")
    capture_root = Path(settings.CAPTURES_DIR).resolve()
    file_path = Path(evidence.image_path).resolve()
    try:
        file_path.relative_to(capture_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Đường dẫn bằng chứng không hợp lệ")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file bằng chứng")
    return file_path


@router.get("/attendance")
def get_attendance(date: str = None, emp_code: str = None, days: int = 1,
                   branch_id: int | None = None,
                   db: Session = Depends(get_db),
                   current_user=Depends(_optional_user)):
    if current_user:
        branch_ids = _branch_ids_for_user(db, current_user, branch_id)
        if current_user.role == "staff":
            own = _employee_for_user(db, current_user)
            if not own:
                raise HTTPException(status_code=403, detail="Tài khoản chưa được gắn với nhân viên")
            if emp_code and emp_code != own.emp_code:
                raise HTTPException(status_code=403, detail="Bạn chỉ được xem chấm công của mình")
            emp_code = own.emp_code
            branch_ids = None
    else:
        if branch_id is None:
            raise HTTPException(status_code=400, detail="Kiosk/public cần truyền branch_id")
        branch_ids = [branch_id]
    if date:
        logs = []
        start_date = datetime.strptime(date, "%Y-%m-%d")
        for i in range(max(1, days)):
            d = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            logs.extend(get_logs_by_date(d, emp_code, branch_ids))
    else:
        logs = []
        for i in range(days):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            logs.extend(get_logs_by_date(d, emp_code, branch_ids))
    logs.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    logs = _filter_log_dicts_for_branch_ids(logs, branch_ids)
    return {"logs": logs, "total": len(logs)}


@router.get("/summary")
def summary_today(branch_id: int | None = None, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    return get_summary_today(branch_ids)


@router.get("/summary/range")
def summary_range(from_date: str, to_date: str, branch_id: int | None = None, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0,  minute=0)
    end   = datetime.strptime(to_date,   "%Y-%m-%d").replace(hour=23, minute=59)
    logs  = db.query(AttendanceLog).filter(
        AttendanceLog.timestamp >= start,
        AttendanceLog.timestamp <= end,
    ).all()
    logs = _filter_logs_for_branch_ids(db, logs, branch_ids)
    emp_by_id, emp_by_code, branches = _employee_report_context(logs, db)

    assignments = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.work_date >= start.date(),
              ShiftAssignment.work_date <= end.date(),
              ShiftAssignment.status != "cancelled",
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
    by_date: dict[str, dict] = {}
    for assignment in assignments:
        day = assignment.work_date.isoformat()
        if day not in by_date:
            by_date[day] = {
                "assigned": 0,
                "scheduled_emp": set(),
                "checked_in": 0,
                "checked_out": 0,
            }
        row = by_date[day]
        row["assigned"] += 1
        row["scheduled_emp"].add(assignment.emp_code)
        session = session_by_assignment.get(assignment.id)
        if session and session.check_in_at:
            row["checked_in"] += 1
        if session and session.check_out_at:
            row["checked_out"] += 1

    role_stats: dict[str, int] = {}
    branch_stats: dict[str, int] = {}
    for log in logs:
        emp = _employee_for_log(log, emp_by_id, emp_by_code)
        role = _role_label_for_log(log, emp)
        branch = _branch_label_for_log(log, branches, db)
        role_stats[role] = role_stats.get(role, 0) + 1
        branch_stats[branch] = branch_stats.get(branch, 0) + 1

    return {
        "from_date":  from_date,
        "to_date":    to_date,
        "total_logs": len(logs),
        "by_date": [
            {
                "date": d,
                "assigned": v["assigned"],
                "scheduled_employees": len(v["scheduled_emp"]),
                "checked_in": v["checked_in"],
                "checked_out": v["checked_out"],
                "absent": max(0, v["assigned"] - v["checked_in"]),
            }
            for d, v in sorted(by_date.items())
        ],
        "by_role": [{"role": k, "count": v} for k, v in role_stats.items()],
        "by_branch": [{"branch": k, "count": v} for k, v in branch_stats.items()],
        "by_dept": [{"dept": k, "count": v} for k, v in role_stats.items()],
    }


def _build_attendance_history_workbook(logs: list[AttendanceLog], from_date: str, to_date: str, db: Session):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None

    emp_by_id, emp_by_code, branches = _employee_report_context(logs, db)
    wb = Workbook()
    ws = wb.active
    ws.title = "Lich su cham cong"

    ws.merge_cells("A1:H1")
    ws["A1"]           = f"LỊCH SỬ CHẤM CÔNG  —  {from_date} đến {to_date}"
    ws["A1"].font      = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")

    thin   = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1A365D")
    headers  = ["STT", "Mã NV", "Họ tên", "Chi nhánh", "Vai trò", "Loại", "Thời gian", "Trạng thái"]

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = hdr_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border    = border

    for i, log in enumerate(logs, 1):
        emp = _employee_for_log(log, emp_by_id, emp_by_code)
        branch_label = _branch_label_for_log(log, branches, db)
        role_label = _role_label_for_log(log, emp)
        fill_color = "F0FFF4" if log.check_type == "check_in" else "EBF4FF"
        row_fill   = PatternFill("solid", fgColor=fill_color)
        for col, val in enumerate([
            i, log.emp_code, log.emp_name, branch_label, role_label,
            "Vào" if log.check_type == "check_in" else "Ra",
            log.timestamp.strftime("%H:%M:%S  %d/%m/%Y"),
            log.note or "",
        ], 1):
            cell = ws.cell(row=i + 2, column=col, value=val)
            cell.alignment = Alignment(horizontal="center")
            cell.border    = border
            cell.fill      = row_fill

    for col, w in enumerate([6, 10, 22, 18, 18, 8, 24, 20], 1):
        ws.column_dimensions[get_column_letter(col)].width = w
    return wb


@router.get("/attendance/export")
def export_attendance_history(
    from_date: str,
    to_date: str,
    emp_code: str | None = None,
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _branch_ids_for_user(db, current_user, branch_id)
    if current_user.role == "staff":
        own = _employee_for_user(db, current_user)
        if not own:
            raise HTTPException(status_code=403, detail="Tài khoản chưa được gắn với nhân viên")
        if emp_code and emp_code != own.emp_code:
            raise HTTPException(status_code=403, detail="Bạn chỉ được xuất chấm công của mình")
        emp_code = own.emp_code
        branch_ids = None

    start, end = _parse_export_range(from_date, to_date)
    q = db.query(AttendanceLog).filter(
        AttendanceLog.timestamp >= start,
        AttendanceLog.timestamp <= end,
    )
    if emp_code:
        q = q.filter(AttendanceLog.emp_code == emp_code)
    logs = q.order_by(AttendanceLog.timestamp).all()
    logs = _filter_logs_for_branch_ids(db, logs, branch_ids)

    wb = _build_attendance_history_workbook(logs, from_date, to_date, db)
    if wb is None:
        return JSONResponse({"error": "Cài openpyxl: pip install openpyxl"}, status_code=500)

    settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{emp_code}" if emp_code else ""
    filepath = str(settings.EXPORTS_DIR / f"lichsu_chamcong_{from_date}_{to_date}{suffix}.xlsx")
    wb.save(filepath)

    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"LichSuChamCong_{from_date}_{to_date}{suffix}.xlsx",
    )


@router.get("/reports/export")
def export_excel(from_date: str, to_date: str, branch_id: int | None = None, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return JSONResponse({"error": "Cài openpyxl: pip install openpyxl"}, status_code=500)

    start, end = _parse_export_range(from_date, to_date)
    assignments = (
        db.query(ShiftAssignment)
          .filter(
              ShiftAssignment.work_date >= start.date(),
              ShiftAssignment.work_date <= end.date(),
              ShiftAssignment.status != "cancelled",
          )
          .order_by(ShiftAssignment.work_date, ShiftAssignment.emp_code, ShiftAssignment.shift_id)
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

    shift_ids = {a.shift_id for a in assignments if a.shift_id}
    shifts = {
        s.id: s
        for s in db.query(Shift).filter(Shift.id.in_(list(shift_ids))).all()
    } if shift_ids else {}
    emp_by_id, emp_by_code = _employee_context_for_assignments(assignments, db)
    branch_ids_for_labels = {a.branch_id for a in assignments}
    branch_ids_for_labels.update({s.branch_id for s in sessions if s.branch_id})
    branches = _branch_labels(branch_ids_for_labels, db)

    summary: dict[str, dict] = {}
    detail_rows: list[list] = []
    for idx, assignment in enumerate(assignments, 1):
        emp = emp_by_id.get(assignment.employee_id) or emp_by_code.get(assignment.emp_code)
        shift = shifts.get(assignment.shift_id)
        session = session_by_assignment.get(assignment.id)
        emp_code = assignment.emp_code
        emp_name = (emp.name or emp.full_name) if emp else emp_code
        role_label = _role_label_for_log(
            AttendanceLog(emp_code=emp_code, department=(emp.position if emp else "")),
            emp,
        )
        assignment_branch_id = assignment.branch_id or (session.branch_id if session else None)
        branch_label = branches.get(assignment_branch_id, f"Chi nhánh #{assignment_branch_id}" if assignment_branch_id else "Chưa xác định")
        employment = _employment_label(emp.employment_type if emp else None)
        leave_approved = assignment.status == LEAVE_ASSIGNMENT_STATUS
        work_credit = 1 if (not leave_approved and session_counts_as_work(session)) else 0
        worked_minutes = payable_work_minutes(session) if work_credit else 0
        leave_credit, leave_minutes = leave_credit_for_assignment(assignment, shift)
        ot_recorded = recorded_overtime_minutes(session)
        needs_review = _session_needs_review(session)
        missing_checkout = missing_checkout_recorded(session)
        missing_checkin = missing_checkin_recorded(session)
        absent = confirmed_absent(session)
        rejected = bool(session and session.review_status == "rejected")

        row = summary.setdefault(emp_code, {
            "emp_code": emp_code,
            "emp_name": emp_name,
            "branches": set(),
            "role": role_label,
            "employment": employment,
            "assigned": 0,
            "work_credit": 0,
            "worked_minutes": 0,
            "leave_credit": 0,
            "leave_minutes": 0,
            "ot_recorded": 0,
            "late_count": 0,
            "late_minutes": 0,
            "early_leave_minutes": 0,
            "absent": 0,
            "missing_checkin": 0,
            "missing_checkout": 0,
            "pending_review": 0,
        })
        row["branches"].add(branch_label)
        row["assigned"] += 1
        row["work_credit"] += work_credit
        row["worked_minutes"] += worked_minutes
        row["leave_credit"] += leave_credit
        row["leave_minutes"] += leave_minutes
        row["ot_recorded"] += ot_recorded
        if session and session.late_minutes:
            row["late_count"] += 1
            row["late_minutes"] += int(session.late_minutes or 0)
        if session and session.early_leave_minutes:
            row["early_leave_minutes"] += int(session.early_leave_minutes or 0)
        if absent:
            row["absent"] += 1
        if missing_checkin:
            row["missing_checkin"] += 1
        if missing_checkout:
            row["missing_checkout"] += 1
        if needs_review:
            row["pending_review"] += 1

        note_parts = [assignment.note or ""]
        if session:
            note_parts.extend([session.note or "", session.review_note or ""])
        detail_rows.append([
            idx,
            assignment.work_date.isoformat(),
            branch_label,
            emp_code,
            emp_name,
            role_label,
            employment,
            shift.name if shift else f"Ca #{assignment.shift_id}",
            f"{shift.work_start}-{shift.work_end}" if shift else "",
            _dt_text(session.check_in_at if session else None),
            _dt_text(session.check_out_at if session else None),
            "Nghỉ phép" if leave_approved else ("Từ chối" if rejected else _session_status_label(session)),
            _review_label(session.review_status if session else None),
            work_credit,
            _hours(worked_minutes),
            leave_credit,
            _hours(leave_minutes),
            _hours(ot_recorded),
            int(session.late_minutes or 0) if session else 0,
            int(session.early_leave_minutes or 0) if session else 0,
            " | ".join([p for p in note_parts if p]),
        ])

    summary_rows = []
    for idx, row in enumerate(sorted(summary.values(), key=lambda item: (",".join(sorted(item["branches"])), item["emp_name"])), 1):
        summary_rows.append([
            idx,
            row["emp_code"],
            row["emp_name"],
            ", ".join(sorted(row["branches"])),
            row["role"],
            row["employment"],
            row["assigned"],
            row["work_credit"],
            _hours(row["worked_minutes"]),
            row["leave_credit"],
            _hours(row["leave_minutes"]),
            _hours(row["ot_recorded"]),
            row["late_count"],
            row["late_minutes"],
            row["early_leave_minutes"],
            row["absent"],
            row["missing_checkin"],
            row["missing_checkout"],
            row["pending_review"],
        ])

    logs = (
        db.query(AttendanceLog)
          .filter(AttendanceLog.timestamp >= start, AttendanceLog.timestamp <= end)
          .order_by(AttendanceLog.timestamp)
          .all()
    )
    logs = _filter_logs_for_branch_ids(db, logs, branch_ids)
    log_emp_by_id, log_emp_by_code, log_branches = _employee_report_context(logs, db)
    loose_rows = []
    loose_idx = 1
    for log in logs:
        if _log_linked_to_shift_session(db, log):
            continue
        emp = _employee_for_log(log, log_emp_by_id, log_emp_by_code)
        loose_rows.append([
            loose_idx,
            log.emp_code,
            log.emp_name,
            _branch_label_for_log(log, log_branches, db),
            _role_label_for_log(log, emp),
            "Vào" if log.check_type == "check_in" else "Ra",
            _dt_text(log.timestamp),
            log.note or "",
            round(float(log.confidence or 0) * 100, 1) if log.confidence else "",
        ])
        loose_idx += 1

    styles = (Alignment, Border, Font, PatternFill, Side, get_column_letter)
    wb = Workbook()
    ws_summary = wb.active
    _write_table_sheet(
        ws_summary,
        "Tong hop",
        ["STT", "Mã NV", "Họ tên", "Chi nhánh", "Vai trò", "Loại nhân sự", "Số ca phân", "Công làm", "Giờ làm thực tế", "Công phép", "Giờ phép", "OT ghi nhận (giờ)", "Số lần đi muộn", "Phút đi muộn", "Phút về sớm", "Vắng xác nhận", "Ca thiếu check-in", "Ca quên checkout", "Ca cần duyệt"],
        summary_rows,
        [6, 12, 24, 22, 18, 14, 12, 12, 16, 12, 14, 16, 14, 14, 14, 14, 16, 16, 14],
        styles,
    )
    _write_table_sheet(
        wb.create_sheet("Chi tiet ca"),
        "Chi tiet ca",
        ["STT", "Ngày", "Chi nhánh", "Mã NV", "Họ tên", "Vai trò", "Loại nhân sự", "Ca", "Giờ ca", "Giờ vào", "Giờ ra", "Trạng thái ca", "Trạng thái duyệt", "Công làm", "Giờ làm thực tế", "Công phép", "Giờ phép", "OT ghi nhận (giờ)", "Đi muộn (phút)", "Về sớm (phút)", "Ghi chú"],
        detail_rows,
        [6, 12, 22, 12, 24, 18, 14, 18, 14, 18, 18, 16, 16, 12, 16, 12, 14, 16, 14, 14, 30],
        styles,
    )
    _write_table_sheet(
        wb.create_sheet("Cham cong le"),
        "Cham cong le",
        ["STT", "Mã NV", "Họ tên", "Chi nhánh", "Vai trò", "Loại", "Thời gian", "Trạng thái", "Độ chính xác (%)"],
        loose_rows,
        [6, 12, 24, 22, 18, 10, 20, 24, 16],
        styles,
    )

    settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = str(settings.EXPORTS_DIR / f"bangcong_{from_date}_{to_date}.xlsx")
    wb.save(filepath)

    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"BangCong_{from_date}_{to_date}.xlsx",
    )


def _leave_status_label(status: str | None) -> str:
    return {
        "approved": "Đã duyệt",
        "pending": "Chờ duyệt",
        "rejected": "Từ chối",
        "cancelled": "Đã hủy",
    }.get(status or "", status or "")


def _leave_day_detail(day: dict) -> str:
    return {
        "am": "Nghỉ buổi sáng",
        "pm": "Nghỉ buổi chiều",
    }.get(day.get("half"), "Nghỉ cả ngày")


@router.get("/reports/attendance-exceptions")
def attendance_exceptions(
    from_date: str,
    to_date: str,
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    start, end = _parse_export_range(from_date, to_date)
    fd, td = start.date(), end.date()

    allowed_emp_codes: set[str] | None = None
    if branch_ids is not None:
        allowed_emp_codes = {
            row[0]
            for row in db.query(Employee.emp_code).filter(Employee.branch_id.in_(branch_ids)).all()
        }

    leave_q = db.query(LeaveRequest).filter(
        LeaveRequest.request_type == "leave",
        LeaveRequest.status.in_(["approved", "pending"]),
    )
    if allowed_emp_codes is not None:
        if not allowed_emp_codes:
            leave_q = leave_q.filter(LeaveRequest.id == -1)
        else:
            leave_q = leave_q.filter(LeaveRequest.emp_code.in_(list(allowed_emp_codes)))
    leave_requests = leave_q.order_by(LeaveRequest.submitted_at.desc()).all()

    session_q = db.query(AttendanceSession).filter(
        AttendanceSession.work_date >= fd,
        AttendanceSession.work_date <= td,
        AttendanceSession.review_status == "pending_review",
        AttendanceSession.review_type.in_(["absent", "missing_checkout", "missing_checkin"]),
    )
    if branch_ids is not None:
        session_q = session_q.filter(AttendanceSession.branch_id.in_(branch_ids))
    sessions = session_q.order_by(AttendanceSession.work_date.asc(), AttendanceSession.id.asc()).all()

    emp_codes = {req.emp_code for req in leave_requests}
    employee_ids = {session.employee_id for session in sessions if session.employee_id}
    employees = []
    if emp_codes or employee_ids:
        q = db.query(Employee)
        if emp_codes and employee_ids:
            from sqlalchemy import or_
            q = q.filter(or_(Employee.emp_code.in_(list(emp_codes)), Employee.id.in_(list(employee_ids))))
        elif emp_codes:
            q = q.filter(Employee.emp_code.in_(list(emp_codes)))
        else:
            q = q.filter(Employee.id.in_(list(employee_ids)))
        employees = q.all()
    emp_by_code = {emp.emp_code: emp for emp in employees}
    emp_by_id = {emp.id: emp for emp in employees}

    branch_ids_for_labels = {emp.branch_id for emp in employees if emp.branch_id}
    branch_ids_for_labels.update({session.branch_id for session in sessions if session.branch_id})
    branches = _branch_labels(branch_ids_for_labels, db)

    items: list[dict] = []
    for req in leave_requests:
        emp = emp_by_code.get(req.emp_code)
        branch_id_for_row = emp.branch_id if emp else None
        for day in req.get_dates():
            raw_date = day.get("date")
            if not raw_date:
                continue
            try:
                d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < fd or d > td:
                continue
            items.append({
                "type": "leave",
                "date": d.isoformat(),
                "emp_code": req.emp_code,
                "emp_name": req.emp_name or (emp.name if emp else ""),
                "branch": branches.get(branch_id_for_row, f"Chi nhánh #{branch_id_for_row}" if branch_id_for_row else ""),
                "shift_name": "",
                "status": _leave_status_label(req.status),
                "detail": _leave_day_detail(day),
                "note": req.reason or req.note or "",
            })

    for session in sessions:
        row = _session_review_to_dict(session, db)
        review_type = row.get("review_type") or ""
        items.append({
            "type": review_type,
            "date": row.get("work_date") or "",
            "emp_code": row.get("emp_code") or "",
            "emp_name": row.get("emp_name") or "",
            "branch": row.get("branch_name") or "",
            "shift_name": row.get("shift_name") or "",
            "status": _review_label(row.get("review_status")),
            "detail": (
                "Không check-in cả ca"
                if review_type == "absent"
                else ("Thiếu check-in, cần quản lý xác nhận giờ vào" if review_type == "missing_checkin" else "Quên checkout")
            ),
            "note": row.get("review_note") or row.get("note") or "",
        })

    order = {"leave": 0, "absent": 1, "missing_checkin": 2, "missing_checkout": 3}
    items.sort(key=lambda item: (item.get("date") or "", order.get(item.get("type"), 9), item.get("emp_name") or ""))
    summary = {"leave": 0, "absent": 0, "missing_checkin": 0, "missing_checkout": 0, "total": len(items)}
    for item in items:
        if item["type"] in summary:
            summary[item["type"]] += 1
    return {"summary": summary, "items": items}


# ── GET /api/reports/employee/{emp_code}/stats ───────────────────

@router.get("/reports/employee/{emp_code}/stats")
def employee_stats(
    emp_code: str,
    year: int = 0,
    month: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    from app.services.work_calendar import get_employee_stats, get_employee_stats_month
    from datetime import date as _date
    emp = db.query(Employee).filter_by(emp_code=emp_code).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân viên")
    if current_user.role == "staff":
        own = _employee_for_user(db, current_user)
        if not own or own.emp_code != emp_code:
            raise HTTPException(status_code=403, detail="Bạn chỉ được xem thống kê của mình")
    else:
        ensure_branch_access(db, current_user, emp.branch_id)
    y = year  or _date.today().year
    m = month or 0

    if m:
        return get_employee_stats_month(emp_code, y, m, db)
    return get_employee_stats(emp_code, y, db)


# ── Schemas điểm danh thủ công ────────────────────────────────────

class AttendanceUpdateRequest(BaseModel):
    check_type:    Optional[str] = None   # "check_in" | "check_out"
    timestamp:     Optional[str] = None   # "YYYY-MM-DD HH:MM:SS" hoặc ISO
    note:          Optional[str] = None
    reason:        str


class AttendanceCreateRequest(BaseModel):
    emp_code:   str
    check_type: str                       # "check_in" | "check_out"
    timestamp:  str                       # "YYYY-MM-DD HH:MM:SS" hoặc ISO
    note:       Optional[str] = ""
    reason:     str


class AuditRunRequest(BaseModel):
    branch_id: Optional[int] = None
    run_type: str = "daily"  # daily | low_confidence | employee_day
    date: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    emp_code: Optional[str] = ""
    use_ai: bool = True


class AuditReviewRequest(BaseModel):
    review_status: str
    note: Optional[str] = ""


class AttendanceSessionReviewRequest(BaseModel):
    review_status: str
    note: Optional[str] = ""
    attach_unscheduled_shift: bool = False
    shift_id: Optional[int] = None
    check_in_at: Optional[str] = None
    check_out_at: Optional[str] = None


class AbsentSessionManualLogsRequest(BaseModel):
    check_in_at: str
    check_out_at: str
    note: Optional[str] = ""
    reason: str


def _shift_overlap_minutes(work_date, shift: Shift, start: datetime, end: datetime) -> int:
    shift_start, shift_end, _from, _until = shift_window(work_date, shift)
    overlap_start = max(start, shift_start)
    overlap_end = min(end, shift_end)
    return max(0, int((overlap_end - overlap_start).total_seconds() / 60))


def _shift_review_candidate_dict(session: AttendanceSession, shift: Shift) -> dict:
    shift_start, shift_end, checkin_from, checkout_until = shift_window(session.work_date, shift)
    overlap = _shift_overlap_minutes(session.work_date, shift, session.check_in_at, session.check_out_at)
    return {
        "id": shift.id,
        "branch_id": shift.branch_id,
        "name": shift.name,
        "code": shift.code,
        "work_start": shift.work_start,
        "work_end": shift.work_end,
        "required_position": shift.required_position or "",
        "shift_start": shift_start.isoformat(),
        "shift_end": shift_end.isoformat(),
        "checkin_from": checkin_from.isoformat(),
        "checkout_until": checkout_until.isoformat(),
        "overlap_minutes": overlap,
        "contains_session": bool(shift_start <= session.check_in_at and session.check_out_at <= shift_end),
        "in_attendance_window": bool(checkin_from <= session.check_in_at and session.check_out_at <= checkout_until),
    }


def _unscheduled_shift_candidates(db: Session, session: AttendanceSession, branch_id: int | None) -> list[dict]:
    if not session.check_in_at or not session.check_out_at:
        return []
    q = db.query(Shift).filter(Shift.is_active.is_(True))
    if branch_id is None:
        q = q.filter(Shift.branch_id.is_(None))
    else:
        q = q.filter((Shift.branch_id == branch_id) | (Shift.branch_id.is_(None)))
    candidates = [
        _shift_review_candidate_dict(session, shift)
        for shift in q.order_by(Shift.branch_id.desc(), Shift.work_start, Shift.name).all()
    ]
    candidates.sort(
        key=lambda item: (
            not item["in_attendance_window"],
            not item["contains_session"],
            -item["overlap_minutes"],
            item["work_start"],
            item["name"],
        )
    )
    return candidates


def _approve_unscheduled_session(
    db: Session,
    current_user,
    session: AttendanceSession,
    *,
    attach_shift: bool = False,
    selected_shift_id: int | None = None,
) -> ShiftAssignment:
    if not attach_shift or not selected_shift_id:
        raise HTTPException(
            status_code=400,
            detail="Duyệt ngoài ca cần chọn một ca làm việc có sẵn",
        )
    if not session.check_in_at or not session.check_out_at:
        raise HTTPException(
            status_code=400,
            detail="Chấm công ngoài ca cần có đủ giờ vào và giờ ra trước khi duyệt/gắn ca",
        )
    if session.check_out_at <= session.check_in_at:
        raise HTTPException(status_code=400, detail="Giờ ra phải sau giờ vào")
    emp = db.query(Employee).filter_by(id=session.employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân viên của phiên chấm công")
    branch_id = session.branch_id or emp.branch_id
    ensure_branch_access(db, current_user, branch_id)
    ensure_period_unlocked(db, branch_id, session.work_date)
    _ensure_assignable_workday(session.work_date, db, branch_id)

    shift = db.query(Shift).filter_by(id=selected_shift_id, is_active=True).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Không tìm thấy ca làm việc đã chọn")
    if shift.branch_id is not None and shift.branch_id != branch_id:
        raise HTTPException(status_code=403, detail="Ca đã chọn không thuộc chi nhánh của phiên ngoài ca")
    if not _employee_role_matches_shift(emp, shift):
        role = emp.job_role or emp.position or "chưa xác định"
        raise HTTPException(
            status_code=400,
            detail=f"Nhân viên {emp.emp_code} có vai trò '{role}' không phù hợp với ca yêu cầu '{shift.required_position}'",
        )
    assignment = (
        db.query(ShiftAssignment)
          .filter_by(emp_code=emp.emp_code, work_date=session.work_date, shift_id=shift.id)
          .first()
    )
    try:
        ensure_can_assign_employee(db, emp.emp_code, session.work_date, assignment)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if assignment:
        assignment.employee_id = emp.id
        assignment.branch_id = branch_id
        assignment.status = UNSCHEDULED_APPROVED_ASSIGNMENT_STATUS
        assignment.assigned_by = current_user.email
        assignment.assigned_by_id = current_user.id
        assignment.note = f"Gắn phiên ngoài ca #{session.id} vào ca có sẵn"
    else:
        assignment = ShiftAssignment(
            employee_id=emp.id,
            branch_id=branch_id,
            emp_code=emp.emp_code,
            shift_id=shift.id,
            work_date=session.work_date,
            status=UNSCHEDULED_APPROVED_ASSIGNMENT_STATUS,
            assigned_by=current_user.email,
            assigned_by_id=current_user.id,
            note=f"Gắn phiên ngoài ca #{session.id} vào ca có sẵn",
        )
        db.add(assignment)
        db.flush()

    linked = (
        db.query(AttendanceSession)
          .filter(
              AttendanceSession.shift_assignment_id == assignment.id,
              AttendanceSession.id != session.id,
          )
          .first()
    )
    if linked:
        raise HTTPException(status_code=409, detail="Khoảng ngoài ca này đã được gắn với phiên chấm công khác")

    session.branch_id = branch_id
    session.shift_id = shift.id
    session.shift_assignment_id = assignment.id
    session.status = "completed"
    session.check_in_status = session.check_in_status or "unscheduled"
    session.check_out_status = session.check_out_status or "unscheduled"
    session.break_minutes = shift.break_minutes or 0
    session.worked_minutes = max(0, int((session.check_out_at - session.check_in_at).total_seconds() / 60) - int(shift.break_minutes or 0))
    session.note = f"Đã duyệt ngoài ca và gắn vào ca {shift.name}"

    events = (
        db.query(AttendanceEvent)
          .filter(
              AttendanceEvent.employee_id == emp.id,
              AttendanceEvent.event_time >= session.check_in_at,
              AttendanceEvent.event_time <= session.check_out_at,
              or_(
                  AttendanceEvent.session_id.is_(None),
                  AttendanceEvent.session_id == session.id,
              ),
          )
          .all()
    )
    for event in events:
        event.session_id = session.id
        event.branch_id = branch_id
    return assignment


def _parse_review_datetime(value: str | None, field: str) -> datetime:
    if not (value or "").strip():
        raise HTTPException(status_code=422, detail=f"{field} không được để trống")
    raw = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise HTTPException(status_code=422, detail=f"{field} phải định dạng YYYY-MM-DD HH:MM:SS")


def _checkout_log_for_missing_session(
    db: Session,
    session: AttendanceSession,
    checkout_at: datetime | None,
) -> AttendanceLog | None:
    if not checkout_at:
        return None
    return (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.employee_id == session.employee_id,
              AttendanceLog.check_type == "check_out",
              AttendanceLog.timestamp == checkout_at,
          )
          .order_by(AttendanceLog.id.desc())
          .first()
    )


def _checkin_log_for_missing_session(
    db: Session,
    session: AttendanceSession,
    checkin_at: datetime | None,
) -> AttendanceLog | None:
    if not checkin_at:
        return None
    return (
        db.query(AttendanceLog)
          .filter(
              AttendanceLog.employee_id == session.employee_id,
              AttendanceLog.check_type == "check_in",
              AttendanceLog.timestamp == checkin_at,
          )
          .order_by(AttendanceLog.id.desc())
          .first()
    )


def _apply_missing_checkout_review(
    db: Session,
    session: AttendanceSession,
    body: AttendanceSessionReviewRequest,
    reviewer: str,
) -> None:
    if body.review_status in ("pending_review", "rejected"):
        session.status = "missing_checkout"
        return
    if body.review_status != "approved":
        return
    if not session.check_in_at:
        raise HTTPException(status_code=422, detail="Phiên quên checkout chưa có giờ vào")

    previous_checkout_at = session.check_out_at
    checkout_at = _parse_review_datetime(body.check_out_at, "Giờ ra") if body.check_out_at else previous_checkout_at
    if not checkout_at:
        raise HTTPException(status_code=422, detail="Phiên quên checkout chưa có giờ ra")
    if checkout_at <= session.check_in_at:
        raise HTTPException(status_code=422, detail="Giờ ra phải sau giờ vào")

    locked_dates = [session.work_date, checkout_at.date()]
    if previous_checkout_at:
        locked_dates.append(previous_checkout_at.date())
    try:
        ensure_period_unlocked(db, session.branch_id, min(locked_dates), max(locked_dates))
    except ValueError as e:
        raise HTTPException(status_code=423, detail=str(e))

    shift = db.query(Shift).filter_by(id=session.shift_id).first() if session.shift_id else None
    if shift:
        _shift_start, shift_end, checkin_from, checkout_until = shift_window(session.work_date, shift)
        if not (checkin_from <= checkout_at <= checkout_until):
            raise HTTPException(status_code=422, detail="Giờ ra phải nằm trong cửa sổ chấm công của ca")
        session.early_leave_minutes = max(0, int((shift_end - checkout_at).total_seconds() / 60))
        session.overtime_minutes = 0
    else:
        session.early_leave_minutes = 0
        session.overtime_minutes = 0

    checkout_log = _checkout_log_for_missing_session(db, session, previous_checkout_at)
    session.check_out_at = checkout_at
    session.status = "completed"
    if body.check_out_at and previous_checkout_at != checkout_at:
        session.check_out_status = "manual"
    else:
        session.check_out_status = session.check_out_status or "auto"
    session.worked_minutes = max(0, int((checkout_at - session.check_in_at).total_seconds() / 60) - int(session.break_minutes or 0))

    if checkout_log:
        checkout_log.timestamp = checkout_at
        trail = f"[Duyệt quên checkout bởi {reviewer} lúc {datetime.now().strftime('%H:%M %d/%m/%Y')}]"
        checkout_log.note = f"{checkout_log.note or 'Tự động chấm ra - nhân viên quên check out'} {trail}".strip()


def _apply_missing_checkin_review(
    db: Session,
    session: AttendanceSession,
    body: AttendanceSessionReviewRequest,
    reviewer: str,
) -> None:
    if body.review_status in ("pending_review", "rejected"):
        session.status = "missing_checkin"
        return
    if body.review_status != "approved":
        return
    if not session.check_out_at:
        raise HTTPException(status_code=422, detail="Phiên thiếu check-in chưa có giờ ra")

    previous_checkin_at = session.check_in_at
    checkin_at = _parse_review_datetime(body.check_in_at, "Giờ vào") if body.check_in_at else previous_checkin_at
    if not checkin_at:
        shift = db.query(Shift).filter_by(id=session.shift_id).first() if session.shift_id else None
        if shift:
            shift_start, _shift_end, _from, _until = shift_window(session.work_date, shift)
            checkin_at = shift_start
    if not checkin_at:
        raise HTTPException(status_code=422, detail="Phiên thiếu check-in cần nhập giờ vào")
    if session.check_out_at <= checkin_at:
        raise HTTPException(status_code=422, detail="Giờ vào phải trước giờ ra")

    locked_dates = [session.work_date, checkin_at.date(), session.check_out_at.date()]
    if previous_checkin_at:
        locked_dates.append(previous_checkin_at.date())
    try:
        ensure_period_unlocked(db, session.branch_id, min(locked_dates), max(locked_dates))
    except ValueError as e:
        raise HTTPException(status_code=423, detail=str(e))

    shift = db.query(Shift).filter_by(id=session.shift_id).first() if session.shift_id else None
    if shift:
        shift_start, shift_end, checkin_from, checkout_until = shift_window(session.work_date, shift)
        if not (checkin_from <= checkin_at <= checkout_until):
            raise HTTPException(status_code=422, detail="Giờ vào phải nằm trong cửa sổ chấm công của ca")
        if not (checkin_from <= session.check_out_at <= checkout_until):
            raise HTTPException(status_code=422, detail="Giờ ra phải nằm trong cửa sổ chấm công của ca")
        threshold = shift.late_threshold_minutes if shift.late_threshold_minutes is not None else settings.CHECKIN_GRACE_MINUTES
        raw_late = max(0, int((checkin_at - shift_start).total_seconds() / 60))
        session.late_minutes = max(0, raw_late - threshold)
        session.early_leave_minutes = max(0, int((shift_end - session.check_out_at).total_seconds() / 60))
        raw_overtime = max(0, int((session.check_out_at - shift_end).total_seconds() / 60))
        session.overtime_minutes = raw_overtime if raw_overtime > settings.OVERTIME_APPROVAL_THRESHOLD_MINUTES else 0
    else:
        session.late_minutes = 0
        session.early_leave_minutes = 0
        session.overtime_minutes = 0

    emp = db.query(Employee).filter_by(id=session.employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân viên của phiên chấm công")

    checkin_log = _checkin_log_for_missing_session(db, session, previous_checkin_at)
    trail = f"[Duyệt thiếu check-in bởi {reviewer} lúc {datetime.now().strftime('%H:%M %d/%m/%Y')}]"
    note = f"Bổ sung giờ vào khi duyệt thiếu check-in {trail}".strip()
    session.check_in_at = checkin_at
    session.status = "completed"
    session.check_in_status = "manual" if body.check_in_at or not checkin_log else (session.check_in_status or "manual")
    session.check_out_status = session.check_out_status or "normal"
    gross_minutes = int((session.check_out_at - session.check_in_at).total_seconds() / 60)
    session.worked_minutes = max(0, gross_minutes - int(session.break_minutes or 0))

    if checkin_log:
        checkin_log.timestamp = checkin_at
        checkin_log.note = f"{checkin_log.note or 'Bổ sung giờ vào'} {trail}".strip()
    else:
        checkin_log = AttendanceLog(
            employee_id=emp.id,
            emp_code=emp.emp_code,
            emp_name=emp.name,
            department=emp.department,
            check_type="check_in",
            timestamp=checkin_at,
            confidence=0.0,
            capture_path="",
            note=note,
        )
        db.add(checkin_log)
        db.flush()

    event = (
        db.query(AttendanceEvent)
          .filter(
              AttendanceEvent.employee_id == emp.id,
              AttendanceEvent.event_type == "check_in",
              AttendanceEvent.event_time == checkin_at,
          )
          .order_by(AttendanceEvent.id.desc())
          .first()
    )
    if event:
        event.session_id = session.id
        event.branch_id = session.branch_id
        event.note = event.note or note
    else:
        db.add(AttendanceEvent(
            session_id=session.id,
            employee_id=emp.id,
            branch_id=session.branch_id,
            event_type="check_in",
            event_time=checkin_at,
            confidence=0.0,
            source="manual",
            note=note,
        ))


class AttendancePeriodLockRequest(BaseModel):
    branch_id: Optional[int] = None
    from_date: str
    to_date: str
    note: Optional[str] = ""


def _session_review_to_dict(session: AttendanceSession, db: Session) -> dict:
    emp = db.query(Employee).filter_by(id=session.employee_id).first()
    shift = db.query(Shift).filter_by(id=session.shift_id).first() if session.shift_id else None
    branch = db.query(Branch).filter_by(id=session.branch_id).first() if session.branch_id else None
    shift_start = None
    shift_end = None
    checkin_from = None
    checkout_until = None
    if shift and session.work_date:
        shift_start, shift_end, checkin_from, checkout_until = shift_window(session.work_date, shift)
    data = {
        "id": session.id,
        "employee_id": session.employee_id,
        "emp_code": emp.emp_code if emp else "",
        "emp_name": emp.name if emp else "",
        "department": emp.department if emp else "",
        "role_label": _role_label_for_log(
            AttendanceLog(emp_code=emp.emp_code if emp else "", department=emp.department if emp else ""),
            emp,
        ) if emp else "",
        "branch_name": branch.name if branch else "",
        "branch_id": session.branch_id,
        "shift_assignment_id": session.shift_assignment_id,
        "shift_id": session.shift_id,
        "shift_name": shift.name if shift else "",
        "shift_start": shift_start.isoformat() if shift_start else None,
        "shift_end": shift_end.isoformat() if shift_end else None,
        "checkin_from": checkin_from.isoformat() if checkin_from else None,
        "checkout_until": checkout_until.isoformat() if checkout_until else None,
        "work_date": session.work_date.isoformat() if session.work_date else "",
        "check_in_at": session.check_in_at.isoformat() if session.check_in_at else None,
        "check_out_at": session.check_out_at.isoformat() if session.check_out_at else None,
        "status": session.status,
        "check_in_status": session.check_in_status or "",
        "check_out_status": session.check_out_status or "",
        "late_minutes": session.late_minutes or 0,
        "early_leave_minutes": session.early_leave_minutes or 0,
        "overtime_minutes": session.overtime_minutes or 0,
        "worked_minutes": session.worked_minutes or 0,
        "review_type": session.review_type or "",
        "review_status": session.review_status or "none",
        "review_note": session.review_note or "",
        "reviewed_by": session.reviewed_by or "",
        "reviewed_at": session.reviewed_at.isoformat() if session.reviewed_at else None,
        "note": session.note or "",
    }
    if session.review_type == "unscheduled" and session.check_in_at and session.check_out_at:
        data["shift_candidates"] = _unscheduled_shift_candidates(db, session, session.branch_id or (emp.branch_id if emp else None))
    return data


# ── GET /api/attendance/sessions/{session_id}/events ─────────────

@router.get("/attendance/sessions/{session_id}/events")
def get_attendance_session_events(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Lấy các event/bằng chứng ảnh của một phiên làm việc."""
    session = db.query(AttendanceSession).filter_by(id=session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chấm công")
    _ensure_session_scope(db, current_user, session)
    rows = (
        db.query(AttendanceEvent)
          .filter_by(session_id=session_id)
          .order_by(AttendanceEvent.event_time.asc())
          .all()
    )
    return {
        "session_id": session_id,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "event_time": e.event_time.isoformat() if e.event_time else None,
                "confidence": e.confidence,
                "capture_available": bool(e.capture_path),
                "image_hash": e.image_hash or "",
                "source": e.source,
                "note": e.note,
            }
            for e in rows
        ],
    }


# ── GET /api/attendance/review-items ─────────────────────────────

@router.get("/attendance/review-items")
def get_attendance_review_items(
    type: str = "",
    status: str = "pending_review",
    from_date: str = "",
    to_date: str = "",
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    q = db.query(AttendanceSession)
    if branch_ids is not None:
        q = q.filter(AttendanceSession.branch_id.in_(branch_ids))
    if status:
        q = q.filter(AttendanceSession.review_status == status)
    if type:
        q = q.filter(AttendanceSession.review_type == type)
    if from_date:
        q = q.filter(AttendanceSession.work_date >= datetime.strptime(from_date, "%Y-%m-%d").date())
    if to_date:
        q = q.filter(AttendanceSession.work_date <= datetime.strptime(to_date, "%Y-%m-%d").date())
    rows = q.order_by(AttendanceSession.work_date.desc(), AttendanceSession.id.desc()).limit(500).all()
    return {"items": [_session_review_to_dict(row, db) for row in rows], "total": len(rows)}


@router.get("/attendance/review-count")
def get_attendance_review_count(
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    q = db.query(AttendanceSession.review_type, AttendanceSession.id).filter(AttendanceSession.review_status == "pending_review")
    if branch_ids is not None:
        q = q.filter(AttendanceSession.branch_id.in_(branch_ids))
    rows = q.all()
    counts = {"total": len(rows), "absent": 0, "missing_checkin": 0, "missing_checkout": 0, "overtime": 0, "unscheduled": 0}
    for review_type, _id in rows:
        if review_type in counts:
            counts[review_type] += 1
    return counts


@router.post("/attendance/sessions/{session_id}/absent-manual-logs")
def create_absent_manual_logs(
    session_id: int,
    body: AbsentSessionManualLogsRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    session = db.query(AttendanceSession).filter_by(id=session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chấm công")
    _ensure_session_scope(db, current_user, session)
    try:
        result = create_absent_session_manual_logs(
            session_id=session_id,
            check_in_timestamp_str=body.check_in_at,
            check_out_timestamp_str=body.check_out_at,
            note=body.note or "",
            created_by=current_user.full_name or current_user.email,
            created_by_id=current_user.id,
            reason=body.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chấm công")
    db.expire_all()
    fresh = db.query(AttendanceSession).filter_by(id=session_id).first()
    return {
        "success": True,
        "message": "Đã tạo log vào/ra cho ca vắng",
        "logs": [result["check_in_log"], result["check_out_log"]],
        "session": _session_review_to_dict(fresh, db) if fresh else None,
    }


@router.put("/attendance/sessions/{session_id}/review")
def review_attendance_session(
    session_id: int,
    body: AttendanceSessionReviewRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    if body.review_status not in ("pending_review", "approved", "rejected"):
        raise HTTPException(status_code=422, detail="review_status không hợp lệ")
    session = db.query(AttendanceSession).filter_by(id=session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chấm công")
    _ensure_session_scope(db, current_user, session)
    try:
        ensure_period_unlocked(db, session.branch_id, session.work_date)
    except ValueError as e:
        raise HTTPException(status_code=423, detail=str(e))
    assignment = None
    if session.review_type == "unscheduled" and body.review_status == "approved":
        assignment = _approve_unscheduled_session(
            db,
            current_user,
            session,
            attach_shift=body.attach_unscheduled_shift,
            selected_shift_id=body.shift_id,
        )
    session.review_status = body.review_status
    session.review_note = body.note or ""
    session.reviewed_by = current_user.full_name or current_user.email
    session.reviewed_at = datetime.now() if body.review_status != "pending_review" else None
    session.updated_by_id = current_user.id
    if session.review_type == "missing_checkout":
        _apply_missing_checkout_review(
            db,
            session,
            body,
            current_user.full_name or current_user.email,
        )
    elif session.review_type == "missing_checkin":
        _apply_missing_checkin_review(
            db,
            session,
            body,
            current_user.full_name or current_user.email,
        )
    elif session.review_type == "unscheduled" and body.review_status == "rejected":
        session.status = "cancelled"
    db.commit()
    db.refresh(session)
    return {
        "success": True,
        "session": _session_review_to_dict(session, db),
        "assignment_id": assignment.id if assignment else None,
    }


def _parse_lock_date(value: str, field: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=422, detail=f"{field} phải định dạng YYYY-MM-DD")


def _lock_branch_for_user(db: Session, current_user, branch_id: int | None) -> int | None:
    _require_manager_or_admin(current_user)
    if current_user.role == "admin":
        if branch_id is not None:
            ensure_branch_access(db, current_user, branch_id)
        return branch_id
    branch_ids = selected_branch_ids(db, current_user, branch_id)
    if branch_id is None:
        if len(branch_ids) == 1:
            return branch_ids[0]
        raise HTTPException(status_code=400, detail="Manager cần chọn chi nhánh để khóa kỳ")
    if branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Không có quyền với chi nhánh này")
    return branch_id


@router.get("/attendance/period-locks")
def get_attendance_period_locks(
    branch_id: int | None = None,
    active_only: bool = True,
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    rows = list_period_locks(
        db,
        branch_ids=branch_ids,
        active_only=active_only,
        from_date=_parse_lock_date(from_date, "from_date") if from_date else None,
        to_date=_parse_lock_date(to_date, "to_date") if to_date else None,
    )
    return {"items": [period_lock_to_dict(row) for row in rows], "total": len(rows)}


@router.post("/attendance/period-locks", status_code=201)
def create_attendance_period_lock(
    body: AttendancePeriodLockRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_id = _lock_branch_for_user(db, current_user, body.branch_id)
    try:
        row = create_period_lock(
            db,
            branch_id=branch_id,
            from_date=_parse_lock_date(body.from_date, "from_date"),
            to_date=_parse_lock_date(body.to_date, "to_date"),
            locked_by=current_user.full_name or current_user.email,
            locked_by_id=current_user.id,
            note=body.note or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"success": True, "lock": period_lock_to_dict(row)}


@router.delete("/attendance/period-locks/{lock_id}")
def delete_attendance_period_lock(
    lock_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    row = db.query(AttendancePeriodLock).filter_by(id=lock_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy kỳ công")
    if row.branch_id is None and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Chỉ admin được mở khóa kỳ toàn hệ thống")
    if row.branch_id is not None:
        ensure_branch_access(db, current_user, row.branch_id)
    unlocked = unlock_period(
        db,
        lock_id,
        unlocked_by=current_user.full_name or current_user.email,
        unlocked_by_id=current_user.id,
    )
    return {"success": True, "lock": period_lock_to_dict(unlocked)}


# ── GET /api/attendance/{log_id}/capture ─────────────────────────

@router.get("/attendance/{log_id}/capture")
def get_attendance_capture(
    log_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Trả ảnh bằng chứng chấm công cho manager/admin, không expose thư mục captures."""
    _require_manager_or_admin(current_user)
    log = db.query(AttendanceLog).filter_by(id=log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")
    _ensure_log_scope(db, current_user, log)
    try:
        capture_file = _evidence_image_file_for_log(log_id, db)
    except HTTPException:
        capture_file = _capture_file_for_log(log)
    return FileResponse(
        str(capture_file),
        media_type="image/jpeg",
        filename=f"attendance_{log_id}.jpg",
    )


# ── AI audit review-first ───────────────────────────────────────

@router.post("/attendance/audit-runs")
def create_attendance_audit_run(
    body: AuditRunRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _manager_branch_ids(db, current_user, body.branch_id)
    try:
        if body.date:
            from_date = to_date = datetime.strptime(body.date, "%Y-%m-%d").date()
        else:
            from_date = datetime.strptime(body.from_date or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d").date()
            to_date = datetime.strptime(body.to_date or from_date.isoformat(), "%Y-%m-%d").date()
        if to_date < from_date:
            raise ValueError("to_date phải lớn hơn hoặc bằng from_date")
        run_type = body.run_type if body.run_type in ("daily", "low_confidence", "employee_day") else "daily"
        return run_attendance_audit(
            db=db,
            from_date=from_date,
            to_date=to_date,
            run_type=run_type,
            created_by=current_user.full_name or current_user.email,
            emp_code=(body.emp_code or "").strip(),
            branch_ids=branch_ids,
            use_ai=body.use_ai,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/attendance/audit-runs/{run_id}")
def get_attendance_audit_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    run = get_audit_run(db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Không tìm thấy lượt audit")
    return run


@router.get("/attendance/audit-findings")
def get_attendance_audit_findings(
    date: str = "",
    from_date: str = "",
    to_date: str = "",
    emp_code: str = "",
    review_status: str = "pending_review",
    risk_level: str = "",
    limit: int = 100,
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    branch_ids = _manager_branch_ids(db, current_user, branch_id)
    providers = list_ai_provider_settings(db)
    vision_ready = any(p.get("configured") and p.get("is_enabled") for p in providers)
    return {
        "findings": list_audit_findings(db, date, review_status, risk_level, limit, from_date, to_date, emp_code, branch_ids=branch_ids),
        "vision_ai_configured": vision_ready,
        "ai_provider": next((p["label"] for p in providers if p.get("configured") and p.get("is_enabled")), ""),
    }


@router.put("/attendance/audit-findings/{finding_id}/review")
def review_attendance_audit_finding(
    finding_id: int,
    body: AuditReviewRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    finding_row = db.query(AttendanceAuditFinding).filter_by(id=finding_id).first()
    if not finding_row:
        raise HTTPException(status_code=404, detail="Không tìm thấy finding")
    log_row = db.query(AttendanceLog).filter_by(id=finding_row.log_id).first()
    if log_row:
        _ensure_log_scope(db, current_user, log_row)
    try:
        finding = update_audit_finding_review(
            finding_id=finding_id,
            status=body.review_status,
            note=body.note or "",
            reviewer=current_user.full_name or current_user.email,
            db=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not finding:
        raise HTTPException(status_code=404, detail="Không tìm thấy finding")
    return {"success": True, "finding": finding}


@router.post("/attendance/{log_id}/review")
def review_attendance_log_manually(
    log_id: int,
    body: AuditReviewRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Đánh dấu soát tay cho một log bất kỳ, kể cả confidence >= 70%."""
    _require_manager_or_admin(current_user)
    if body.review_status not in ("reviewed", "dismissed", "confirmed", "pending_review"):
        raise HTTPException(status_code=422, detail="review_status không hợp lệ")
    log = db.query(AttendanceLog).filter_by(id=log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")
    _ensure_log_scope(db, current_user, log)
    evidence = (
        db.query(AttendanceEvidence)
          .filter_by(log_id=log.id)
          .order_by(AttendanceEvidence.id.desc())
          .first()
    )
    finding = (
        db.query(AttendanceAuditFinding)
          .filter_by(log_id=log.id, source="manual_review")
          .order_by(AttendanceAuditFinding.id.desc())
          .first()
    )
    if not finding:
        finding = AttendanceAuditFinding(log_id=log.id, source="manual_review")
        db.add(finding)
    finding.event_id = None
    finding.evidence_id = evidence.id if evidence else None
    finding.employee_id = log.employee_id
    finding.emp_code = log.emp_code or ""
    finding.emp_name = log.emp_name or ""
    finding.risk_score = 0.50 if body.review_status == "confirmed" else 0.0
    finding.risk_level = "medium" if body.review_status == "confirmed" else "clear"
    finding.reasons = '["manual_review"]'
    finding.metrics = "{}"
    finding.review_status = body.review_status
    finding.reviewer_note = body.note or ""
    finding.reviewed_by = current_user.full_name or current_user.email
    finding.reviewed_at = datetime.now() if body.review_status != "pending_review" else None
    finding.updated_at = datetime.now()
    db.commit()
    return {"success": True, "review_status": finding.review_status, "finding_id": finding.id}


# ── GET /api/attendance/{log_id} ─────────────────────────────────

@router.get("/attendance/{log_id}")
def get_attendance_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Lấy thông tin 1 bản ghi điểm danh theo ID."""
    log_row = db.query(AttendanceLog).filter_by(id=log_id).first()
    if not log_row:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")
    _ensure_log_scope(db, current_user, log_row)
    log = get_log_by_id(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")
    return log


# ── PUT /api/attendance/{log_id} ─────────────────────────────────

@router.put("/attendance/{log_id}")
def edit_attendance_log(
    log_id: int,
    body: AttendanceUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Chỉnh sửa bản ghi điểm danh.
    Yêu cầu quyền manager hoặc admin.
    """
    from app.services.auth_service import require_manager
    # Kiểm tra quyền manager/admin
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Chỉ quản lý (manager/admin) mới được chỉnh sửa điểm danh",
        )
    log_row = db.query(AttendanceLog).filter_by(id=log_id).first()
    if not log_row:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")
    _ensure_log_scope(db, current_user, log_row)

    if body.check_type and body.check_type not in ("check_in", "check_out"):
        raise HTTPException(status_code=422, detail="check_type phải là 'check_in' hoặc 'check_out'")

    try:
        updated = update_attendance_log(
            log_id       = log_id,
            check_type   = body.check_type,
            timestamp_str= body.timestamp,
            note         = body.note,
            updated_by   = current_user.full_name or current_user.email,
            updated_by_id= current_user.id,
            reason       = body.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if updated is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")

    return {"success": True, "message": "Đã cập nhật bản ghi điểm danh", "log": updated}


# ── DELETE /api/attendance/{log_id} ──────────────────────────────

@router.delete("/attendance/{log_id}")
def remove_attendance_log(
    log_id: int,
    reason: str,
    current_user=Depends(get_current_user),
):
    """
    Xoá bản ghi điểm danh.
    Yêu cầu quyền admin.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Chỉ admin mới được xoá bản ghi điểm danh",
        )

    try:
        deleted = delete_attendance_log(
            log_id,
            deleted_by=current_user.full_name or current_user.email,
            deleted_by_id=current_user.id,
            reason=reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")

    return {"success": True, "message": f"Đã xoá bản ghi #{log_id}", "audit_id": deleted.get("audit_id")}


# ── POST /api/attendance/manual ──────────────────────────────────

@router.post("/attendance/manual")
def add_manual_attendance(
    body: AttendanceCreateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Tạo thủ công bản ghi điểm danh bù (quản lý thêm khi nhân viên quên chấm).
    Yêu cầu quyền manager hoặc admin.
    """
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Chỉ quản lý (manager/admin) mới được tạo điểm danh thủ công",
        )

    if body.check_type not in ("check_in", "check_out"):
        raise HTTPException(status_code=422, detail="check_type phải là 'check_in' hoặc 'check_out'")
    emp = db.query(Employee).filter_by(emp_code=body.emp_code).first()
    if emp:
        ensure_branch_access(db, current_user, emp.branch_id)

    try:
        new_log = create_manual_attendance_log(
            emp_code    = body.emp_code,
            check_type  = body.check_type,
            timestamp_str = body.timestamp,
            note        = body.note or "",
            created_by  = current_user.full_name or current_user.email,
            created_by_id = current_user.id,
            reason      = body.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if new_log is None:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy nhân viên với mã '{body.emp_code}' hoặc tài khoản đã bị vô hiệu hoá",
        )

    return {"success": True, "message": "Đã tạo bản ghi điểm danh thủ công", "log": new_log}
