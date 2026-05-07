"""
app/core/security.py
Các hàm bảo mật thuần túy: hash password, JWT, phân quyền.
KHÔNG chứa logic gửi email, không gọi DB trực tiếp.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db

_bearer = HTTPBearer(auto_error=False)

# ── Roles & Permissions ──────────────────────────────────────────
ROLES = ("staff", "manager", "admin")

PERMISSIONS: dict[str, set[str]] = {
    "attendance:read_own":  {"staff", "manager", "admin"},
    "attendance:read_all":  {"manager", "admin"},
    "attendance:write":     {"admin"},

    "face:register_own":    {"staff", "manager", "admin"},
    "face:register_any":    {"manager", "admin"},

    "employee:read":        {"manager", "admin"},
    "employee:write":       {"manager", "admin"},
    "employee:delete":      {"admin"},

    "profile:write":        {"staff", "manager", "admin"},

    "user:read":            {"manager", "admin"},
    "user:set_role":        {"admin"},
    "user:approve":         {"manager", "admin"},
    "user:delete":          {"admin"},

    "report:read":          {"manager", "admin"},
    "report:export":        {"manager", "admin"},

    "system:config":        {"admin"},
}


def has_permission(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


# ── Password ─────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── Token helpers ────────────────────────────────────────────────
def hash_token(token: str) -> str:
    """SHA-256 hash — lưu DB thay vì plain token."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_otp(length: int = 6) -> str:
    return "".join([str(secrets.randbelow(10)) for _ in range(length)])


# ── JWT ──────────────────────────────────────────────────────────
def create_access_token(user_id: int, email: str, role: str) -> str:
    payload = {
        "sub":   str(user_id),
        "email": email,
        "role":  role,
        "exp":   datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXP),
        "type":  "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token đã hết hạn")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token không hợp lệ")


# ── FastAPI dependencies ─────────────────────────────────────────
def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
):
    """Dependency: parse JWT → trả về User object."""
    from app.models.user import User

    if creds is None:
        raise HTTPException(status_code=401, detail="Cần đăng nhập")

    payload = decode_access_token(creds.credentials)
    user = db.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
    if not user:
        raise HTTPException(status_code=401, detail="Tài khoản không tồn tại hoặc đã bị khóa")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Tài khoản chưa được duyệt. Vui lòng chờ admin/manager phê duyệt.")
    return user


def require_permission(permission: str):
    """Dependency factory: kiểm tra quyền."""
    def _check(user=Depends(get_current_user)):
        if not has_permission(user.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Không có quyền: {permission}",
            )
        return user
    return _check
