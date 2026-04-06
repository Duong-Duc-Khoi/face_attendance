"""
app/auth.py
Hệ thống xác thực + phân quyền 3 cấp:
  - staff   : xem lịch sử điểm danh bản thân + sửa thông tin cá nhân
  - manager : quản lý nhân viên + xem toàn bộ dữ liệu, không tạo/xóa manager
  - admin   : toàn quyền
"""

import os
import secrets
import hashlib
import smtplib
import bcrypt
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import Session

from app.database import Base, get_db

# ──────────────────────────────────────────
# Config
# ──────────────────────────────────────────
JWT_SECRET        = os.getenv("JWT_SECRET", "CHANGE_THIS_SECRET_IN_PRODUCTION_32CHARS")
JWT_ALGORITHM     = "HS256"
ACCESS_TOKEN_EXP  = int(os.getenv("ACCESS_TOKEN_EXP",  "15"))
REFRESH_TOKEN_EXP = int(os.getenv("REFRESH_TOKEN_EXP", "10080"))  # 7 ngày
OTP_EXP_MINUTES   = int(os.getenv("OTP_EXP_MINUTES",   "10"))

EMAIL_HOST     = os.getenv("EMAIL_HOST",     "smtp.gmail.com")
EMAIL_PORT     = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER     = os.getenv("EMAIL_USER",     "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
APP_NAME       = "FaceAttend"
BASE_URL       = os.getenv("BASE_URL", "http://localhost:8000")

# ──────────────────────────────────────────
# Phân quyền — định nghĩa tập trung 1 chỗ
# ──────────────────────────────────────────
ROLES = ("staff", "manager", "admin")

PERMISSIONS = {
    # ── Điểm danh ──────────────────────────────────────────────
    # Xem lịch sử điểm danh của chính mình
    "attendance:read_own":   {"staff", "manager", "admin"},
    # Xem toàn bộ lịch sử điểm danh
    "attendance:read_all":   {"manager", "admin"},
    # Sửa/xóa log điểm danh
    "attendance:write":      {"admin"},

    # ── Khuôn mặt ──────────────────────────────────────────────
    # Đăng ký khuôn mặt của chính mình
    "face:register_own":     {"staff", "manager", "admin"},
    # Đăng ký/xóa khuôn mặt của người khác
    "face:register_any":     {"manager", "admin"},

    # ── Nhân viên ───────────────────────────────────────────────
    # Xem danh sách nhân viên
    "employee:read":         {"manager", "admin"},
    # Thêm/sửa nhân viên
    "employee:write":        {"manager", "admin"},
    # Xóa nhân viên
    "employee:delete":       {"admin"},

    # ── Tài khoản cá nhân ───────────────────────────────────────
    # Sửa thông tin cá nhân (họ tên, avatar...)
    "profile:write":         {"staff", "manager", "admin"},

    # ── Quản lý tài khoản ───────────────────────────────────────
    # Xem danh sách user
    "user:read":             {"manager", "admin"},
    # Nâng/hạ role — chỉ admin
    "user:set_role":         {"admin"},
    # Duyệt/khóa tài khoản — manager chỉ duyệt được staff
    "user:approve":          {"manager", "admin"},
    # Xóa tài khoản — chỉ admin
    "user:delete":           {"admin"},
}

def has_permission(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


# ──────────────────────────────────────────
# Password hashing
# ──────────────────────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ──────────────────────────────────────────
# Models
# ──────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id                = Column(Integer, primary_key=True, index=True)
    email             = Column(String(150), unique=True, index=True, nullable=False)
    full_name         = Column(String(120), default="")
    hashed_password   = Column(String(255), nullable=False)
    role              = Column(String(20),  default="staff")   # staff | manager | admin
    is_active         = Column(Boolean, default=False)         # False cho đến khi xác minh email
    is_email_verified = Column(Boolean, default=False)
    is_approved       = Column(Boolean, default=False)         # Admin/manager duyệt tài khoản
    created_at        = Column(DateTime, default=datetime.now)
    last_login        = Column(DateTime, nullable=True)


class EmailToken(Base):
    __tablename__ = "email_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, index=True, nullable=False)
    token      = Column(String(128), unique=True, index=True, nullable=False)
    token_type = Column(String(30),  nullable=False)  # verify_email | login_otp
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, index=True, nullable=False)
    token_hash = Column(String(128), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked    = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


# ──────────────────────────────────────────
# JWT helpers
# ──────────────────────────────────────────
def _gen_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXP)
    return jwt.encode({
        "sub":  str(user_id),
        "role": role,
        "type": "access",
        "exp":  expire,
        "iat":  datetime.utcnow(),
    }, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: int, db: Session) -> str:
    raw = _gen_token(48)
    db.add(RefreshToken(
        user_id    = user_id,
        token_hash = _hash_token(raw),
        expires_at = datetime.utcnow() + timedelta(minutes=REFRESH_TOKEN_EXP),
    ))
    db.commit()
    return raw

def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise ValueError("wrong token type")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token đã hết hạn")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token không hợp lệ")


# ──────────────────────────────────────────
# Email token helpers
# ──────────────────────────────────────────
def _create_email_token(user_id: int, token_type: str,
                        exp_minutes: int, db: Session) -> str:
    db.query(EmailToken).filter_by(
        user_id=user_id, token_type=token_type, used=False
    ).delete()
    raw = _gen_token(32)
    db.add(EmailToken(
        user_id    = user_id,
        token      = raw,
        token_type = token_type,
        expires_at = datetime.utcnow() + timedelta(minutes=exp_minutes),
    ))
    db.commit()
    return raw

def create_verify_token(user_id: int, db: Session) -> str:
    return _create_email_token(user_id, "verify_email", 1440, db)

def create_otp_token(user_id: int, db: Session) -> str:
    otp = str(secrets.randbelow(900000) + 100000)
    db.query(EmailToken).filter_by(
        user_id=user_id, token_type="login_otp", used=False
    ).delete()
    db.add(EmailToken(
        user_id    = user_id,
        token      = otp,
        token_type = "login_otp",
        expires_at = datetime.utcnow() + timedelta(minutes=OTP_EXP_MINUTES),
    ))
    db.commit()
    return otp

def verify_email_token(token: str, token_type: str, db: Session) -> Optional[EmailToken]:
    et = db.query(EmailToken).filter_by(
        token=token, token_type=token_type, used=False
    ).first()
    if not et or et.expires_at < datetime.utcnow():
        return None
    return et

def consume_token(et: EmailToken, db: Session):
    et.used = True
    db.commit()


# ──────────────────────────────────────────
# Email sending
# ──────────────────────────────────────────
def _send_email(to: str, subject: str, html: str) -> bool:
    if not EMAIL_USER or not EMAIL_PASSWORD:
        print(f"  [AUTH] Email chưa cấu hình — bỏ qua gửi tới {to}")
        return False
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{APP_NAME} <{EMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=10) as s:
            s.starttls()
            s.login(EMAIL_USER, EMAIL_PASSWORD)
            s.sendmail(EMAIL_USER, to, msg.as_string())
        print(f"  [AUTH] ✓ Email gửi tới {to}")
        return True
    except Exception as e:
        print(f"  [AUTH] ✗ Lỗi email: {e}")
        return False

def send_verification_email(to: str, full_name: str, token: str):
    link = f"{BASE_URL}/auth/verify-email?token={token}"
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
          Tài khoản của bạn đã được tạo. Sau khi xác minh email, tài khoản sẽ chờ
          được <strong style="color:#f6c90e">admin/manager duyệt</strong> trước khi đăng nhập được.
        </p>
        <a href="{link}" style="display:inline-block;background:#00d4aa;color:#000;
           font-weight:700;font-size:14px;padding:13px 32px;border-radius:8px;text-decoration:none">
          ✓ Xác minh Email
        </a>
        <p style="margin-top:28px;font-size:12px;color:#3a5a7a">
          Link có hiệu lực trong <strong>24 giờ</strong>.
        </p>
        <hr style="border:none;border-top:1px solid #1a2f50;margin:24px 0">
        <p style="font-size:11px;color:#2a4a6a;word-break:break-all">Hoặc copy link: {link}</p>
      </div>
    </div>"""
    _send_email(to, f"[{APP_NAME}] Xác minh email tài khoản", html)

def send_login_otp_email(to: str, full_name: str, otp: str):
    name = full_name or to
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#060c17;color:#e8f0fe;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#0d1626,#111e34);padding:36px 40px 28px;
                  border-bottom:1px solid #1a2f50">
        <div style="font-family:monospace;font-size:18px;font-weight:700;color:#00d4aa;
                    letter-spacing:.12em">FACEATTEND</div>
        <h1 style="font-size:22px;font-weight:700;margin:14px 0 6px">Mã xác nhận đăng nhập</h1>
        <p style="color:#5a7a9a;font-size:13px;margin:0">
          Xin chào <strong style="color:#e8f0fe">{name}</strong></p>
      </div>
      <div style="padding:32px 40px;text-align:center">
        <p style="font-size:14px;color:#a0b4c8;margin-bottom:24px">Mã OTP đăng nhập của bạn:</p>
        <div style="background:#0d1626;border:2px solid #00d4aa;border-radius:12px;
                    padding:24px 40px;display:inline-block;margin-bottom:24px">
          <span style="font-family:monospace;font-size:42px;font-weight:700;color:#00d4aa;
                       letter-spacing:.25em">{otp}</span>
        </div>
        <p style="font-size:13px;color:#5a7a9a">
          Có hiệu lực trong <strong style="color:#f6c90e">{OTP_EXP_MINUTES} phút</strong>.<br>
          Không chia sẻ mã này với bất kỳ ai.
        </p>
      </div>
    </div>"""
    _send_email(to, f"[{APP_NAME}] Mã OTP đăng nhập: {otp}", html)

def send_approval_notification(to: str, full_name: str, role: str):
    """Gửi email thông báo tài khoản đã được duyệt."""
    role_display = {"staff": "Nhân viên", "manager": "Quản lý", "admin": "Quản trị viên"}.get(role, role)
    name = full_name or to
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#060c17;color:#e8f0fe;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#0d1626,#111e34);padding:36px 40px 28px;
                  border-bottom:1px solid #1a2f50">
        <div style="font-family:monospace;font-size:18px;font-weight:700;color:#00d4aa;
                    letter-spacing:.12em">FACEATTEND</div>
        <h1 style="font-size:22px;font-weight:700;margin:14px 0 6px">Tài khoản đã được duyệt!</h1>
      </div>
      <div style="padding:32px 40px">
        <p style="font-size:14px;color:#a0b4c8;line-height:1.7;margin-bottom:20px">
          Xin chào <strong style="color:#e8f0fe">{name}</strong>,<br><br>
          Tài khoản của bạn đã được phê duyệt với vai trò
          <strong style="color:#00d4aa">{role_display}</strong>.
          Bạn có thể đăng nhập ngay bây giờ.
        </p>
        <a href="{BASE_URL}/auth/login-page"
           style="display:inline-block;background:#00d4aa;color:#000;font-weight:700;
                  font-size:14px;padding:13px 32px;border-radius:8px;text-decoration:none">
          Đăng nhập ngay
        </a>
      </div>
    </div>"""
    _send_email(to, f"[{APP_NAME}] Tài khoản đã được phê duyệt", html)


# ──────────────────────────────────────────
# FastAPI dependencies
# ──────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập")
    payload = decode_access_token(credentials.credentials)
    user    = db.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
    if not user:
        raise HTTPException(status_code=401, detail="Tài khoản không tồn tại hoặc đã bị khóa")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Tài khoản chưa được duyệt. Vui lòng chờ admin/manager phê duyệt.")
    return user

def require_role(*roles: str):
    """Dependency factory: yêu cầu role cụ thể."""
    def _checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Cần quyền: {', '.join(roles)}")
        return current_user
    return _checker

def require_permission(permission: str):
    """Dependency factory: yêu cầu permission cụ thể."""
    def _checker(current_user: User = Depends(get_current_user)) -> User:
        if not has_permission(current_user.role, permission):
            raise HTTPException(status_code=403, detail=f"Không có quyền thực hiện thao tác này")
        return current_user
    return _checker

# Shortcut dependencies
require_admin   = require_role("admin")
require_manager = require_role("admin", "manager")
require_any     = get_current_user