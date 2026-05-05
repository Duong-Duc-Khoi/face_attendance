"""
app/core/database.py
Kết nối database và session factory.
Seed dữ liệu mẫu nằm trong scripts/seed.py — không lẫn vào đây.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — tự đóng session sau mỗi request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Tạo tất cả bảng. Gọi khi app khởi động."""
    from app.models import Base  # noqa — import để SQLAlchemy nhận diện tất cả models
    Base.metadata.create_all(bind=engine)
    _seed_sample_data()


def _seed_sample_data():
    """Tạo dữ liệu mẫu nếu DB trống (chỉ chạy lần đầu)."""
    from app.models.employee import Employee
    db = SessionLocal()
    try:
        if db.query(Employee).count() == 0:
            samples = [
                Employee(emp_code="NV001", name="Nguyễn Văn An",
                         department="Kỹ thuật",   position="Lập trình viên"),
                Employee(emp_code="NV002", name="Trần Thị Bình",
                         department="Kinh doanh",  position="Nhân viên kinh doanh"),
                Employee(emp_code="NV003", name="Lê Minh Cường",
                         department="Kế toán",     position="Kế toán viên"),
            ]
            db.add_all(samples)
            db.commit()
            print("  ✓ Đã tạo dữ liệu nhân viên mẫu")
    finally:
        db.close()
