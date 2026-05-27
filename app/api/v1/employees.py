"""
app/api/v1/employees.py
CRUD nhân viên + đăng ký khuôn mặt.

Thay đổi:
  - Import từ app.models, app.core, app.services thay vì app.database/face_engine
  - Schemas Pydantic tách ra app/schemas/employee.py
"""

import base64
from datetime import datetime
from decimal import Decimal
import re
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.employee import Employee
from app.models.user import User
from app.schemas.employee import (
    EmployeeUpdate,
    JOB_ROLE_LABELS,
    is_active_for_status,
    normalize_employee_status,
    normalize_employment_type,
    normalize_job_role,
)
from app.services.face_engine import face_engine
from app.core.security import hash_password, get_current_user
from app.services.auth_service import create_verify_token, send_verification_email

router = APIRouter(prefix="/api/employees", tags=["employees"])


# ── Helper ───────────────────────────────────────────────────────
def _money(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _employee_job_role(e: Employee) -> str:
    return e.job_role or normalize_job_role(e.position or "")


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _clean_phone(value: str | None) -> str:
    phone = (value or "").strip()
    if len(phone) > 20:
        raise HTTPException(400, "Số điện thoại tối đa 20 ký tự")
    return phone


def _generate_employee_code(db: Session) -> str:
    rows = db.query(Employee.emp_code).filter(Employee.emp_code.like("NV%")).all()
    max_num = 0
    for (code,) in rows:
        match = re.fullmatch(r"NV(\d+)", (code or "").upper())
        if match:
            max_num = max(max_num, int(match.group(1)))
    for num in range(max_num + 1, max_num + 10000):
        code = f"NV{num:03d}"
        if not db.query(Employee).filter_by(emp_code=code).first():
            return code
    raise HTTPException(500, "Không thể tự tạo mã nhân viên")


def _emp_dict(e: Employee) -> dict:
    return {
        "id":         e.id,
        "user_id":    e.user_id,
        "branch_id":  e.branch_id,
        "emp_code":   e.emp_code,
        "name":       e.name,
        "full_name":  e.full_name or e.name,
        "department": e.department,
        "position":   e.position,
        "job_role":   _employee_job_role(e),
        "job_role_label": JOB_ROLE_LABELS.get(_employee_job_role(e), _employee_job_role(e)),
        "employment_type": e.employment_type or "full_time",
        "hourly_rate": _money(e.hourly_rate),
        "base_salary": _money(e.base_salary),
        "email":      e.email,
        "phone":      e.phone,
        "avatar_url": e.avatar_url,
        "status":     e.status or ("active" if e.is_active else "inactive"),
        "is_active":  e.is_active,
        "created_at": e.created_at.strftime("%d/%m/%Y") if e.created_at else "",
    }


def _profile_fields_from_payload(payload: dict) -> dict:
    position = (payload.get("position") or "").strip()
    job_role = normalize_job_role(payload.get("job_role") or position)
    status = normalize_employee_status(payload.get("status"), payload.get("is_active"))
    return {
        "branch_id": _optional_int(payload.get("branch_id")),
        "full_name": (payload.get("full_name") or payload.get("name") or "").strip(),
        "department": (payload.get("department") or "").strip(),
        "position": position,
        "job_role": job_role,
        "employment_type": normalize_employment_type(payload.get("employment_type")),
        "hourly_rate": _optional_decimal(payload.get("hourly_rate")),
        "base_salary": _optional_decimal(payload.get("base_salary")),
        "status": status,
        "is_active": is_active_for_status(status),
    }


# ── GET /api/employees ───────────────────────────────────────────
@router.get("")
def list_employees(
    active_only: bool = False,
    inactive_only: bool = False,
    branch_id: Optional[int] = None,
    job_role: str = "",
    employment_type: str = "",
    status: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Lấy danh sách nhân viên.
    - active_only=true:   chỉ nhân viên đang làm việc (is_active=True)
    - inactive_only=true: chỉ nhân viên đã nghỉ (is_active=False)
    - không truyền gì:    tất cả nhân viên
    """
    q = db.query(Employee)
    if active_only:
        q = q.filter_by(is_active=True)
    elif inactive_only:
        q = q.filter_by(is_active=False)
    if branch_id is not None:
        q = q.filter_by(branch_id=branch_id)
    if job_role:
        q = q.filter_by(job_role=normalize_job_role(job_role))
    if employment_type:
        q = q.filter_by(employment_type=normalize_employment_type(employment_type))
    if status:
        q = q.filter_by(status=normalize_employee_status(status))
    return [_emp_dict(e) for e in q.order_by(Employee.name).all()]

# ── GET /api/employees/{id} ──────────────────────────────────────
@router.get("/{emp_id}")
def get_employee(
    emp_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    return _emp_dict(emp)


# ── POST /api/employees — Tạo nhân viên + upload ảnh ─────────────
@router.post("")
async def create_employee(
    emp_code:   str              = Form(...),
    name:       str              = Form(...),
    branch_id:  Optional[int]    = Form(None),
    full_name:  str              = Form(""),
    department: str              = Form(""),
    position:   str              = Form(""),
    job_role:   str              = Form(""),
    employment_type: str         = Form("full_time"),
    hourly_rate: Optional[Decimal] = Form(None),
    base_salary: Optional[Decimal] = Form(None),
    status:     str              = Form("active"),
    email:      str              = Form(""),
    phone:      str              = Form(""),
    images:     list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if db.query(Employee).filter_by(emp_code=emp_code).first():
        raise HTTPException(400, f"Mã nhân viên '{emp_code}' đã tồn tại")
    phone = _clean_phone(phone)

    cv_images = []
    for upload in images:
        data  = await upload.read()
        nparr = np.frombuffer(data, np.uint8)
        img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is not None:
            cv_images.append(img)

    if not cv_images:
        raise HTTPException(400, "Không có ảnh hợp lệ")

    result = face_engine.register(emp_code, cv_images)
    if not result["success"]:
        raise HTTPException(400, result["message"])

    status_value = normalize_employee_status(status)
    emp = Employee(
        emp_code   = emp_code,
        name       = name,
        full_name  = full_name or name,
        branch_id  = branch_id,
        department = department,
        position   = position,
        job_role   = normalize_job_role(job_role or position),
        employment_type = normalize_employment_type(employment_type),
        hourly_rate = hourly_rate,
        base_salary = base_salary,
        status     = status_value,
        is_active  = is_active_for_status(status_value),
        email      = email,
        phone      = phone,
        face_path  = f"data/faces/{emp_code}",
        avatar_url = f"/data/faces/{emp_code}/0.jpg",
    )
    db.add(emp); db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp), "message": result["message"]}


# ── POST /api/employees/register-from-camera ─────────────────────
@router.post("/register-from-camera")
async def register_from_camera(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    emp_code = payload.get("emp_code", "").strip()
    name     = payload.get("name", "").strip()
    frames   = payload.get("frames", [])

    if not emp_code or not name:
        raise HTTPException(400, "Thiếu mã nhân viên hoặc tên")
    if db.query(Employee).filter_by(emp_code=emp_code).first():
        raise HTTPException(400, f"Mã '{emp_code}' đã tồn tại")
    if not frames:
        raise HTTPException(400, "Không có ảnh nào")
    phone = _clean_phone(payload.get("phone"))

    cv_images = []
    for b64 in frames:
        try:
            img_bytes = base64.b64decode(b64.split(",")[-1])
            nparr = np.frombuffer(img_bytes, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv_images.append(img)
        except Exception:
            continue

    result = face_engine.register(emp_code, cv_images)
    if not result["success"]:
        raise HTTPException(400, result["message"])

    profile = _profile_fields_from_payload(payload)
    emp = Employee(
        emp_code   = emp_code, name=name,
        full_name  = profile["full_name"] or name,
        branch_id  = profile["branch_id"],
        department = profile["department"],
        position   = profile["position"],
        job_role   = profile["job_role"],
        employment_type = profile["employment_type"],
        hourly_rate = profile["hourly_rate"],
        base_salary = profile["base_salary"],
        status     = profile["status"],
        is_active  = profile["is_active"],
        email      = payload.get("email", ""),
        phone      = phone,
        face_path  = f"data/faces/{emp_code}",
        avatar_url = f"/data/faces/{emp_code}/0.jpg",
    )
    db.add(emp); db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp), "message": result["message"]}


# ── POST /api/employees/self-register ────────────────────────────
# NV tự đăng ký: điền form + chụp mặt → tạo Employee + User → gửi xác minh email
@router.post("/self-register")
async def self_register(payload: dict, db: Session = Depends(get_db)):
    emp_code = payload.get("emp_code", "").strip()
    name     = payload.get("name", "").strip()
    email    = payload.get("email", "").strip()
    password = payload.get("password", "")
    frames   = payload.get("frames", [])
    phone    = _clean_phone(payload.get("phone"))

    # ── Validate ──────────────────────────────────────────────────
    if not name:
        raise HTTPException(400, "Thiếu họ tên nhân viên")
    if not email:
        raise HTTPException(400, "Email là bắt buộc để tạo tài khoản")
    if not password or len(password) < 8:
        raise HTTPException(400, "Mật khẩu cần ít nhất 8 ký tự")
    if not any(c.isdigit() for c in password):
        raise HTTPException(400, "Mật khẩu cần có ít nhất 1 chữ số")
    if not frames:
        raise HTTPException(400, "Không có ảnh khuôn mặt nào")

    # ── Kiểm tra trùng lặp ───────────────────────────────────────
    if emp_code:
        if db.query(Employee).filter_by(emp_code=emp_code).first():
            raise HTTPException(400, f"Mã nhân viên '{emp_code}' đã tồn tại")
    else:
        emp_code = _generate_employee_code(db)
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(400, "Email này đã được đăng ký")

    # ── Xử lý ảnh khuôn mặt ─────────────────────────────────────
    import base64
    cv_images = []
    for b64 in frames:
        try:
            img_bytes = base64.b64decode(b64.split(",")[-1])
            nparr = np.frombuffer(img_bytes, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv_images.append(img)
        except Exception:
            continue

    if not cv_images:
        raise HTTPException(400, "Không có ảnh hợp lệ")

    # ── Đăng ký face vector ───────────────────────────────────────
    result = face_engine.register(emp_code, cv_images)
    if not result["success"]:
        raise HTTPException(400, result["message"])

    # ── Tạo Employee ──────────────────────────────────────────────
    profile = _profile_fields_from_payload(payload)
    emp = Employee(
        emp_code   = emp_code,
        name       = name,
        full_name  = profile["full_name"] or name,
        branch_id  = profile["branch_id"],
        department = profile["department"],
        position   = profile["position"],
        job_role   = profile["job_role"],
        employment_type = profile["employment_type"],
        hourly_rate = profile["hourly_rate"],
        base_salary = profile["base_salary"],
        status     = profile["status"],
        is_active  = profile["is_active"],
        email      = email,
        phone      = phone,
        face_path  = f"data/faces/{emp_code}",
        avatar_url = f"/data/faces/{emp_code}/0.jpg",
    )
    db.add(emp)

    # ── Tạo User (chưa active, chưa duyệt) ───────────────────────
    user = User(
        email             = email,
        full_name         = name,
        hashed_password   = hash_password(password),
        role              = "staff",
        is_active         = False,
        is_email_verified = False,
        is_approved       = False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # ── Gửi email xác minh ────────────────────────────────────────
    token = create_verify_token(user.id, db)
    send_verification_email(user.email, user.full_name, token)

    return {
        "success": True,
        "message": "Đăng ký thành công! Kiểm tra email để xác minh tài khoản. Sau khi xác minh, tài khoản sẽ chờ admin/manager phê duyệt.",
        "employee": _emp_dict(emp),
    }
@router.put("/{emp_id}")
def update_employee(emp_id: int, data: EmployeeUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    update_data = data.model_dump(exclude_unset=True)
    if "position" in update_data and "job_role" not in update_data:
        update_data["job_role"] = normalize_job_role(update_data.get("position") or "")
    if "status" in update_data:
        update_data["status"] = normalize_employee_status(update_data["status"])
        update_data["is_active"] = is_active_for_status(update_data["status"])
    elif "is_active" in update_data:
        update_data["status"] = normalize_employee_status(None, update_data["is_active"])
    if "employment_type" in update_data:
        update_data["employment_type"] = normalize_employment_type(update_data["employment_type"])
    if "job_role" in update_data:
        update_data["job_role"] = normalize_job_role(update_data["job_role"] or "")
    if "phone" in update_data:
        update_data["phone"] = _clean_phone(update_data["phone"])

    allowed = {
        "name", "full_name", "branch_id", "department", "position", "job_role",
        "employment_type", "hourly_rate", "base_salary", "email", "phone",
        "status", "is_active",
    }
    for field, val in update_data.items():
        if field in allowed and val is not None:
            setattr(emp, field, val)
    if "is_active" in update_data or "status" in update_data:
        emp.deactivated_at = None if emp.is_active else datetime.now()
    db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp)}


# ── DELETE /api/employees/{id} ───────────────────────────────────
@router.delete("/{emp_id}")
def delete_employee(emp_id: int, hard: bool = False, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    if hard:
        face_engine.delete(emp.emp_code)
        db.delete(emp)
    else:
        emp.is_active = False
        emp.status = "inactive"
        emp.deactivated_at = datetime.now()
    db.commit()
    return {"success": True, "message": "Đã xóa nhân viên"}
