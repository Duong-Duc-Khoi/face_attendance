"""
app/database.py
Kết nối PostgreSQL + models Employee, AttendanceLog.
Models auth (User, EmailToken, RefreshToken) nằm trong app/auth.py.
"""

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean, Column, DateTime, Float,
    Integer, String, Text, create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ════════════════════════════════════════════════════════════════
# CONNECTION
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


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════
def get_db():
    """Dependency cho FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Tạo tất cả bảng và seed dữ liệu mẫu nếu DB trống."""
    # Import auth models để SQLAlchemy nhận diện và tạo bảng
    from app.auth import User, EmailToken, RefreshToken  # noqa
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