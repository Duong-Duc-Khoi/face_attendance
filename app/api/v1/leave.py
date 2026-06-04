"""
app/api/v1/leave.py
Endpoints quản lý đơn nghỉ phép.
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.leave import LeaveRequest, LeaveRequestDay
from app.models.shift import ShiftAssignment
from app.models.user import User
from app.services.branch_scope import BRANCH_MANAGER_STORE_ROLES, ensure_branch_access, selected_branch_ids
from app.services.leave_policy import (
    apply_approved_leave_to_assignments,
    assignment_ids_from_entry,
    leave_request_scope,
    leave_scope_label,
    request_assignment_labels,
    validate_leave_conflicts,
)
from app.services.notify import (
    notify_leave_submitted,
    notify_leave_approved,
    notify_leave_rejected,
    notify_leave_cancelled,
)

router = APIRouter(prefix="/api/leave", tags=["leave"])

MAX_DAYS_PER_REQUEST = 3   # Nhân viên tối đa 3 ngày/đơn


# ── Helpers ──────────────────────────────────────────────────────

def _req_dict(req: LeaveRequest) -> dict:
    scope = leave_request_scope(req)
    return {
        "id":           req.id,
        "emp_code":     req.emp_code,
        "emp_name":     req.emp_name,
        "department":   req.department,
        "emp_email":    req.emp_email,
        "request_type": req.request_type,
        "dates":        req.get_dates(),
        "leave_scope":  scope,
        "scope_label":  leave_scope_label(scope),
        "assignment_labels": [],
        "total_days":   req.total_days(),
        "reason":       req.reason,
        "status":       req.status,
        "submitted_at": req.submitted_at.isoformat() if req.submitted_at else None,
        "reviewed_at":  req.reviewed_at.isoformat() if req.reviewed_at else None,
        "reviewed_by":  req.reviewed_by,
        "note":         req.note,
    }


def _req_dict_with_db(db: Session, req: LeaveRequest) -> dict:
    data = _req_dict(req)
    data["assignment_labels"] = request_assignment_labels(db, req)
    return data


def _get_emp(emp_code: str, db: Session) -> Optional[Employee]:
    return db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()


def _find_emp_by_user(user: User, db: Session) -> Optional[Employee]:
    emp = db.query(Employee).filter_by(user_id=user.id, is_active=True).first()
    if emp:
        return emp
    return db.query(Employee).filter_by(email=user.email, is_active=True).first()


def _scope_leave_query(db: Session, current_user: User, q, branch_id: int | None = None):
    if current_user.role == "staff":
        emp = _find_emp_by_user(current_user, db)
        if not emp:
            return q.filter(LeaveRequest.id == -1)
        return q.filter(LeaveRequest.emp_code == emp.emp_code)
    allowed = selected_branch_ids(db, current_user, branch_id)
    if allowed is None:
        return q
    emp_codes = [row[0] for row in db.query(Employee.emp_code).filter(Employee.branch_id.in_(allowed)).all()]
    if not emp_codes:
        return q.filter(LeaveRequest.id == -1)
    return q.filter(LeaveRequest.emp_code.in_(emp_codes))


def _ensure_leave_scope(db: Session, current_user: User, req: LeaveRequest) -> None:
    if current_user.role == "admin":
        return
    if current_user.role == "staff":
        emp = _find_emp_by_user(current_user, db)
        if not emp or emp.emp_code != req.emp_code:
            raise HTTPException(403, "Bạn chỉ được thao tác đơn của mình")
        return
    emp = db.query(Employee).filter_by(emp_code=req.emp_code).first()
    if not emp:
        raise HTTPException(404, "Không tìm thấy nhân viên trong đơn")
    ensure_branch_access(db, current_user, emp.branch_id)


def _ensure_manager_can_review(db: Session, current_user: User, req: LeaveRequest, action: str) -> None:
    if current_user.role != "manager":
        return
    submitter = db.query(Employee).filter_by(emp_code=req.emp_code).first()
    submitter_user = db.query(User).filter_by(email=req.emp_email).first()
    is_manager_account = bool(submitter_user and submitter_user.role in ("admin", "manager"))
    is_branch_lead = bool(submitter and submitter.store_role in BRANCH_MANAGER_STORE_ROLES)
    if is_manager_account or is_branch_lead:
        raise HTTPException(403, f"Cửa hàng trưởng/phó không thể {action} đơn của quản lý khác")


def _validate_leave_assignment_ids(
    db: Session,
    emp: Employee,
    work_date: date,
    assignment_ids: list[int],
) -> list[int]:
    if not assignment_ids:
        raise HTTPException(400, "Vui lòng chọn ít nhất một ca cần nghỉ")
    rows = (
        db.query(ShiftAssignment)
        .filter(
            ShiftAssignment.id.in_(assignment_ids),
            ShiftAssignment.emp_code == emp.emp_code,
            ShiftAssignment.work_date == work_date,
            ShiftAssignment.status != "cancelled",
        )
        .all()
    )
    found = {row.id for row in rows}
    missing = [str(assignment_id) for assignment_id in assignment_ids if assignment_id not in found]
    if missing:
        raise HTTPException(400, "Có ca không hợp lệ hoặc không thuộc lịch của bạn: " + ", ".join(missing))
    return sorted(found)


# ── POST /api/leave — Gửi đơn ────────────────────────────────────

@router.post("")
def submit_leave(payload: dict, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):

    request_type = payload.get("request_type", "leave")
    if request_type != "leave":
        raise HTTPException(400, "Hiện chỉ hỗ trợ đơn nghỉ phép, không còn tạo đơn làm từ xa")
    leave_scope = payload.get("leave_scope", "day")
    if leave_scope not in ("day", "shift"):
        raise HTTPException(400, "leave_scope phải là 'day' hoặc 'shift'")
    dates_raw    = payload.get("dates", [])   # [{"date":"2026-05-12","half":null}, ...]
    reason       = payload.get("reason", "").strip()

    if not dates_raw:
        raise HTTPException(400, "Vui lòng chọn ít nhất 1 ngày")

    # Admin có thể gửi đơn hộ người khác (override emp_code)
    target_emp_code = payload.get("emp_code")
    is_admin_override = (current_user.role == "admin" and target_emp_code)

    if is_admin_override:
        emp = _get_emp(target_emp_code, db)
        if not emp:
            raise HTTPException(404, "Không tìm thấy nhân viên")
    else:
        emp = _find_emp_by_user(current_user, db)
        if not emp:
            raise HTTPException(404, "Tài khoản chưa được liên kết với nhân viên")

    # Validate ngày
    today = date.today()
    min_date = today + timedelta(days=1)   # tối thiểu ngày mai

    validated_dates = []
    for entry in dates_raw:
        d_str = entry.get("date", "")
        half  = entry.get("half")   # None | "am" | "pm"
        try:
            d = date.fromisoformat(d_str)
        except Exception:
            raise HTTPException(400, f"Ngày không hợp lệ: {d_str}")

        # Admin override: cho phép nhập ngày đã qua (đánh dấu hộ)
        if not is_admin_override and d < min_date:
            raise HTTPException(400,
                f"Chỉ được gửi đơn từ ngày {min_date.isoformat()} trở đi "
                f"(tối thiểu 1 ngày trước)")

        if half not in (None, "am", "pm"):
            raise HTTPException(400, f"Giá trị 'half' không hợp lệ: {half}")

        normalized = {"date": d_str, "half": half, "scope": leave_scope}
        if leave_scope == "shift":
            assignment_ids = _validate_leave_assignment_ids(
                db,
                emp,
                d,
                assignment_ids_from_entry(entry),
            )
            normalized["assignment_ids"] = assignment_ids
        else:
            normalized["assignment_ids"] = []
        validated_dates.append(normalized)

    # Giới hạn số ngày (chỉ áp dụng nhân viên/manager thường)
    if not is_admin_override and len(validated_dates) > MAX_DAYS_PER_REQUEST:
        raise HTTPException(400,
            f"Tối đa {MAX_DAYS_PER_REQUEST} ngày/đơn. "
            "Xin nhiều hơn vui lòng liên hệ trực tiếp quản lý.")

    try:
        validate_leave_conflicts(db, emp.emp_code, validated_dates, leave_scope=leave_scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Tạo đơn
    req = LeaveRequest(
        employee_id  = emp.id,
        emp_code     = emp.emp_code,
        emp_name     = emp.name,
        department   = emp.department or "",
        emp_email    = emp.email or "",
        request_type = request_type,
        reason       = reason,
        status       = "approved" if is_admin_override else "pending",
        submitted_at = datetime.now(),
        reviewed_at  = datetime.now() if is_admin_override else None,
        reviewed_by  = current_user.email if is_admin_override else None,
    )
    req.set_dates(validated_dates)
    db.add(req)
    db.commit()
    db.refresh(req)
    for day in validated_dates:
        db.add(LeaveRequestDay(
            leave_request_id=req.id,
            date=date.fromisoformat(day["date"]),
            half_day=day.get("half"),
        ))
    db.commit()

    # Notify
    if not is_admin_override:
        try:
            notify_leave_submitted(req)
        except Exception as e:
            print(f"[leave] notify_submitted lỗi: {e}")

    if is_admin_override:
        apply_approved_leave_to_assignments(db, req, current_user.email)
        db.commit()

    return {"success": True, "request": _req_dict_with_db(db, req)}


# ── GET /api/leave — Danh sách đơn ──────────────────────────────

@router.get("")
def list_leaves(
    status: str = "",
    emp_code: str = "",
    request_type: str = "",
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(LeaveRequest)

    # Staff chỉ xem của mình
    if current_user.role == "staff":
        emp = _find_emp_by_user(current_user, db)
        if emp:
            q = q.filter(LeaveRequest.emp_code == emp.emp_code)
        else:
            return []

    # Manager xem tất cả (trừ đơn của admin khác)
    else:
        q = _scope_leave_query(db, current_user, q, branch_id)
        if emp_code:
            q = q.filter(LeaveRequest.emp_code == emp_code)

    if status:
        q = q.filter(LeaveRequest.status == status)
    if request_type:
        q = q.filter(LeaveRequest.request_type == request_type)

    reqs = q.order_by(LeaveRequest.submitted_at.desc()).all()
    return [_req_dict_with_db(db, r) for r in reqs]


# ── GET /api/leave/pending-count ─────────────────────────────────

@router.get("/pending-count")
def pending_count(db: Session = Depends(get_db),
                  branch_id: int | None = None,
                  current_user: User = Depends(get_current_user)):
    if current_user.role == "staff":
        return {"count": 0}
    q = _scope_leave_query(db, current_user, db.query(LeaveRequest), branch_id)
    count = q.filter_by(status="pending").count()
    return {"count": count}


# ── PUT /api/leave/{id}/approve ─────────────────────────────────

@router.put("/{req_id}/approve")
def approve_leave(req_id: int, payload: dict = {},
                  db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):

    req = db.query(LeaveRequest).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(404, "Không tìm thấy đơn")
    if req.status != "pending":
        raise HTTPException(400, f"Đơn đang ở trạng thái '{req.status}', không thể duyệt")
    _ensure_leave_scope(db, current_user, req)

    _ensure_manager_can_review(db, current_user, req, "duyệt")

    req.status      = "approved"
    req.reviewed_at = datetime.now()
    req.reviewed_by = current_user.email
    req.note        = payload.get("note", "")
    apply_approved_leave_to_assignments(db, req, current_user.email)
    db.commit()
    db.refresh(req)

    try:
        notify_leave_approved(req)
    except Exception as e:
        print(f"[leave] notify_approved lỗi: {e}")

    return {"success": True, "request": _req_dict_with_db(db, req)}


# ── PUT /api/leave/{id}/reject ──────────────────────────────────

@router.put("/{req_id}/reject")
def reject_leave(req_id: int, payload: dict = {},
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):

    req = db.query(LeaveRequest).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(404, "Không tìm thấy đơn")
    if req.status != "pending":
        raise HTTPException(400, f"Đơn đang ở trạng thái '{req.status}'")
    _ensure_leave_scope(db, current_user, req)
    _ensure_manager_can_review(db, current_user, req, "từ chối")

    note = payload.get("note", "").strip()
    if not note:
        raise HTTPException(400, "Vui lòng ghi lý do từ chối")

    req.status      = "rejected"
    req.reviewed_at = datetime.now()
    req.reviewed_by = current_user.email
    req.note        = note
    db.commit()
    db.refresh(req)

    try:
        notify_leave_rejected(req)
    except Exception as e:
        print(f"[leave] notify_rejected lỗi: {e}")

    return {"success": True, "request": _req_dict_with_db(db, req)}


# ── DELETE /api/leave/{id} — Nhân viên hủy đơn ─────────────────

@router.delete("/{req_id}")
def cancel_leave(req_id: int,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):

    req = db.query(LeaveRequest).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(404, "Không tìm thấy đơn")

    # Chỉ người gửi hoặc admin mới hủy được
    emp = _find_emp_by_user(current_user, db)
    is_owner = emp and emp.emp_code == req.emp_code
    if not is_owner and current_user.role != "admin":
        raise HTTPException(403, "Không có quyền hủy đơn này")

    if req.status not in ("pending",):
        raise HTTPException(400, "Chỉ có thể hủy đơn đang chờ duyệt")

    req.status = "cancelled"
    db.commit()

    if getattr(__import__('app.core.config', fromlist=['settings']).settings,
               'NOTIFY_LEAVE_CANCEL', True):
        try:
            notify_leave_cancelled(req, current_user.email)
        except Exception as e:
            print(f"[leave] notify_cancelled lỗi: {e}")

    return {"success": True}
