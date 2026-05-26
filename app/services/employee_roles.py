"""Helpers for restaurant employee operating roles."""

from sqlalchemy.orm import Session

from app.models.employee_role import EmployeeRole


DEFAULT_EMPLOYEE_ROLES = [
    ("server", "Phục vụ", "Nhân viên phục vụ bàn", 10),
    ("cashier", "Thu ngân", "Nhân viên thu ngân", 20),
    ("kitchen", "Bếp", "Bếp chính/phụ bếp", 30),
    ("bar", "Bar", "Pha chế/quầy bar", 40),
    ("cleaner", "Tạp vụ", "Vệ sinh, dọn dẹp", 50),
    ("shift_lead", "Quản lý ca", "Điều phối vận hành trong ca", 60),
]


def seed_default_employee_roles(db: Session):
    existing = {r.code for r in db.query(EmployeeRole).all()}
    changed = False
    for code, name, description, sort_order in DEFAULT_EMPLOYEE_ROLES:
        if code in existing:
            continue
        db.add(EmployeeRole(code=code, name=name, description=description, sort_order=sort_order, is_active=True))
        changed = True
    if changed:
        db.commit()
