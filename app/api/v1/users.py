"""
app/api/v1/users.py
Quản lý tài khoản người dùng — dành cho admin và manager.

Endpoints:
  GET  /api/users                    — danh sách users (có filter pending/all)
  GET  /api/users/pending            — danh sách chờ duyệt
  POST /api/users/{id}/approve       — duyệt tài khoản
  POST /api/users/{id}/reject        — từ chối / thu hồi duyệt
  PUT  /api/users/{id}/role          — đổi role (chỉ admin)
  PUT  /api/users/{id}/active        — khóa / mở khóa tài khoản
  DELETE /api/users/{id}             — xóa tài khoản (chỉ admin)
"""

from datetime import date, datetime
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.user import User
from app.schemas.employee import STORE_ROLE_LABELS, normalize_store_role
from app.services.branch_scope import ensure_branch_access, scoped_branch_filter, selected_branch_ids
from app.services.auth_service import (
    require_admin,
    require_manager,
    send_approval_notification,
)
from app.services.employee_branch_history import transfer_employee_to_branch

router = APIRouter(prefix="/api/users", tags=["users"])
MANAGEMENT_STORE_ROLES = ("store_manager", "assistant_manager")


# ── Helper ───────────────────────────────────────────────────────
def _find_employee_for_user(db: Session, user: User) -> Employee | None:
    emp = (
        db.query(Employee)
        .filter(Employee.user_id == user.id)
        .order_by(Employee.id.asc())
        .first()
    )
    if emp:
        return emp
    email = (user.email or "").strip()
    if not email:
        return None
    return (
        db.query(Employee)
        .filter(Employee.email == email)
        .order_by(Employee.id.asc())
        .first()
    )


def _employee_summary(db: Session, emp: Employee | None) -> dict | None:
    if not emp:
        return None
    branch = db.query(Branch).filter_by(id=emp.branch_id).first() if emp.branch_id else None
    store_role = emp.store_role or "staff"
    return {
        "id": emp.id,
        "user_id": emp.user_id,
        "branch_id": emp.branch_id,
        "branch_name": branch.name if branch else "",
        "emp_code": emp.emp_code,
        "name": emp.name,
        "store_role": store_role,
        "store_role_label": STORE_ROLE_LABELS.get(store_role, store_role),
        "is_active": bool(emp.is_active),
    }


def _user_dict(u: User, db: Session | None = None) -> dict:
    emp = _find_employee_for_user(db, u) if db else None
    return {
        "id":                u.id,
        "email":             u.email,
        "full_name":         u.full_name,
        "role":              u.role,
        "is_active":         u.is_active,
        "is_email_verified": u.is_email_verified,
        "is_approved":       u.is_approved,
        "created_at":        u.created_at.strftime("%d/%m/%Y %H:%M") if u.created_at else "",
        "last_login":        u.last_login.strftime("%d/%m/%Y %H:%M") if u.last_login else None,
        "employee":          _employee_summary(db, emp) if db else None,
    }


def _scope_users_query(db: Session, current_user: User, q, branch_id: int | None = None):
    if current_user.role == "admin" and branch_id is None:
        return q
    allowed = selected_branch_ids(db, current_user, branch_id) if current_user.role == "admin" else scoped_branch_filter(db, current_user)
    if allowed is None:
        return q
    employee_user_ids = [
        row[0]
        for row in db.query(Employee.user_id)
        .filter(Employee.branch_id.in_(allowed), Employee.user_id.isnot(None))
        .all()
    ]
    employee_emails = [
        row[0]
        for row in db.query(Employee.email)
        .filter(Employee.branch_id.in_(allowed), Employee.email != "")
        .all()
    ]
    if not employee_user_ids and not employee_emails:
        return q.filter(User.id == -1)
    from sqlalchemy import or_
    return q.filter(or_(User.id.in_(employee_user_ids), User.email.in_(employee_emails)))


def _ensure_user_scope(db: Session, current_user: User, target_user: User) -> None:
    if current_user.role == "admin":
        return
    if target_user.role in ("manager", "admin"):
        raise HTTPException(403, "Không có quyền thao tác với tài khoản này")
    emp = (
        db.query(Employee)
        .filter((Employee.user_id == target_user.id) | (Employee.email == target_user.email))
        .order_by(Employee.id.asc())
        .first()
    )
    if not emp:
        raise HTTPException(403, "Tài khoản này chưa gắn với nhân viên trong cửa hàng của bạn")
    ensure_branch_access(db, current_user, emp.branch_id)


def _generate_employee_code(db: Session) -> str:
    rows = db.query(Employee.emp_code).filter(Employee.emp_code.like("NV%")).all()
    max_num = 0
    for (code,) in rows:
        match = re.fullmatch(r"NV(\d+)", (code or "").upper())
        if match:
            max_num = max(max_num, int(match.group(1)))
    for num in range(max_num + 1, max_num + 10000):
        code = f"NV{num:03d}"
        if not db.query(Employee).filter_by(emp_code=code).first():
            return code
    raise HTTPException(500, "Không thể tự tạo mã nhân viên")


def _ensure_management_slot(
    db: Session,
    branch_id: int,
    store_role: str,
    current_emp_id: int | None = None,
) -> None:
    q = db.query(Employee).filter(
        Employee.branch_id == branch_id,
        Employee.store_role == store_role,
        Employee.is_active == True,
    )
    if current_emp_id is not None:
        q = q.filter(Employee.id != current_emp_id)
    existing = q.first()
    if existing:
        label = STORE_ROLE_LABELS.get(store_role, store_role)
        raise HTTPException(400, f"Cửa hàng này đã có {label}: {existing.name} ({existing.emp_code})")


def _assign_store_manager_profile(
    db: Session,
    user: User,
    branch_id: int | None,
    store_role: str | None,
) -> Employee:
    role_value = normalize_store_role(store_role)
    if role_value not in MANAGEMENT_STORE_ROLES:
        raise HTTPException(400, "Vui lòng chọn Cửa hàng trưởng hoặc Cửa hàng phó")
    if branch_id is None:
        raise HTTPException(400, "Vui lòng chọn chi nhánh quản lý")
    branch = db.query(Branch).filter_by(id=branch_id, is_active=True).first()
    if not branch:
        raise HTTPException(404, "Chi nhánh không tồn tại hoặc đã ngừng hoạt động")

    emp = _find_employee_for_user(db, user)
    if emp and emp.user_id not in (None, user.id):
        raise HTTPException(400, "Hồ sơ nhân viên này đã gắn với tài khoản khác")

    _ensure_management_slot(db, branch.id, role_value, emp.id if emp else None)
    label = STORE_ROLE_LABELS.get(role_value, role_value)
    display_name = (user.full_name or user.email or "").strip()

    if not emp:
        emp = Employee(
            user_id=user.id,
            branch_id=branch.id,
            emp_code=_generate_employee_code(db),
            name=display_name,
            full_name=display_name,
            position=label,
            store_role=role_value,
            email=user.email,
            status="active",
            is_active=True,
        )
        db.add(emp)
        db.flush()
        return emp

    emp.user_id = user.id
    if emp.branch_id != branch.id:
        transfer_employee_to_branch(
            db,
            emp,
            branch,
            effective_date=date.today(),
            changed_by=user.email or user.full_name or "",
            note="Chuyển cửa hàng khi gán tài khoản quản lý",
        )
    emp.store_role = role_value
    emp.position = label
    emp.email = user.email
    if display_name:
        emp.name = emp.name or display_name
        emp.full_name = emp.full_name or display_name
    emp.status = "active"
    emp.is_active = True
    return emp


def _clear_store_manager_profile(db: Session, user: User) -> Employee | None:
    emp = _find_employee_for_user(db, user)
    if emp and (emp.store_role or "staff") in MANAGEMENT_STORE_ROLES:
        emp.store_role = "staff"
        emp.position = "Nhân viên"
    return emp


def _apply_role_and_management(
    db: Session,
    user: User,
    role: str,
    branch_id: int | None = None,
    store_role: str | None = None,
) -> Employee | None:
    if role not in ("staff", "manager", "admin"):
        raise HTTPException(400, "Role không hợp lệ. Chọn: staff | manager | admin")
    user.role = role
    if role == "manager":
        return _assign_store_manager_profile(db, user, branch_id, store_role)
    return _clear_store_manager_profile(db, user)


# ── Schemas ──────────────────────────────────────────────────────
class SetRoleRequest(BaseModel):
    role: str  # staff | manager | admin
    branch_id: Optional[int] = None
    store_role: Optional[str] = None

class ApproveUserRequest(BaseModel):
    role: Optional[str] = None
    branch_id: Optional[int] = None
    store_role: Optional[str] = None

class SetActiveRequest(BaseModel):
    is_active: bool


# ── GET /api/users ───────────────────────────────────────────────
@router.get("")
def list_users(
    pending_only: bool = False,
    role: Optional[str] = None,
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """
    Lấy danh sách users.
    - pending_only=true: chỉ user đã xác minh email nhưng chưa được duyệt
    - role: lọc theo role (staff | manager | admin)
    - Manager chỉ thấy được role staff; admin thấy tất cả
    """
    q = db.query(User)

    if pending_only:
        q = q.filter_by(is_email_verified=True, is_approved=False, is_active=False)

    if role:
        q = q.filter_by(role=role)

    if current_user.role == "manager":
        q = q.filter(User.role != "admin")
        q = _scope_users_query(db, current_user, q)
    elif branch_id is not None:
        q = _scope_users_query(db, current_user, q, branch_id)

    users = q.order_by(User.created_at.desc()).all()
    return {"success": True, "users": [_user_dict(u, db) for u in users], "total": len(users)}


# ── GET /api/users/pending ───────────────────────────────────────
@router.get("/pending")
def list_pending_users(
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """Shortcut: danh sách user chờ duyệt."""
    q = db.query(User).filter_by(is_email_verified=True, is_approved=False, is_active=False)

    if current_user.role == "manager":
        q = q.filter(User.role != "admin")
        q = _scope_users_query(db, current_user, q)
    elif branch_id is not None:
        q = _scope_users_query(db, current_user, q, branch_id)

    users = q.order_by(User.created_at.asc()).all()
    return {"success": True, "users": [_user_dict(u, db) for u in users], "total": len(users)}


# ── POST /api/users/{id}/approve ─────────────────────────────────
@router.post("/{user_id}/approve")
def approve_user(
    user_id: int,
    req: ApproveUserRequest | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """
    Duyệt tài khoản user.
    - Manager chỉ được duyệt role staff.
    - Admin được duyệt tất cả.
    """
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "Tài khoản không tồn tại")

    if not user.is_email_verified:
        raise HTTPException(400, "User chưa xác minh email, không thể duyệt")

    if user.is_approved:
        raise HTTPException(400, "Tài khoản đã được duyệt rồi")

    target_role = (req.role if req and req.role else user.role or "staff").strip()

    # Manager không được duyệt admin/manager khác
    if current_user.role == "manager" and (user.role in ("manager", "admin") or target_role != "staff"):
        raise HTTPException(403, "Manager chỉ được duyệt tài khoản role staff")
    _ensure_user_scope(db, current_user, user)

    if current_user.role == "admin":
        _apply_role_and_management(
            db,
            user,
            target_role,
            req.branch_id if req else None,
            req.store_role if req else None,
        )

    user.is_approved = True
    user.is_active   = True   # Kích hoạt tài khoản khi được duyệt
    db.commit()
    db.refresh(user)

    # Gửi email thông báo
    send_approval_notification(user.email, user.full_name, user.role)

    return {
        "success": True,
        "message": f"Đã duyệt tài khoản {user.email}",
        "user": _user_dict(user, db),
    }


# ── POST /api/users/{id}/reject ──────────────────────────────────
@router.post("/{user_id}/reject")
def reject_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """
    Thu hồi duyệt / từ chối tài khoản.
    Đặt is_approved=False, user sẽ không đăng nhập được.
    """
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "Tài khoản không tồn tại")

    if current_user.role == "manager" and user.role in ("manager", "admin"):
        raise HTTPException(403, "Không có quyền thao tác với tài khoản này")
    _ensure_user_scope(db, current_user, user)

    # Không được tự thu hồi chính mình
    if user.id == current_user.id:
        raise HTTPException(400, "Không thể thu hồi chính tài khoản của mình")

    user.is_approved = False
    db.commit()

    return {
        "success": True,
        "message": f"Đã thu hồi duyệt tài khoản {user.email}",
        "user": _user_dict(user, db),
    }


# ── PUT /api/users/{id}/role ─────────────────────────────────────
@router.put("/{user_id}/role")
def set_user_role(
    user_id: int,
    req: SetRoleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Đổi role user — chỉ admin."""
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "Tài khoản không tồn tại")

    if user.id == current_user.id:
        raise HTTPException(400, "Không thể tự đổi role của chính mình")

    old_role = user.role
    _apply_role_and_management(db, user, req.role, req.branch_id, req.store_role)
    db.commit()
    db.refresh(user)

    return {
        "success": True,
        "message": f"Đã đổi role {user.email}: {old_role} → {req.role}",
        "user":    _user_dict(user, db),
    }


# ── PUT /api/users/{id}/active ───────────────────────────────────
@router.put("/{user_id}/active")
def set_user_active(
    user_id: int,
    req: SetActiveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """Khóa hoặc mở khóa tài khoản."""
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "Tài khoản không tồn tại")

    if user.id == current_user.id:
        raise HTTPException(400, "Không thể tự khóa tài khoản của mình")

    if current_user.role == "manager" and user.role in ("manager", "admin"):
        raise HTTPException(403, "Không có quyền thao tác với tài khoản này")
    _ensure_user_scope(db, current_user, user)

    user.is_active = req.is_active
    db.commit()

    action = "mở khóa" if req.is_active else "khóa"
    return {
        "success": True,
        "message": f"Đã {action} tài khoản {user.email}",
        "user":    _user_dict(user, db),
    }


# ── DELETE /api/users/{id} ───────────────────────────────────────
@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Xóa hẳn tài khoản — chỉ admin."""
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "Tài khoản không tồn tại")

    if user.id == current_user.id:
        raise HTTPException(400, "Không thể tự xóa tài khoản của mình")

    db.delete(user)
    db.commit()

    return {"success": True, "message": f"Đã xóa tài khoản {user.email}"}
