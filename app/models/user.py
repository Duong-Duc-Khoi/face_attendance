"""
app/models/user.py
SQLAlchemy models cho hệ thống xác thực:
  - User
  - EmailToken  (xác minh email + OTP)
  - RefreshToken
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Integer, String
from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id                = Column(Integer, primary_key=True, index=True)
    email             = Column(String(150), unique=True, index=True, nullable=False)
    full_name         = Column(String(100), default="")
    hashed_password   = Column(String(255), nullable=False)
    role              = Column(String(20),  default="staff")   # staff | manager | admin
    is_active         = Column(Boolean, default=False)         # False cho đến khi được duyệt
    is_email_verified = Column(Boolean, default=False)
    created_at        = Column(DateTime, default=datetime.now)
    last_login        = Column(DateTime, nullable=True)


class EmailToken(Base):
    """Lưu token xác minh email và OTP đăng nhập (dùng chung 1 bảng)."""
    __tablename__ = "email_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, index=True, nullable=False)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    token_type = Column(String(20), nullable=False)   # "verify" | "otp"
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, index=True, nullable=False)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked    = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
