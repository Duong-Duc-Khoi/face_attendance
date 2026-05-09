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

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.user import User
from app.services.auth_service import (
    require_admin,
    require_manager,
    send_approval_notification,
)

router = APIRouter(prefix="/api/users", tags=["users"])


# ── Helper ───────────────────────────────────────────────────────
def _user_dict(u: User) -> dict:
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
    }


# ── Schemas ──────────────────────────────────────────────────────
class SetRoleRequest(BaseModel):
    role: str  # staff | manager | admin

class SetActiveRequest(BaseModel):
    is_active: bool


# ── GET /api/users ───────────────────────────────────────────────
@router.get("")
def list_users(
    pending_only: bool = False,
    role: Optional[str] = None,
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

    # Manager không được thấy danh sách admin
    if current_user.role == "manager":
        q = q.filter(User.role != "admin")

    users = q.order_by(User.created_at.desc()).all()
    return {"success": True, "users": [_user_dict(u) for u in users], "total": len(users)}


# ── GET /api/users/pending ───────────────────────────────────────
@router.get("/pending")
def list_pending_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """Shortcut: danh sách user chờ duyệt."""
    q = db.query(User).filter_by(is_email_verified=True, is_approved=False, is_active=False)

    if current_user.role == "manager":
        q = q.filter(User.role != "admin")

    users = q.order_by(User.created_at.asc()).all()
    return {"success": True, "users": [_user_dict(u) for u in users], "total": len(users)}


# ── POST /api/users/{id}/approve ─────────────────────────────────
@router.post("/{user_id}/approve")
def approve_user(
    user_id: int,
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

    # Manager không được duyệt admin/manager khác
    if current_user.role == "manager" and user.role in ("manager", "admin"):
        raise HTTPException(403, "Manager chỉ được duyệt tài khoản role staff")

    user.is_approved = True
    user.is_active   = True   # Kích hoạt tài khoản khi được duyệt
    db.commit()

    # Gửi email thông báo
    send_approval_notification(user.email, user.full_name, user.role)

    return {
        "success": True,
        "message": f"Đã duyệt tài khoản {user.email}",
        "user": _user_dict(user),
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

    # Không được tự thu hồi chính mình
    if user.id == current_user.id:
        raise HTTPException(400, "Không thể thu hồi chính tài khoản của mình")

    user.is_approved = False
    db.commit()

    return {
        "success": True,
        "message": f"Đã thu hồi duyệt tài khoản {user.email}",
        "user": _user_dict(user),
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
    if req.role not in ("staff", "manager", "admin"):
        raise HTTPException(400, "Role không hợp lệ. Chọn: staff | manager | admin")

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "Tài khoản không tồn tại")

    if user.id == current_user.id:
        raise HTTPException(400, "Không thể tự đổi role của chính mình")

    old_role   = user.role
    user.role  = req.role
    db.commit()

    return {
        "success": True,
        "message": f"Đã đổi role {user.email}: {old_role} → {req.role}",
        "user":    _user_dict(user),
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

    user.is_active = req.is_active
    db.commit()

    action = "mở khóa" if req.is_active else "khóa"
    return {
        "success": True,
        "message": f"Đã {action} tài khoản {user.email}",
        "user":    _user_dict(user),
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
