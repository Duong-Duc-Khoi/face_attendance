"""
app/core/database.py
Kết nối database và session factory.
Seed dữ liệu mẫu nằm trong scripts/seed.py — không lẫn vào đây.

Fix: Lazy engine initialization để tránh lỗi import khi thiếu psycopg2
hoặc chưa cấu hình .env.
"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.core.config import settings

_engine = None
_SessionLocal = None


def get_engine():
    """Lazy-init engine — chỉ tạo lần đầu gọi."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def SessionLocal() -> Session:
    """Tạo session mới. Dùng như context manager hoặc gọi thủ công."""
    return get_session_factory()()


def get_db():
    """FastAPI dependency — tự đóng session sau mỗi request."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Tạo tất cả bảng. Gọi khi app khởi động."""
    from app.models import Base  # noqa — import để SQLAlchemy nhận diện tất cả models
    Base.metadata.create_all(bind=get_engine())
    _run_restaurant_schema_migration()
    db = get_session_factory()()
    try:
        from app.services.shift_service import seed_default_shifts
        seed_default_shifts(db)
    finally:
        db.close()
#     _seed_sample_data()


def _run_restaurant_schema_migration():
    """
    create_all() không ALTER bảng đã tồn tại. Chạy migration idempotent để DB cũ
    có đủ cột/bảng trước khi ORM query các model mới.
    """
    migration_path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_restaurant_schema.sql"
    if not migration_path.exists():
        return

    sql = migration_path.read_text(encoding="utf-8")
    conn = get_engine().raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("  ✓ Restaurant schema migration sẵn sàng")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# def _seed_sample_data():
#     """Tạo dữ liệu mẫu nếu DB trống (chỉ chạy lần đầu)."""
#     from app.models.employee import Employee
#     db = get_session_factory()()
#     try:
#         if db.query(Employee).count() == 0:
#             samples = [
#                 Employee(emp_code="NV001", name="Nguyễn Văn An",
#                          department="Kỹ thuật",   position="Lập trình viên"),
#                 Employee(emp_code="NV002", name="Trần Thị Bình",
#                          department="Kinh doanh",  position="Nhân viên kinh doanh"),
#                 Employee(emp_code="NV003", name="Lê Minh Cường",
#                          department="Kế toán",     position="Kế toán viên"),
#             ]
#             db.add_all(samples)
#             db.commit()
#             print("  ✓ Đã tạo dữ liệu nhân viên mẫu")
#     finally:
#         db.close()
