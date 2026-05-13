"""
app/api/v1/leave.py
Endpoints quản lý đơn nghỉ phép / remote.
"""

import json
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.leave import LeaveRequest, LeaveRequestDay
from app.models.user import User
from app.services.work_calendar import get_calendar_day
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
    return {
        "id":           req.id,
        "emp_code":     req.emp_code,
        "emp_name":     req.emp_name,
        "department":   req.department,
        "emp_email":    req.emp_email,
        "request_type": req.request_type,
        "dates":        req.get_dates(),
        "total_days":   req.total_days(),
        "reason":       req.reason,
        "status":       req.status,
        "submitted_at": req.submitted_at.isoformat() if req.submitted_at else None,
        "reviewed_at":  req.reviewed_at.isoformat() if req.reviewed_at else None,
        "reviewed_by":  req.reviewed_by,
        "note":         req.note,
    }


def _get_emp(emp_code: str, db: Session) -> Optional[Employee]:
    return db.query(Employee).filter_by(emp_code=emp_code, is_active=True).first()


def _find_emp_by_user(user: User, db: Session) -> Optional[Employee]:
    return db.query(Employee).filter_by(email=user.email, is_active=True).first()


# ── POST /api/leave — Gửi đơn ────────────────────────────────────

@router.post("")
def submit_leave(payload: dict, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):

    request_type = payload.get("request_type", "leave")
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

        # Kiểm tra ngày có phải ngày làm không
        cal = get_calendar_day(d, db)
        if cal["day_type"] in ("off", "holiday"):
            lbl = "ngày nghỉ" if cal["day_type"] == "off" else "ngày lễ"
            raise HTTPException(400,
                f"{d_str} là {lbl} ({cal['label'] or 'theo lịch công ty'}), "
                "không cần gửi đơn")

        if half not in (None, "am", "pm"):
            raise HTTPException(400, f"Giá trị 'half' không hợp lệ: {half}")

        validated_dates.append({"date": d_str, "half": half})

    # Giới hạn số ngày (chỉ áp dụng nhân viên/manager thường)
    if not is_admin_override and len(validated_dates) > MAX_DAYS_PER_REQUEST:
        raise HTTPException(400,
            f"Tối đa {MAX_DAYS_PER_REQUEST} ngày/đơn. "
            "Xin nhiều hơn vui lòng liên hệ trực tiếp quản lý.")

    # Kiểm tra trùng đơn
    existing = db.query(LeaveRequest).filter(
        LeaveRequest.emp_code == emp.emp_code,
        LeaveRequest.status.in_(["pending", "approved"]),
    ).all()

    existing_dates = set()
    for req in existing:
        existing_dates.update(req.date_strings())

    conflicts = [d["date"] for d in validated_dates if d["date"] in existing_dates]
    if conflicts:
        raise HTTPException(400,
            f"Đã có đơn (đang chờ duyệt hoặc đã duyệt) cho ngày: {', '.join(conflicts)}")

    # Tạo đơn
    req = LeaveRequest(
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

    return {"success": True, "request": _req_dict(req)}


# ── GET /api/leave — Danh sách đơn ──────────────────────────────

@router.get("")
def list_leaves(
    status: str = "",
    emp_code: str = "",
    request_type: str = "",
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
        if emp_code:
            q = q.filter(LeaveRequest.emp_code == emp_code)

    if status:
        q = q.filter(LeaveRequest.status == status)
    if request_type:
        q = q.filter(LeaveRequest.request_type == request_type)

    reqs = q.order_by(LeaveRequest.submitted_at.desc()).all()
    return [_req_dict(r) for r in reqs]


# ── GET /api/leave/pending-count ─────────────────────────────────

@router.get("/pending-count")
def pending_count(db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    if current_user.role == "staff":
        return {"count": 0}
    count = db.query(LeaveRequest).filter_by(status="pending").count()
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

    # Manager chỉ duyệt staff; admin duyệt tất cả kể cả manager
    if current_user.role == "manager":
        submitter = db.query(Employee).filter_by(emp_code=req.emp_code).first()
        submitter_user = db.query(User).filter_by(email=req.emp_email).first()
        if submitter_user and submitter_user.role in ("admin", "manager"):
            raise HTTPException(403, "Manager không thể duyệt đơn của admin/manager khác")

    req.status      = "approved"
    req.reviewed_at = datetime.now()
    req.reviewed_by = current_user.email
    req.note        = payload.get("note", "")
    db.commit()
    db.refresh(req)

    try:
        notify_leave_approved(req)
    except Exception as e:
        print(f"[leave] notify_approved lỗi: {e}")

    return {"success": True, "request": _req_dict(req)}


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

    return {"success": True, "request": _req_dict(req)}


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
