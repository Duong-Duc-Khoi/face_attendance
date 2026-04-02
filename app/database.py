"""
app/database.py
Kết nối PostgreSQL + toàn bộ models + auth helpers.

Models:
  - Employee
  - AttendanceLog
  - User
  - EmailToken
  - RefreshToken

Auth helpers (được import trong auth_routes.py):
  hash_password, verify_password,
  create_access_token, create_refresh_token, decode_access_token,
  create_verify_token, create_otp_token,
  verify_email_token, consume_token,
  send_verification_email, send_login_otp_email,
  get_current_user, _hash_token,
  REFRESH_TOKEN_EXP
"""

# ── Stdlib ──────────────────────────────────────────────────────
import hashlib
import os
import random
import secrets
import smtplib
import string
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

# ── Third-party ─────────────────────────────────────────────────
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker

# ── Load .env ────────────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ════════════════════════════════════════════════════════════════
# DATABASE CONNECTION
# ════════════════════════════════════════════════════════════════
DATABASE_URL = (
    f"postgresql+psycopg2://"
    f"{os.getenv('DB_USER', 'postgres')}:"
    f"{os.getenv('DB_PASSWORD', '')}@"
    f"{os.getenv('DB_HOST', 'localhost')}:"
    f"{os.getenv('DB_PORT', '5432')}/"
    f"{os.getenv('DB_NAME', 'face_attendance')}"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ════════════════════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════════════════════

class Employee(Base):
    __tablename__ = "employees"

    id           = Column(Integer, primary_key=True, index=True)
    emp_code     = Column(String(20), unique=True, index=True, nullable=False)
    name         = Column(String(100), nullable=False)
    department   = Column(String(100), default="")
    position     = Column(String(100), default="")
    email        = Column(String(150), default="")
    phone        = Column(String(20),  default="")
    face_path    = Column(String(255), default="")
    avatar_url   = Column(String(255), default="")
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.now)
    updated_at   = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AttendanceLog(Base):
    __tablename__ = "attendance_logs"

    id           = Column(Integer, primary_key=True, index=True)
    employee_id  = Column(Integer, index=True)
    emp_code     = Column(String(20), index=True)
    emp_name     = Column(String(100), default="")
    department   = Column(String(100), default="")
    check_type   = Column(String(20))          # "check_in" | "check_out"
    timestamp    = Column(DateTime, default=datetime.now, index=True)
    confidence   = Column(Float, default=0.0)
    capture_path = Column(String(255), default="")
    note         = Column(Text, default="")


class User(Base):
    __tablename__ = "users"

    id                = Column(Integer, primary_key=True, index=True)
    email             = Column(String(150), unique=True, index=True, nullable=False)
    username          = Column(String(50),  unique=True, index=True, nullable=False)
    full_name         = Column(String(100), default="")
    hashed_password   = Column(String(255), nullable=False)
    role              = Column(String(20),  default="staff")   # admin | manager | staff
    is_active         = Column(Boolean, default=False)
    is_email_verified = Column(Boolean, default=False)
    created_at        = Column(DateTime, default=datetime.now)
    updated_at        = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    last_login        = Column(DateTime, nullable=True)


class EmailToken(Base):
    """Dùng cho cả xác minh email (verify_email) và OTP đăng nhập (login_otp)."""
    __tablename__ = "email_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    purpose    = Column(String(20), nullable=False)   # "verify_email" | "login_otp"
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked    = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


# ════════════════════════════════════════════════════════════════
# CONFIG (đọc từ .env)
# ════════════════════════════════════════════════════════════════
SECRET_KEY        = os.getenv("SECRET_KEY", "change-me-in-production")
ALGORITHM         = "HS256"
ACCESS_TOKEN_EXP  = int(os.getenv("ACCESS_TOKEN_EXP_MINUTES", 15))   # phút
REFRESH_TOKEN_EXP = int(os.getenv("REFRESH_TOKEN_EXP_DAYS",   30))   # ngày

SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", 587))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
APP_BASE_URL  = os.getenv("APP_BASE_URL", "http://localhost:8000")

# ════════════════════════════════════════════════════════════════
# PASSWORD HASHING
# ════════════════════════════════════════════════════════════════
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ════════════════════════════════════════════════════════════════
# TOKEN UTILITIES
# ════════════════════════════════════════════════════════════════
def _hash_token(raw: str) -> str:
    """SHA-256 của raw token — dùng để lưu DB, không lưu raw."""
    return hashlib.sha256(raw.encode()).hexdigest()


def create_access_token(user_id: int, role: str) -> str:
    payload = {
        "sub":  str(user_id),
        "role": role,
        "exp":  datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXP),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def create_refresh_token(user_id: int, db: Session) -> str:
    raw = secrets.token_urlsafe(48)
    rt  = RefreshToken(
        user_id    = user_id,
        token_hash = _hash_token(raw),
        expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXP),
    )
    db.add(rt)
    db.commit()
    return raw


def create_verify_token(user_id: int, db: Session) -> str:
    """Tạo token xác minh email (có hiệu lực 24h)."""
    raw = secrets.token_urlsafe(32)
    et  = EmailToken(
        user_id    = user_id,
        token_hash = _hash_token(raw),
        purpose    = "verify_email",
        expires_at = datetime.utcnow() + timedelta(hours=24),
    )
    db.add(et)
    db.commit()
    return raw


def create_otp_token(user_id: int, db: Session) -> str:
    """Tạo OTP 6 chữ số cho đăng nhập (có hiệu lực 10 phút)."""
    otp = "".join(random.choices(string.digits, k=6))
    et  = EmailToken(
        user_id    = user_id,
        token_hash = _hash_token(otp),
        purpose    = "login_otp",
        expires_at = datetime.utcnow() + timedelta(minutes=10),
    )
    db.add(et)
    db.commit()
    return otp


def verify_email_token(raw: str, purpose: str, db: Session) -> Optional[EmailToken]:
    """Tìm EmailToken hợp lệ theo raw value và purpose."""
    et = db.query(EmailToken).filter_by(
        token_hash = _hash_token(raw),
        purpose    = purpose,
        used       = False,
    ).first()
    if not et:
        return None
    if et.expires_at < datetime.utcnow():
        return None
    return et


def consume_token(et: EmailToken, db: Session) -> None:
    """Đánh dấu token đã dùng."""
    et.used = True
    db.commit()


# ════════════════════════════════════════════════════════════════
# EMAIL SENDING
# ════════════════════════════════════════════════════════════════
def _send_email(to: str, subject: str, html: str) -> None:
    """Gửi email qua SMTP. Lỗi được log nhưng không raise để không block request."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL] SMTP chưa cấu hình — bỏ qua gửi tới {to}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, [to], msg.as_string())
    except Exception as exc:
        print(f"[EMAIL ERROR] Không gửi được tới {to}: {exc}")


def send_verification_email(to: str, username: str, token: str) -> None:
    link = f"{APP_BASE_URL}/auth/verify-email?token={token}"
    html = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:auto">
      <h2 style="color:#00d4aa">Xác minh tài khoản FaceAttend</h2>
      <p>Xin chào <strong>{username}</strong>,</p>
      <p>Click nút bên dưới để kích hoạt tài khoản của bạn (hiệu lực 24 giờ):</p>
      <a href="{link}" style="display:inline-block;background:#00d4aa;color:#000;
         font-weight:700;padding:12px 28px;border-radius:8px;text-decoration:none;margin:16px 0">
        Xác minh Email
      </a>
      <p style="color:#888;font-size:12px">Nếu bạn không đăng ký, hãy bỏ qua email này.</p>
    </div>"""
    _send_email(to, "FaceAttend — Xác minh email của bạn", html)


def send_login_otp_email(to: str, username: str, otp: str) -> None:
    html = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:auto">
      <h2 style="color:#00d4aa">Mã đăng nhập FaceAttend</h2>
      <p>Xin chào <strong>{username}</strong>,</p>
      <p>Mã OTP đăng nhập của bạn:</p>
      <div style="font-size:36px;font-weight:700;letter-spacing:8px;color:#00d4aa;margin:20px 0">
        {otp}
      </div>
      <p style="color:#888;font-size:12px">Mã có hiệu lực trong <strong>10 phút</strong>.
        Không chia sẻ mã này với ai.</p>
    </div>"""
    _send_email(to, "FaceAttend — Mã OTP đăng nhập", html)


# ════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCIES
# ════════════════════════════════════════════════════════════════
_bearer = HTTPBearer()

def get_db():
    """Dependency cho FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    token   = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token không hợp lệ hoặc đã hết hạn")

    user = db.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
    if not user:
        raise HTTPException(status_code=401, detail="Tài khoản không tồn tại hoặc đã bị khóa")
    return user


# ════════════════════════════════════════════════════════════════
# INIT DB
# ════════════════════════════════════════════════════════════════
def init_db():
    """Tạo tất cả bảng và seed dữ liệu mẫu nếu DB trống."""
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        if db.query(Employee).count() == 0:
            samples = [
                Employee(emp_code="NV001", name="Nguyễn Văn An",
                         department="Kỹ thuật", position="Lập trình viên"),
                Employee(emp_code="NV002", name="Trần Thị Bình",
                         department="Kinh doanh", position="Nhân viên kinh doanh"),
                Employee(emp_code="NV003", name="Lê Minh Cường",
                         department="Kế toán", position="Kế toán viên"),
            ]
            db.add_all(samples)
            db.commit()
            print("  ✓ Đã tạo dữ liệu nhân viên mẫu")
    finally:
        db.close()