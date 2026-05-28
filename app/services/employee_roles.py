"""Helpers for store employee operating roles."""

from sqlalchemy.orm import Session

from app.models.employee_role import EmployeeRole


DEFAULT_EMPLOYEE_ROLES = [
    ("security", "Bảo vệ", "Trông xe, giữ an ninh cửa hàng", 10),
    ("cashier", "Thu ngân", "Nhận order, thanh toán, đối soát tiền", 20),
    ("barista", "Pha chế", "Chuẩn bị sữa chua trân châu, topping và đồ uống", 30),
    ("table_service", "Phục vụ bàn", "Phục vụ khách tại bàn, dọn bàn, hỗ trợ sảnh", 40),
]


def seed_default_employee_roles(db: Session):
    existing = {r.code for r in db.query(EmployeeRole).all()}
    changed = False
    for code, name, description, sort_order in DEFAULT_EMPLOYEE_ROLES:
        role = db.query(EmployeeRole).filter_by(code=code).first()
        if role:
            if role.name != name or role.description != description or role.sort_order != sort_order:
                role.name = name
                role.description = description
                role.sort_order = sort_order
                role.is_active = True
                changed = True
            continue
        if code not in existing:
            db.add(EmployeeRole(code=code, name=name, description=description, sort_order=sort_order, is_active=True))
            changed = True
    if changed:
        db.commit()
