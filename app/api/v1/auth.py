"""
app/api/v1/auth.py
Auth endpoints: register, verify-email, login (2-step OTP), refresh, logout, me.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import (
    create_access_token, decode_access_token,
    hash_password, verify_password,
)
from app.models.user import User, RefreshToken
from app.services.auth_service import (
    _hash_token,
    consume_token, create_otp_token, create_refresh_token_db, create_verify_token,
    require_any,
    send_approval_notification, send_login_otp_email, send_verification_email,
    verify_email_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Schemas ──────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email:     EmailStr
    full_name: Optional[str] = ""
    password:  str
    role:      Optional[str] = "staff"

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Mật khẩu cần ít nhất 8 ký tự")
        if not any(c.isdigit() for c in v):
            raise ValueError("Mật khẩu cần có ít nhất 1 chữ số")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        return v if v in ("admin", "manager", "staff") else "staff"


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp:   str


class RefreshRequest(BaseModel):
    refresh_token: str


class ResendVerifyRequest(BaseModel):
    email: EmailStr


# ── Helper ───────────────────────────────────────────────────────
def _user_dict(u: User) -> dict:
    return {
        "id":                u.id,
        "email":             u.email,
        "full_name":         u.full_name,
        "role":              u.role,
        "is_active":         u.is_active,
        "is_approved":       u.is_approved,
        "is_email_verified": u.is_email_verified,
        "created_at":        u.created_at.strftime("%d/%m/%Y") if u.created_at else "",
        "last_login":        u.last_login.strftime("%d/%m/%Y %H:%M") if u.last_login else None,
    }


# ── POST /auth/register ──────────────────────────────────────────
@router.post("/register", status_code=201)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter_by(email=req.email).first():
        raise HTTPException(400, "Email này đã được đăng ký")
    user = User(
        email           = req.email,
        full_name       = req.full_name,
        hashed_password = hash_password(req.password),
        role            = req.role,
        is_active       = False,
        is_email_verified = False,
    )
    db.add(user); db.commit(); db.refresh(user)
    token = create_verify_token(user.id, db)
    send_verification_email(user.email, user.full_name, token)
    return {
        "success": True,
        "message": "Đăng ký thành công! Kiểm tra email để xác minh tài khoản.",
    }


# ── GET /auth/verify-email ───────────────────────────────────────
@router.get("/verify-email")
def verify_email(token: str = Query(...), db: Session = Depends(get_db)):
    et = verify_email_token(token, "verify_email", db)
    if not et:
        return HTMLResponse(_verify_html("error", "Link không hợp lệ hoặc đã hết hạn",
                                        "Vui lòng đăng ký lại hoặc yêu cầu gửi lại email xác minh."), 400)
    user = db.query(User).filter_by(id=et.user_id).first()
    if not user:
        return HTMLResponse(_verify_html("error", "Tài khoản không tồn tại", ""), 400)
    user.is_email_verified = True
    # is_active vẫn giữ False — chờ admin/manager phê duyệt
    consume_token(et, db)
    db.commit()
    return HTMLResponse(_verify_html("success", "Xác minh thành công!",
                                    f"Email <strong>{user.email}</strong> đã được xác minh. Tài khoản đang chờ admin/manager phê duyệt trước khi đăng nhập được."))


# ── POST /auth/login (bước 1) ────────────────────────────────────
@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(401, "Email hoặc mật khẩu không đúng")
    if not user.is_email_verified:
        raise HTTPException(403, "Email chưa được xác minh. Kiểm tra hộp thư và click link xác minh.")
    if not user.is_active:
        raise HTTPException(403, "Tài khoản đã bị khóa. Liên hệ quản trị viên.")
    if not user.is_approved:
        raise HTTPException(403, "Tài khoản chưa được duyệt. Vui lòng chờ admin/manager phê duyệt.")
    otp = create_otp_token(user.id, db)
    send_login_otp_email(user.email, user.full_name, otp)
    return {"success": True, "message": f"Mã OTP đã gửi tới {user.email}.", "step": "otp_required", "email": user.email}


# ── POST /auth/login/verify-otp (bước 2) ─────────────────────────
@router.post("/login/verify-otp")
def login_verify_otp(req: OTPVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email, is_active=True).first()
    if not user:
        raise HTTPException(401, "Tài khoản không tồn tại")
    et = verify_email_token(req.otp.strip(), "login_otp", db)
    if not et or et.user_id != user.id:
        raise HTTPException(401, "OTP không đúng hoặc đã hết hạn")
    consume_token(et, db)
    user.last_login = datetime.now()
    db.commit()
    access_token  = create_access_token(user.id, user.email, user.role)
    refresh_token = create_refresh_token_db(user.id, db)
    return {"success": True, "access_token": access_token, "refresh_token": refresh_token,
            "token_type": "bearer", "expires_in": 15 * 60, "user": _user_dict(user)}


# ── POST /auth/refresh ───────────────────────────────────────────
@router.post("/refresh")
def refresh_token(req: RefreshRequest, db: Session = Depends(get_db)):
    rt = db.query(RefreshToken).filter_by(token_hash=_hash_token(req.refresh_token), revoked=False).first()
    if not rt or rt.expires_at < datetime.utcnow():
        raise HTTPException(401, "Refresh token không hợp lệ hoặc đã hết hạn")
    user = db.query(User).filter_by(id=rt.user_id, is_active=True).first()
    if not user:
        raise HTTPException(401, "Tài khoản không tồn tại")
    rt.revoked = True; db.commit()
    new_access  = create_access_token(user.id, user.email, user.role)
    new_refresh = create_refresh_token_db(user.id, db)
    return {"success": True, "access_token": new_access, "refresh_token": new_refresh,
            "token_type": "bearer", "expires_in": 15 * 60}


# ── POST /auth/logout ────────────────────────────────────────────
@router.post("/logout")
def logout(req: RefreshRequest, db: Session = Depends(get_db)):
    rt = db.query(RefreshToken).filter_by(token_hash=_hash_token(req.refresh_token)).first()
    if rt:
        rt.revoked = True; db.commit()
    return {"success": True, "message": "Đã đăng xuất"}


# ── GET /auth/me ─────────────────────────────────────────────────
@router.get("/me")
def get_me(user: User = Depends(require_any)):
    return {"success": True, "user": _user_dict(user)}


# ── POST /auth/resend-verify ─────────────────────────────────────
@router.post("/resend-verify")
def resend_verify(req: ResendVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email).first()
    if user and not user.is_email_verified:
        token = create_verify_token(user.id, db)
        send_verification_email(user.email, user.full_name, token)
    return {"success": True, "message": "Nếu email tồn tại và chưa xác minh, bạn sẽ nhận được email mới."}


# ── HTML verify page ─────────────────────────────────────────────
def _verify_html(status: str, title: str, body: str) -> str:
    is_ok   = status == "success"
    color   = "#00d4aa" if is_ok else "#ff4d6a"
    rgb     = "0,212,170" if is_ok else "255,77,106"
    icon    = "✓" if is_ok else "✕"
    btn_txt = "Đến trang đăng nhập" if is_ok else "Về trang chủ"
    btn_url = "/auth/login-page" if is_ok else "/"
    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8"><title>{title}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{min-height:100vh;background:#060c17;display:flex;align-items:center;
  justify-content:center;font-family:sans-serif;color:#e8f0fe}}
.card{{background:#0d1626;border:1px solid #1a2f50;border-radius:20px;
  padding:52px 48px;max-width:440px;width:90%;text-align:center}}
.icon{{width:72px;height:72px;border-radius:50%;border:2px solid {color};
  background:rgba({rgb},.12);display:flex;align-items:center;justify-content:center;
  font-size:28px;color:{color};margin:0 auto 24px}}
h1{{font-size:20px;font-weight:700;margin-bottom:12px}}
p{{font-size:13px;color:#5a7a9a;line-height:1.7;margin-bottom:32px}}
p strong{{color:#e8f0fe}}
.btn{{display:inline-block;background:{color};color:#000;font-weight:700;
  font-size:13px;padding:12px 28px;border-radius:8px;text-decoration:none}}
</style></head>
<body><div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1><p>{body}</p>
  <a href="{btn_url}" class="btn">{btn_txt}</a>
</div></body></html>"""
