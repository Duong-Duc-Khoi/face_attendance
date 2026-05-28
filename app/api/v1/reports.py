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
from sqlalchemy.orm import Session
from typing import Optional

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.services.branch_scope import ensure_branch_access, scoped_branch_filter
from app.models.attendance import (
    AttendanceAuditFinding,
    AttendanceEvent,
    AttendanceEvidence,
    AttendanceLog,
    AttendanceSession,
)
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.schemas.employee import JOB_ROLE_LABELS, normalize_job_role
from app.services.attendance import (
    get_logs_by_date, get_summary_today,
    get_log_by_id, update_attendance_log,
    delete_attendance_log, create_manual_attendance_log,
)
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
    emp = None
    if log.employee_id:
        emp = db.query(Employee).filter_by(id=log.employee_id).first()
    if not emp and log.emp_code:
        emp = db.query(Employee).filter_by(emp_code=log.emp_code).first()
    return emp.branch_id if emp else None


def _ensure_log_scope(db: Session, current_user, log: AttendanceLog) -> None:
    ensure_branch_access(db, current_user, _employee_branch_for_log(db, log))


def _ensure_session_scope(db: Session, current_user, session: AttendanceSession) -> None:
    ensure_branch_access(db, current_user, session.branch_id)


def _filter_logs_for_scope(db: Session, current_user, logs: list[AttendanceLog]) -> list[AttendanceLog]:
    if not current_user or current_user.role == "admin":
        return logs
    allowed = scoped_branch_filter(db, current_user)
    scoped = []
    for log in logs:
        if _employee_branch_for_log(db, log) in allowed:
            scoped.append(log)
    return scoped


def _filter_log_dicts_for_scope(db: Session, current_user, logs: list[dict]) -> list[dict]:
    if not current_user or current_user.role == "admin":
        return logs
    allowed = scoped_branch_filter(db, current_user)
    return [row for row in logs if row.get("branch_id") in allowed]


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
    branch_ids = {e.branch_id for e in employees if e.branch_id}
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


def _branch_label_for_log(emp: Employee | None, branches: dict[int, str]) -> str:
    if emp and emp.branch_id:
        return branches.get(emp.branch_id, f"Chi nhánh #{emp.branch_id}")
    return "Chưa xác định"


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
                   db: Session = Depends(get_db),
                   current_user=Depends(_optional_user)):
    if date:
        logs = []
        start_date = datetime.strptime(date, "%Y-%m-%d")
        for i in range(max(1, days)):
            d = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            logs.extend(get_logs_by_date(d, emp_code))
    else:
        logs = []
        for i in range(days):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            logs.extend(get_logs_by_date(d, emp_code))
    logs.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    logs = _filter_log_dicts_for_scope(db, current_user, logs)
    return {"logs": logs, "total": len(logs)}


@router.get("/summary")
def summary_today(current_user=Depends(get_current_user)):
    return get_summary_today()


@router.get("/summary/range")
def summary_range(from_date: str, to_date: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0,  minute=0)
    end   = datetime.strptime(to_date,   "%Y-%m-%d").replace(hour=23, minute=59)
    logs  = db.query(AttendanceLog).filter(
        AttendanceLog.timestamp >= start,
        AttendanceLog.timestamp <= end,
    ).all()
    logs = _filter_logs_for_scope(db, current_user, logs)
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
    allowed = scoped_branch_filter(db, current_user) if current_user.role != "admin" else None
    if allowed is not None:
        assignments = [a for a in assignments if a.branch_id in allowed]
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
        branch = _branch_label_for_log(emp, branches)
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


@router.get("/reports/export")
def export_excel(from_date: str, to_date: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return JSONResponse({"error": "Cài openpyxl: pip install openpyxl"}, status_code=500)

    start = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0)
    end   = datetime.strptime(to_date,   "%Y-%m-%d").replace(hour=23, minute=59)
    logs  = db.query(AttendanceLog).filter(
        AttendanceLog.timestamp >= start,
        AttendanceLog.timestamp <= end,
    ).order_by(AttendanceLog.timestamp).all()
    logs = _filter_logs_for_scope(db, current_user, logs)
    emp_by_id, emp_by_code, branches = _employee_report_context(logs, db)

    wb = Workbook()
    ws = wb.active
    ws.title = "Báo cáo chấm công"

    ws.merge_cells("A1:H1")
    ws["A1"]           = f"BÁO CÁO CHẤM CÔNG  —  {from_date} đến {to_date}"
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
        branch_label = _branch_label_for_log(emp, branches)
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

    settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = str(settings.EXPORTS_DIR / f"chamcong_{from_date}_{to_date}.xlsx")
    wb.save(filepath)

    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"BaoCaoChamCong_{from_date}_{to_date}.xlsx",
    )


# ── GET /api/reports/employee/{emp_code}/stats ───────────────────

@router.get("/reports/employee/{emp_code}/stats")
def employee_stats(
    emp_code: str,
    year: int = 0,
    month: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(_optional_user),
):
    from app.services.work_calendar import get_employee_stats, get_employee_stats_month
    from datetime import date as _date
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


class AttendanceCreateRequest(BaseModel):
    emp_code:   str
    check_type: str                       # "check_in" | "check_out"
    timestamp:  str                       # "YYYY-MM-DD HH:MM:SS" hoặc ISO
    note:       Optional[str] = ""


class AuditRunRequest(BaseModel):
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


def _session_review_to_dict(session: AttendanceSession, db: Session) -> dict:
    emp = db.query(Employee).filter_by(id=session.employee_id).first()
    shift = db.query(Shift).filter_by(id=session.shift_id).first() if session.shift_id else None
    branch = db.query(Branch).filter_by(id=session.branch_id).first() if session.branch_id else None
    return {
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
        "shift_id": session.shift_id,
        "shift_name": shift.name if shift else "",
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


# ── GET /api/attendance/sessions/{session_id}/events ─────────────

@router.get("/attendance/sessions/{session_id}/events")
def get_attendance_session_events(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Lấy các event/bằng chứng ảnh của một phiên làm việc."""
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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    q = db.query(AttendanceSession)
    allowed = scoped_branch_filter(db, current_user) if current_user.role != "admin" else None
    if allowed is not None:
        q = q.filter(AttendanceSession.branch_id.in_(allowed))
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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    rows = (
        db.query(AttendanceSession.review_type, AttendanceSession.id)
          .filter(AttendanceSession.review_status == "pending_review")
          .all()
    )
    if current_user.role != "admin":
        allowed = scoped_branch_filter(db, current_user)
        rows = (
            db.query(AttendanceSession.review_type, AttendanceSession.id)
              .filter(AttendanceSession.review_status == "pending_review", AttendanceSession.branch_id.in_(allowed))
              .all()
        )
    counts = {"total": len(rows), "absent": 0, "missing_checkout": 0, "overtime": 0}
    for review_type, _id in rows:
        if review_type in counts:
            counts[review_type] += 1
    return counts


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
    session.review_status = body.review_status
    session.review_note = body.note or ""
    session.reviewed_by = current_user.full_name or current_user.email
    session.reviewed_at = datetime.now() if body.review_status != "pending_review" else None
    session.updated_by_id = current_user.id
    db.commit()
    db.refresh(session)
    return {"success": True, "session": _session_review_to_dict(session, db)}


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
    _require_manager_or_admin(current_user)
    if current_user.role != "admin" and body.emp_code:
        emp = db.query(Employee).filter_by(emp_code=body.emp_code).first()
        if emp:
            ensure_branch_access(db, current_user, emp.branch_id)
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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_manager_or_admin(current_user)
    if current_user.role != "admin" and emp_code:
        emp = db.query(Employee).filter_by(emp_code=emp_code).first()
        if emp:
            ensure_branch_access(db, current_user, emp.branch_id)
    providers = list_ai_provider_settings(db)
    vision_ready = any(p.get("configured") and p.get("is_enabled") for p in providers)
    return {
        "findings": list_audit_findings(db, date, review_status, risk_level, limit, from_date, to_date, emp_code),
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
        deleted = delete_attendance_log(log_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")

    return {"success": True, "message": f"Đã xoá bản ghi #{log_id}"}


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
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if new_log is None:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy nhân viên với mã '{body.emp_code}' hoặc tài khoản đã bị vô hiệu hoá",
        )

    return {"success": True, "message": "Đã tạo bản ghi điểm danh thủ công", "log": new_log}
