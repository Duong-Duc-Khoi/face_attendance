"""
app/routes/auth.py
Tất cả endpoints xác thực:
  POST /auth/register          — đăng ký tài khoản
  GET  /auth/verify-email      — xác minh email qua link
  POST /auth/login             — đăng nhập (bước 1: gửi OTP)
  POST /auth/login/verify-otp  — đăng nhập (bước 2: xác nhận OTP → cấp JWT)
  POST /auth/refresh           — làm mới access token
  POST /auth/logout            — thu hồi refresh token
  GET  /auth/me                — lấy thông tin user hiện tại
  POST /auth/resend-verify     — gửi lại email xác minh
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import (
    User, EmailToken, RefreshToken,
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_access_token,
    create_verify_token, create_otp_token,
    verify_email_token, consume_token,
    send_verification_email, send_login_otp_email,
    get_current_user, _hash_token,
    REFRESH_TOKEN_EXP,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ──────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────
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
        if v not in ("admin", "manager", "staff"):
            return "staff"
        return v


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


# ──────────────────────────────────────────
# Helper
# ──────────────────────────────────────────
def _user_dict(u: User) -> dict:
    return {
        "id":                u.id,
        "email":             u.email,
        "full_name":         u.full_name,
        "role":              u.role,
        "is_active":         u.is_active,
        "is_email_verified": u.is_email_verified,
        "created_at":        u.created_at.strftime("%d/%m/%Y") if u.created_at else "",
        "last_login":        u.last_login.strftime("%d/%m/%Y %H:%M") if u.last_login else None,
    }


# ══════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════
@router.post("/register", status_code=201)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter_by(email=req.email).first():
        raise HTTPException(400, "Email đã được sử dụng")

    user = User(
        email             = req.email,
        full_name         = req.full_name or "",
        hashed_password   = hash_password(req.password),
        role              = req.role,
        is_active         = False,
        is_email_verified = False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_verify_token(user.id, db)
    send_verification_email(user.email, user.full_name, token)

    return {
        "success": True,
        "message": f"Đăng ký thành công! Vui lòng kiểm tra email {user.email} để xác minh tài khoản.",
        "user_id": user.id,
    }


# ══════════════════════════════════════════
# GET /auth/verify-email?token=...
# ══════════════════════════════════════════
@router.get("/verify-email")
def verify_email(token: str = Query(...), db: Session = Depends(get_db)):
    et = verify_email_token(token, "verify_email", db)
    if not et:
        return HTMLResponse(_verify_html(
            "error",
            "Link xác minh không hợp lệ hoặc đã hết hạn",
            "Vui lòng đăng ký lại hoặc yêu cầu gửi lại email xác minh.",
        ), status_code=400)

    user = db.query(User).filter_by(id=et.user_id).first()
    if not user:
        return HTMLResponse(_verify_html("error", "Tài khoản không tồn tại", ""), status_code=400)

    user.is_email_verified = True
    user.is_active         = True
    consume_token(et, db)
    db.commit()

    return HTMLResponse(_verify_html(
        "success",
        "Xác minh thành công!",
        f"Email <strong>{user.email}</strong> đã được xác minh. Bạn có thể đăng nhập ngay bây giờ.",
    ))


# ══════════════════════════════════════════
# POST /auth/login  (bước 1)
# ══════════════════════════════════════════
@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email).first()

    # Luôn verify dù user không tồn tại để chống timing attack
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(401, "Email hoặc mật khẩu không đúng")

    if not user.is_email_verified:
        raise HTTPException(403, "Email chưa được xác minh. Kiểm tra hộp thư và click link xác minh.")

    if not user.is_active:
        raise HTTPException(403, "Tài khoản đã bị khóa. Liên hệ quản trị viên.")

    otp = create_otp_token(user.id, db)
    send_login_otp_email(user.email, user.full_name, otp)

    return {
        "success": True,
        "message": f"Mã OTP đã gửi tới {user.email}. Có hiệu lực trong 10 phút.",
        "step":    "otp_required",
        "email":   user.email,
    }


# ══════════════════════════════════════════
# POST /auth/login/verify-otp  (bước 2)
# ══════════════════════════════════════════
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

    access_token  = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id, db)

    return {
        "success":       True,
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "token_type":    "bearer",
        "expires_in":    15 * 60,
        "user":          _user_dict(user),
    }


# ══════════════════════════════════════════
# POST /auth/refresh
# ══════════════════════════════════════════
@router.post("/refresh")
def refresh_token(req: RefreshRequest, db: Session = Depends(get_db)):
    token_hash = _hash_token(req.refresh_token)
    rt = db.query(RefreshToken).filter_by(
        token_hash=token_hash, revoked=False
    ).first()

    if not rt or rt.expires_at < datetime.utcnow():
        raise HTTPException(401, "Refresh token không hợp lệ hoặc đã hết hạn")

    user = db.query(User).filter_by(id=rt.user_id, is_active=True).first()
    if not user:
        raise HTTPException(401, "Tài khoản không tồn tại")

    rt.revoked = True
    db.commit()

    new_access  = create_access_token(user.id, user.role)
    new_refresh = create_refresh_token(user.id, db)

    return {
        "success":       True,
        "access_token":  new_access,
        "refresh_token": new_refresh,
        "token_type":    "bearer",
        "expires_in":    15 * 60,
    }


# ══════════════════════════════════════════
# POST /auth/logout
# ══════════════════════════════════════════
@router.post("/logout")
def logout(req: RefreshRequest, db: Session = Depends(get_db)):
    token_hash = _hash_token(req.refresh_token)
    rt = db.query(RefreshToken).filter_by(token_hash=token_hash).first()
    if rt:
        rt.revoked = True
        db.commit()
    return {"success": True, "message": "Đã đăng xuất"}


# ══════════════════════════════════════════
# GET /auth/me
# ══════════════════════════════════════════
@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {"success": True, "user": _user_dict(current_user)}


# ══════════════════════════════════════════
# POST /auth/resend-verify
# ══════════════════════════════════════════
@router.post("/resend-verify")
def resend_verify(req: ResendVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email).first()
    if user and not user.is_email_verified:
        token = create_verify_token(user.id, db)
        send_verification_email(user.email, user.full_name, token)
    return {
        "success": True,
        "message": "Nếu email tồn tại và chưa xác minh, bạn sẽ nhận được email mới.",
    }


# ══════════════════════════════════════════
# HTML page cho verify email
# ══════════════════════════════════════════
def _verify_html(status: str, title: str, body: str) -> str:
    is_ok   = status == "success"
    color   = "#00d4aa" if is_ok else "#ff4d6a"
    rgb     = "0,212,170" if is_ok else "255,77,106"
    icon    = "✓" if is_ok else "✕"
    btn_txt = "Đến trang đăng nhập" if is_ok else "Về trang chủ"
    btn_url = "/auth/login-page" if is_ok else "/"
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — FaceAttend</title>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&family=Space+Mono:wght@700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{min-height:100vh;background:#060c17;display:flex;align-items:center;justify-content:center;
  font-family:'Sora',sans-serif;color:#e8f0fe;
  background-image:linear-gradient(rgba(26,47,80,.3) 1px,transparent 1px),
    linear-gradient(90deg,rgba(26,47,80,.3) 1px,transparent 1px);background-size:40px 40px}}
.card{{background:#0d1626;border:1px solid #1a2f50;border-radius:20px;padding:52px 48px;
  max-width:440px;width:90%;text-align:center;box-shadow:0 40px 80px rgba(0,0,0,.5)}}
.icon{{width:72px;height:72px;border-radius:50%;border:2px solid {color};
  background:rgba({rgb},.12);display:flex;align-items:center;justify-content:center;
  font-size:28px;color:{color};margin:0 auto 24px}}
h1{{font-size:20px;font-weight:700;margin-bottom:12px}}
p{{font-size:13px;color:#5a7a9a;line-height:1.7;margin-bottom:32px}}
p strong{{color:#e8f0fe}}
.btn{{display:inline-block;background:{color};color:#000;font-weight:700;font-size:13px;
  padding:12px 28px;border-radius:8px;text-decoration:none;transition:opacity .2s}}
.btn:hover{{opacity:.85}}
.brand{{font-family:'Space Mono',monospace;font-size:12px;color:#00d4aa;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:28px}}
</style>
</head>
<body>
<div class="card">
  <div class="brand">FaceAttend</div>
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{body}</p>
  <a href="{btn_url}" class="btn">{btn_txt}</a>
</div>
</body>
</html>"""