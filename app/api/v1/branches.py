"""Branch management endpoints."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.branch import Branch
from app.models.shift import ShiftAssignment
from app.services.branch_scope import require_admin, require_branch_manager_or_admin, scoped_branch_filter

router = APIRouter(prefix="/api/branches", tags=["branches"])


class BranchCreate(BaseModel):
    name: str
    address: Optional[str] = ""
    phone: Optional[str] = ""


class BranchUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None


def _branch_to_dict(branch: Branch) -> dict:
    return {
        "id": branch.id,
        "name": branch.name,
        "address": branch.address or "",
        "phone": branch.phone or "",
        "is_active": bool(branch.is_active),
    }


@router.get("")
def api_list_branches(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
    q = db.query(Branch)
    allowed = scoped_branch_filter(db, current_user)
    if allowed is not None:
        q = q.filter(Branch.id.in_(allowed))
    rows = q.order_by(Branch.name.asc()).all()
    return [_branch_to_dict(row) for row in rows]


@router.get("/public")
def api_public_branches(db: Session = Depends(get_db)):
    rows = (
        db.query(Branch)
        .filter(Branch.is_active == True)
        .order_by(Branch.name.asc())
        .all()
    )
    return [_branch_to_dict(row) for row in rows]


@router.post("", status_code=201)
def api_create_branch(
    body: BranchCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_admin(current_user)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Tên cửa hàng là bắt buộc")
    if db.query(Branch).filter_by(name=name).first():
        raise HTTPException(status_code=400, detail="Tên cửa hàng đã tồn tại")
    branch = Branch(
        name=name,
        address=(body.address or "").strip(),
        phone=(body.phone or "").strip(),
        is_active=True,
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return {"success": True, "branch": _branch_to_dict(branch)}


@router.put("/{branch_id}")
def api_update_branch(
    branch_id: int,
    body: BranchUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_admin(current_user)
    branch = db.query(Branch).filter_by(id=branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Không tìm thấy cửa hàng")
    was_active = bool(branch.is_active)
    cancelled_assignments = 0
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        if not name:
            raise HTTPException(status_code=422, detail="Tên cửa hàng là bắt buộc")
        duplicate = db.query(Branch).filter(Branch.id != branch.id, Branch.name == name).first()
        if duplicate:
            raise HTTPException(status_code=400, detail="Tên cửa hàng đã tồn tại")
        branch.name = name
        data.pop("name")
    for field, value in data.items():
        if field in ("address", "phone") and value is not None:
            value = str(value).strip()
        setattr(branch, field, value)
    if was_active and body.is_active is False:
        today = date.today()
        assignments = (
            db.query(ShiftAssignment)
            .filter(
                ShiftAssignment.branch_id == branch.id,
                ShiftAssignment.work_date >= today,
                ShiftAssignment.status != "cancelled",
            )
            .all()
        )
        for assignment in assignments:
            assignment.status = "cancelled"
            suffix = "Tự hủy vì cửa hàng ngừng hoạt động"
            assignment.note = f"{assignment.note} | {suffix}" if assignment.note else suffix
        cancelled_assignments = len(assignments)
    db.commit()
    db.refresh(branch)
    return {
        "success": True,
        "branch": _branch_to_dict(branch),
        "cancelled_assignments": cancelled_assignments,
    }
