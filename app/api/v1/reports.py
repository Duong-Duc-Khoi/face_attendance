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
from app.models.attendance import AttendanceAuditFinding, AttendanceEvent, AttendanceEvidence, AttendanceLog
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

    by_date: dict[str, dict] = {}
    for log in logs:
        day = log.timestamp.strftime("%Y-%m-%d")
        if day not in by_date:
            by_date[day] = {"check_in": set(), "check_out": set()}
        by_date[day][log.check_type].add(log.emp_code)

    dept_stats: dict[str, int] = {}
    for log in logs:
        dept = log.department or "Chưa xác định"
        dept_stats[dept] = dept_stats.get(dept, 0) + 1

    return {
        "from_date":  from_date,
        "to_date":    to_date,
        "total_logs": len(logs),
        "by_date": [
            {"date": d, "checked_in": len(v["check_in"]), "checked_out": len(v["check_out"])}
            for d, v in sorted(by_date.items())
        ],
        "by_dept": [{"dept": k, "count": v} for k, v in dept_stats.items()],
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

    wb = Workbook()
    ws = wb.active
    ws.title = "Báo cáo chấm công"

    ws.merge_cells("A1:G1")
    ws["A1"]           = f"BÁO CÁO CHẤM CÔNG  —  {from_date} đến {to_date}"
    ws["A1"].font      = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")

    thin   = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1A365D")
    headers  = ["STT", "Mã NV", "Họ tên", "Phòng ban", "Loại", "Thời gian", "Trạng thái"]

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = hdr_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border    = border

    for i, log in enumerate(logs, 1):
        fill_color = "F0FFF4" if log.check_type == "check_in" else "EBF4FF"
        row_fill   = PatternFill("solid", fgColor=fill_color)
        for col, val in enumerate([
            i, log.emp_code, log.emp_name, log.department,
            "Vào" if log.check_type == "check_in" else "Ra",
            log.timestamp.strftime("%H:%M:%S  %d/%m/%Y"),
            log.note or "",
        ], 1):
            cell = ws.cell(row=i + 2, column=col, value=val)
            cell.alignment = Alignment(horizontal="center")
            cell.border    = border
            cell.fill      = row_fill

    for col, w in enumerate([6, 10, 22, 18, 8, 24, 20], 1):
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
    current_user=Depends(get_current_user),
):
    """Lấy thông tin 1 bản ghi điểm danh theo ID."""
    log = get_log_by_id(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi điểm danh")
    return log


# ── PUT /api/attendance/{log_id} ─────────────────────────────────

@router.put("/attendance/{log_id}")
def edit_attendance_log(
    log_id: int,
    body: AttendanceUpdateRequest,
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
