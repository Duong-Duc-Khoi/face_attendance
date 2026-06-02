"""Branch-scoped authorization helpers."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.employee import Employee
from app.models.user import User

BRANCH_MANAGER_STORE_ROLES = ("store_manager", "assistant_manager")


def employee_for_user(db: Session, user: User) -> Employee | None:
    emp = db.query(Employee).filter_by(user_id=user.id, is_active=True).first()
    if emp:
        return emp
    return (
        db.query(Employee)
        .filter(Employee.email == user.email, Employee.is_active == True)
        .order_by(Employee.id.asc())
        .first()
    )


def managed_branch_ids(db: Session, user: User) -> list[int]:
    if user.role == "admin":
        return [row[0] for row in db.query(Branch.id).filter(Branch.is_active == True).all()]
    if user.role != "manager":
        return []
    emp = employee_for_user(db, user)
    if not emp or not emp.branch_id:
        return []
    if emp.store_role not in BRANCH_MANAGER_STORE_ROLES:
        return []
    return [emp.branch_id]


def user_scope(db: Session, user: User) -> dict:
    emp = employee_for_user(db, user)
    branch_ids = managed_branch_ids(db, user)
    if user.role == "admin":
        scope_type = "admin"
    elif branch_ids:
        scope_type = "branch_manager"
    else:
        scope_type = "staff"
    return {
        "type": scope_type,
        "branch_ids": branch_ids,
        "branch_id": branch_ids[0] if len(branch_ids) == 1 else None,
        "store_role": (emp.store_role if emp else "") or "",
        "employee_id": emp.id if emp else None,
        "emp_code": emp.emp_code if emp else "",
    }


def selected_branch_ids(db: Session, user: User, branch_id: int | None = None) -> list[int] | None:
    """Return None for admin/all-branches, otherwise a concrete allowed branch id list."""
    if user.role == "admin":
        if branch_id is None:
            return None
        branch = db.query(Branch).filter_by(id=branch_id).first()
        if not branch:
            raise HTTPException(status_code=404, detail="Không tìm thấy cửa hàng")
        return [branch_id]

    allowed = managed_branch_ids(db, user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Bạn chưa được gán quản lý cửa hàng nào")
    if branch_id is None:
        return allowed
    if branch_id not in allowed:
        raise HTTPException(status_code=403, detail="Bạn chỉ được xem cửa hàng mình quản lý")
    return [branch_id]


def default_branch_id_for_write(db: Session, user: User, branch_id: int | None = None) -> int | None:
    if user.role == "admin":
        if branch_id is not None:
            selected_branch_ids(db, user, branch_id)
        return branch_id
    allowed = selected_branch_ids(db, user, branch_id)
    return allowed[0] if allowed else None


def is_admin(user: User) -> bool:
    return user.role == "admin"


def is_branch_manager(db: Session, user: User) -> bool:
    return bool(managed_branch_ids(db, user))


def require_admin(user: User) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Yêu cầu quyền admin")


def require_branch_manager_or_admin(db: Session, user: User) -> list[int]:
    if user.role == "admin":
        return managed_branch_ids(db, user)
    branch_ids = managed_branch_ids(db, user)
    if not branch_ids:
        raise HTTPException(status_code=403, detail="Yêu cầu admin hoặc cửa hàng trưởng/cửa hàng phó")
    return branch_ids


def ensure_branch_access(db: Session, user: User, branch_id: int | None, *, allow_unassigned_for_admin: bool = True) -> None:
    if user.role == "admin":
        if branch_id is None and not allow_unassigned_for_admin:
            raise HTTPException(status_code=400, detail="Cần chọn cửa hàng")
        return
    allowed = managed_branch_ids(db, user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Bạn chưa được gán quản lý cửa hàng nào")
    if branch_id is None or branch_id not in allowed:
        raise HTTPException(status_code=403, detail="Bạn chỉ được thao tác trong cửa hàng mình quản lý")


def scoped_branch_filter(db: Session, user: User):
    if user.role == "admin":
        return None
    allowed = managed_branch_ids(db, user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Bạn chưa được gán quản lý cửa hàng nào")
    return allowed
