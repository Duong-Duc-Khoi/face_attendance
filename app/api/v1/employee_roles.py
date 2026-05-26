"""
CRUD vai trò công việc nhà hàng.

Không liên quan tới User.role dùng cho phân quyền hệ thống.
"""

import re
import unicodedata
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.employee_role import EmployeeRole
from app.models.shift import Shift
from app.models.user import User
from app.schemas.employee import JOB_ROLE_LABELS, normalize_job_role

router = APIRouter(prefix="/api/employee-roles", tags=["employee-roles"])


def _require_manager(user: User):
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Yêu cầu quyền manager hoặc admin")


def _slugify(value: str) -> str:
    normalized = normalize_job_role(value or "") or value or ""
    raw = normalized.replace("đ", "d").replace("Đ", "D")
    ascii_text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").lower()
    code = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")
    return code or "role"


class EmployeeRoleCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    code: Optional[str] = None
    description: Optional[str] = ""
    sort_order: int = 0
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        value = (value or "").strip()
        if not value:
            raise ValueError("Tên vai trò là bắt buộc")
        if len(value) > 100:
            raise ValueError("Tên vai trò tối đa 100 ký tự")
        return value


class EmployeeRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


def _role_dict(role: EmployeeRole, employee_count: int = 0) -> dict:
    return {
        "id": role.id,
        "code": role.code,
        "name": role.name,
        "description": role.description or "",
        "sort_order": role.sort_order or 0,
        "is_active": bool(role.is_active),
        "employee_count": employee_count,
        "created_at": role.created_at.isoformat() if role.created_at else None,
        "updated_at": role.updated_at.isoformat() if role.updated_at else None,
    }


def _employee_count_by_role(db: Session) -> dict[str, int]:
    rows = db.query(Employee.job_role).filter(Employee.job_role != "").all()
    counts: dict[str, int] = {}
    for (role_code,) in rows:
        code = role_code or ""
        counts[code] = counts.get(code, 0) + 1
    return counts


@router.get("")
def list_employee_roles(
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    q = db.query(EmployeeRole)
    if active_only:
        q = q.filter_by(is_active=True)
    rows = q.order_by(EmployeeRole.sort_order.asc(), EmployeeRole.name.asc()).all()
    counts = _employee_count_by_role(db)
    return [_role_dict(row, counts.get(row.code, 0)) for row in rows]


@router.post("", status_code=201)
def create_employee_role(
    body: EmployeeRoleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    code = _slugify(body.code or body.name)
    if db.query(EmployeeRole).filter_by(code=code).first():
        raise HTTPException(400, f"Vai trò '{code}' đã tồn tại")
    role = EmployeeRole(
        code=code,
        name=body.name.strip(),
        description=(body.description or "").strip(),
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    JOB_ROLE_LABELS[role.code] = role.name
    return _role_dict(role)


@router.put("/{role_id}")
def update_employee_role(
    role_id: int,
    body: EmployeeRoleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    role = db.query(EmployeeRole).filter_by(id=role_id).first()
    if not role:
        raise HTTPException(404, "Không tìm thấy vai trò")
    data = body.model_dump(exclude_unset=True)
    old_code = role.code
    if "code" in data and data["code"]:
        code = _slugify(data["code"])
        existing = db.query(EmployeeRole).filter_by(code=code).first()
        if existing and existing.id != role.id:
            raise HTTPException(400, f"Vai trò '{code}' đã tồn tại")
        role.code = code
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name:
            raise HTTPException(422, "Tên vai trò là bắt buộc")
        role.name = name
    if "description" in data:
        role.description = (data["description"] or "").strip()
    if "sort_order" in data and data["sort_order"] is not None:
        role.sort_order = data["sort_order"]
    if "is_active" in data and data["is_active"] is not None:
        role.is_active = bool(data["is_active"])

    if old_code != role.code:
        db.query(Employee).filter_by(job_role=old_code).update({"job_role": role.code})
        db.query(Shift).filter_by(required_position=old_code).update({"required_position": role.code})
    db.commit()
    db.refresh(role)
    JOB_ROLE_LABELS[role.code] = role.name
    return _role_dict(role, db.query(Employee).filter_by(job_role=role.code).count())


@router.delete("/{role_id}")
def delete_employee_role(
    role_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    role = db.query(EmployeeRole).filter_by(id=role_id).first()
    if not role:
        raise HTTPException(404, "Không tìm thấy vai trò")
    role.is_active = False
    db.commit()
    return {"success": True, "message": "Đã ngừng dùng vai trò", "role": _role_dict(role)}
