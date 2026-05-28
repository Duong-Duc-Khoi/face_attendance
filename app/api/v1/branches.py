"""
Branch attendance policy endpoints.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.branch import Branch
from app.services.branch_scope import ensure_branch_access, require_admin, require_branch_manager_or_admin, scoped_branch_filter

router = APIRouter(prefix="/api/branches", tags=["branches"])


class BranchPolicyFields(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    geofence_radius_m: Optional[int] = None
    mobile_attendance_enabled: Optional[bool] = None

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, value):
        if value is not None and not (-90 <= value <= 90):
            raise ValueError("latitude phải trong khoảng -90..90")
        return value

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, value):
        if value is not None and not (-180 <= value <= 180):
            raise ValueError("longitude phải trong khoảng -180..180")
        return value

    @field_validator("geofence_radius_m")
    @classmethod
    def validate_radius(cls, value):
        if value is not None and not (10 <= value <= 1000):
            raise ValueError("geofence_radius_m phải trong khoảng 10..1000")
        return value


class BranchCreate(BranchPolicyFields):
    name: str
    address: Optional[str] = ""
    phone: Optional[str] = ""
    mobile_attendance_enabled: Optional[bool] = False


class BranchUpdate(BranchPolicyFields):
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None


class BranchAttendancePolicyUpdate(BranchPolicyFields):
    pass


def _branch_to_dict(branch: Branch) -> dict:
    return {
        "id": branch.id,
        "name": branch.name,
        "address": branch.address or "",
        "phone": branch.phone or "",
        "latitude": branch.latitude,
        "longitude": branch.longitude,
        "geofence_radius_m": branch.geofence_radius_m or settings.MOBILE_GEOFENCE_RADIUS_DEFAULT_M,
        "mobile_attendance_enabled": bool(branch.mobile_attendance_enabled),
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
        latitude=body.latitude,
        longitude=body.longitude,
        geofence_radius_m=body.geofence_radius_m or settings.MOBILE_GEOFENCE_RADIUS_DEFAULT_M,
        mobile_attendance_enabled=bool(body.mobile_attendance_enabled),
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
    db.commit()
    db.refresh(branch)
    return {"success": True, "branch": _branch_to_dict(branch)}


@router.get("/{branch_id}/attendance-policy")
def api_get_branch_attendance_policy(
    branch_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    ensure_branch_access(db, current_user, branch_id)
    branch = db.query(Branch).filter_by(id=branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Không tìm thấy cửa hàng")
    return _branch_to_dict(branch)


@router.put("/{branch_id}/attendance-policy")
def api_update_branch_attendance_policy(
    branch_id: int,
    body: BranchAttendancePolicyUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    ensure_branch_access(db, current_user, branch_id)
    branch = db.query(Branch).filter_by(id=branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Không tìm thấy cửa hàng")

    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(branch, field, value)
    db.commit()
    db.refresh(branch)
    return {"success": True, "branch": _branch_to_dict(branch)}
