"""
app/services/auth_service.py
Business logic xác thực: tạo token email, gửi email auth, shortcut dependencies.
Logic security thuần (JWT, hash) đã tách sang app/core/security.py.
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, has_permission, decode_access_token
from app.models.user import User, EmailToken, RefreshToken


# ── Token helpers ────────────────────────────────────────────────
def _gen_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def create_verify_token(user_id: int, db: Session) -> str:
    db.query(EmailToken).filter_by(user_id=user_id, token_type="verify_email", used=False).delete()
    raw = _gen_token(32)
    db.add(EmailToken(
        user_id    = user_id,
        token_hash = _hash_token(raw),
        token_type = "verify_email",
        expires_at = datetime.utcnow() + timedelta(hours=24),
    ))
    db.commit()
    return raw


def create_otp_token(user_id: int, db: Session) -> str:
    otp = str(secrets.randbelow(900000) + 100000)
    db.query(EmailToken).filter_by(user_id=user_id, token_type="login_otp", used=False).delete()
    db.add(EmailToken(
        user_id    = user_id,
        token_hash = _hash_token(otp),
        token_type = "login_otp",
        expires_at = datetime.utcnow() + timedelta(minutes=settings.OTP_EXP_MINUTES),
    ))
    db.commit()
    return otp


def verify_email_token(token: str, token_type: str, db: Session) -> Optional[EmailToken]:
    hashed = _hash_token(token)
    et = db.query(EmailToken).filter_by(token_hash=hashed, token_type=token_type, used=False).first()
    if not et or et.expires_at < datetime.utcnow():
        return None
    return et


def consume_token(et: EmailToken, db: Session):
    et.used = True
    db.commit()


def create_refresh_token_db(user_id: int, db: Session) -> str:
    raw = _gen_token(48)
    db.add(RefreshToken(
        user_id    = user_id,
        token_hash = _hash_token(raw),
        expires_at = datetime.utcnow() + timedelta(minutes=settings.REFRESH_TOKEN_EXP),
    ))
    db.commit()
    return raw


# ── Email sending ────────────────────────────────────────────────
def _send_email(to: str, subject: str, html: str) -> bool:
    if not settings.EMAIL_USER or not settings.EMAIL_PASSWORD:
        print(f"  [AUTH] Email chưa cấu hình — bỏ qua gửi tới {to}")
        return False
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{settings.APP_NAME} <{settings.EMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT, timeout=10) as s:
            s.starttls()
            s.login(settings.EMAIL_USER, settings.EMAIL_PASSWORD)
            s.sendmail(settings.EMAIL_USER, to, msg.as_string())
        print(f"  [AUTH] ✓ Email gửi tới {to}")
        return True
    except Exception as e:
        print(f"  [AUTH] ✗ Lỗi email: {e}")
        return False


def send_verification_email(to: str, full_name: str, token: str)-> bool:
    link = f"{settings.BASE_URL}/auth/verify-email?token={token}"
    name = full_name or to
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#060c17;color:#e8f0fe;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#0d1626,#111e34);padding:36px 40px 28px;
                  border-bottom:1px solid #1a2f50">
        <div style="font-family:monospace;font-size:18px;font-weight:700;color:#00d4aa;
                    letter-spacing:.12em">FACEATTEND</div>
        <h1 style="font-size:22px;font-weight:700;margin:14px 0 6px">Xác minh email của bạn</h1>
        <p style="color:#5a7a9a;font-size:13px;margin:0">
          Chào mừng <strong style="color:#e8f0fe">{name}</strong> đến với FaceAttend</p>
      </div>
      <div style="padding:32px 40px">
        <p style="font-size:14px;color:#a0b4c8;line-height:1.7;margin-bottom:28px">
          Sau khi xác minh email, tài khoản sẽ chờ
          <strong style="color:#f6c90e">admin/manager duyệt</strong> trước khi đăng nhập được.
        </p>
        <a href="{link}" style="display:inline-block;background:#00d4aa;color:#000;
           font-weight:700;font-size:14px;padding:13px 32px;border-radius:8px;text-decoration:none">
          ✓ Xác minh Email
        </a>
        <p style="margin-top:28px;font-size:12px;color:#3a5a7a">
          Link có hiệu lực trong <strong>24 giờ</strong>.
        </p>
      </div>
    </div>"""
    return _send_email(to, f"[{settings.APP_NAME}] Xác minh email tài khoản", html)


def send_login_otp_email(to: str, full_name: str, otp: str):
    name = full_name or to
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#060c17;color:#e8f0fe;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#0d1626,#111e34);padding:36px 40px 28px;
                  border-bottom:1px solid #1a2f50">
        <div style="font-family:monospace;font-size:18px;font-weight:700;color:#00d4aa">FACEATTEND</div>
        <h1 style="font-size:22px;font-weight:700;margin:14px 0 6px">Mã xác nhận đăng nhập</h1>
      </div>
      <div style="padding:32px 40px;text-align:center">
        <p style="font-size:14px;color:#a0b4c8;margin-bottom:24px">Xin chào {name}, mã OTP của bạn:</p>
        <div style="background:#0d1626;border:2px solid #00d4aa;border-radius:12px;
                    padding:24px 40px;display:inline-block;margin-bottom:24px">
          <span style="font-family:monospace;font-size:42px;font-weight:700;color:#00d4aa;
                       letter-spacing:.25em">{otp}</span>
        </div>
        <p style="font-size:13px;color:#5a7a9a">
          Có hiệu lực trong <strong style="color:#f6c90e">{settings.OTP_EXP_MINUTES} phút</strong>.
        </p>
      </div>
    </div>"""
    _send_email(to, f"[{settings.APP_NAME}] Mã OTP đăng nhập: {otp}", html)


def send_approval_notification(to: str, full_name: str, role: str):
    role_display = {"staff": "Nhân viên", "manager": "Quản lý", "admin": "Quản trị viên"}.get(role, role)
    name = full_name or to
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#060c17;color:#e8f0fe;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#0d1626,#111e34);padding:36px 40px 28px">
        <div style="font-family:monospace;font-size:18px;font-weight:700;color:#00d4aa">FACEATTEND</div>
        <h1 style="font-size:22px;margin:14px 0 6px">Tài khoản đã được duyệt!</h1>
      </div>
      <div style="padding:32px 40px">
        <p style="font-size:14px;color:#a0b4c8;line-height:1.7">
          Xin chào <strong style="color:#e8f0fe">{name}</strong>,<br>
          Tài khoản của bạn đã được phê duyệt với vai trò
          <strong style="color:#00d4aa">{role_display}</strong>.
        </p>
        <a href="{settings.BASE_URL}/auth/login-page"
           style="display:inline-block;background:#00d4aa;color:#000;font-weight:700;
                  font-size:14px;padding:13px 32px;border-radius:8px;text-decoration:none;margin-top:20px">
          Đăng nhập ngay
        </a>
      </div>
    </div>"""
    _send_email(to, f"[{settings.APP_NAME}] Tài khoản đã được phê duyệt", html)


# ── FastAPI dependency shortcuts ─────────────────────────────────
def require_role(*roles: str):
    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, detail=f"Cần quyền: {', '.join(roles)}")
        return user
    return _checker


def require_permission(permission: str):
    def _checker(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(403, detail="Không có quyền thực hiện thao tác này")
        return user
    return _checker


require_admin   = require_role("admin")
require_manager = require_role("admin", "manager")
require_any     = get_current_user
