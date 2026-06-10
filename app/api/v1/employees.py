"""
app/api/v1/employees.py
CRUD nhân viên + đăng ký khuôn mặt.

Thay đổi:
  - Import từ app.models, app.core, app.services thay vì app.database/face_engine
  - Schemas Pydantic tách ra app/schemas/employee.py
"""

import base64
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.face_enrollment import FaceEnrollmentSession
from app.models.user import User
from app.schemas.employee import (
    EmployeeUpdate,
    JOB_ROLE_LABELS,
    STORE_ROLE_LABELS,
    is_active_for_status,
    normalize_employee_status,
    normalize_employment_type,
    normalize_job_role,
    normalize_job_roles,
    normalize_store_role,
)
from app.services.face_engine import face_engine
from app.core.security import hash_password, get_current_user
from app.services.auth_service import create_verify_token, send_verification_email
from app.services.employee_branch_history import transfer_employee_to_branch
from app.services.face_enrollment import (
    FACE_SESSION_ACTIVE_STATUSES,
    FACE_SESSION_TTL_MINUTES,
    face_action_label,
    generate_face_ticket,
)
from app.services.kiosk_registry import list_online_kiosks, send_to_kiosk
from app.services.branch_scope import (
    BRANCH_MANAGER_STORE_ROLES,
    default_branch_id_for_write,
    ensure_branch_access,
    require_admin,
    require_branch_manager_or_admin,
    scoped_branch_filter,
)

router = APIRouter(prefix="/api/employees", tags=["employees"])


class TransferBranchRequest(BaseModel):
    branch_id: int
    effective_date: Optional[str] = None
    note: Optional[str] = ""


class FaceSessionCreateRequest(BaseModel):
    kiosk_id: Optional[str] = None


# ── Helper ───────────────────────────────────────────────────────
def _money(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _employee_job_role(e: Employee) -> str:
    return e.job_role or normalize_job_role(e.position or "")


def _employee_job_roles(e: Employee) -> list[str]:
    try:
        roles = json.loads(e.job_roles or "[]")
    except Exception:
        roles = []
    roles = normalize_job_roles(roles)
    primary = _employee_job_role(e)
    if primary and primary not in roles:
        roles.insert(0, primary)
    return roles


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


def _ensure_store_role_slot(
    db: Session,
    branch_id: int | None,
    store_role: str,
    current_emp_id: int | None = None,
) -> None:
    if store_role not in ("store_manager", "assistant_manager"):
        return
    if branch_id is None:
        label = STORE_ROLE_LABELS.get(store_role, store_role)
        raise HTTPException(400, f"{label} phải thuộc một cửa hàng cụ thể")
    q = db.query(Employee).filter(
        Employee.branch_id == branch_id,
        Employee.store_role == store_role,
        Employee.is_active == True,
    )
    if current_emp_id is not None:
        q = q.filter(Employee.id != current_emp_id)
    existing = q.first()
    if existing:
        label = STORE_ROLE_LABELS.get(store_role, store_role)
        raise HTTPException(400, f"Cửa hàng này đã có {label}: {existing.name} ({existing.emp_code})")


def _require_employee_access(db: Session, current_user: User, emp: Employee) -> None:
    ensure_branch_access(db, current_user, emp.branch_id)


def _is_store_management_employee(emp: Employee) -> bool:
    return (emp.store_role or "staff") in BRANCH_MANAGER_STORE_ROLES


def _is_current_user_employee(current_user: User, emp: Employee) -> bool:
    if emp.user_id and emp.user_id == current_user.id:
        return True
    emp_email = (emp.email or "").strip().lower()
    user_email = (current_user.email or "").strip().lower()
    return bool(emp_email and user_email and emp_email == user_email)


def _status_change_requested(emp: Employee, update_data: dict) -> bool:
    if "status" not in update_data and "is_active" not in update_data:
        return False
    current_status = normalize_employee_status(emp.status, emp.is_active)
    current_active = is_active_for_status(current_status)
    next_status = update_data.get("status", current_status)
    next_active = update_data.get("is_active", current_active)
    return normalize_employee_status(next_status, next_active) != current_status or bool(next_active) != current_active


def _ensure_manager_can_change_employee_status(current_user: User, emp: Employee, update_data: dict) -> None:
    if current_user.role == "admin" or not _status_change_requested(emp, update_data):
        return
    if _is_current_user_employee(current_user, emp):
        raise HTTPException(403, "Không thể tự thay đổi trạng thái làm việc của chính mình")
    if _is_store_management_employee(emp):
        raise HTTPException(403, "Chỉ admin được thay đổi trạng thái của Cửa hàng trưởng/Cửa hàng phó")


def _ensure_manager_can_deactivate_employee(current_user: User, emp: Employee) -> None:
    if current_user.role == "admin":
        return
    if _is_current_user_employee(current_user, emp):
        raise HTTPException(403, "Không thể tự vô hiệu hóa hồ sơ nhân viên của chính mình")
    if _is_store_management_employee(emp):
        raise HTTPException(403, "Chỉ admin được vô hiệu hóa Cửa hàng trưởng/Cửa hàng phó")


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
        "store_role": e.store_role or "staff",
        "store_role_label": STORE_ROLE_LABELS.get(e.store_role or "staff", e.store_role or "staff"),
        "job_role":   _employee_job_role(e),
        "job_role_label": JOB_ROLE_LABELS.get(_employee_job_role(e), _employee_job_role(e)),
        "job_roles":  _employee_job_roles(e),
        "job_role_labels": [JOB_ROLE_LABELS.get(role, role) for role in _employee_job_roles(e)],
        "employment_type": e.employment_type or "full_time",
        "hourly_rate": _money(e.hourly_rate),
        "base_salary": _money(e.base_salary),
        "email":      e.email,
        "phone":      e.phone,
        "avatar_url": e.avatar_url,
        "has_face":   e.emp_code in face_engine.embeddings,
        "status":     e.status or ("active" if e.is_active else "inactive"),
        "is_active":  e.is_active,
        "created_at": e.created_at.strftime("%d/%m/%Y") if e.created_at else "",
    }


def _profile_fields_from_payload(payload: dict) -> dict:
    position = (payload.get("position") or "").strip()
    job_role = normalize_job_role(payload.get("job_role") or position)
    job_roles = normalize_job_roles(payload.get("job_roles") or job_role)
    if job_role and job_role not in job_roles:
        job_roles.insert(0, job_role)
    if not job_role and job_roles:
        job_role = job_roles[0]
    status = normalize_employee_status(payload.get("status"), payload.get("is_active"))
    return {
        "branch_id": _optional_int(payload.get("branch_id")),
        "full_name": (payload.get("full_name") or payload.get("name") or "").strip(),
        "department": (payload.get("department") or "").strip(),
        "position": position,
        "store_role": normalize_store_role(payload.get("store_role")),
        "job_role": job_role,
        "job_roles": job_roles,
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
    if employment_type:
        q = q.filter_by(employment_type=normalize_employment_type(employment_type))
    if status:
        q = q.filter_by(status=normalize_employee_status(status))
    allowed = scoped_branch_filter(db, current_user)
    if allowed is not None:
        q = q.filter(Employee.branch_id.in_(allowed))
    rows = q.order_by(Employee.name).all()
    if job_role:
        wanted = normalize_job_role(job_role)
        rows = [e for e in rows if wanted in _employee_job_roles(e)]
    return [_emp_dict(e) for e in rows]

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
    _require_employee_access(db, current_user, emp)
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
    store_role: str              = Form("staff"),
    job_role:   str              = Form(""),
    job_roles:  str              = Form(""),
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
    require_branch_manager_or_admin(db, current_user)
    branch_id = default_branch_id_for_write(db, current_user, branch_id)
    ensure_branch_access(db, current_user, branch_id)
    store_role_value = normalize_store_role(store_role)
    if current_user.role != "admin" and store_role_value != "staff":
        raise HTTPException(403, "Chỉ admin được gán Cửa hàng trưởng/Cửa hàng phó")
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
    role_list = normalize_job_roles(job_roles or job_role or position)
    primary_role = normalize_job_role(job_role or position) or (role_list[0] if role_list else "")
    if primary_role and primary_role not in role_list:
        role_list.insert(0, primary_role)
    _ensure_store_role_slot(db, branch_id, store_role_value)
    emp = Employee(
        emp_code   = emp_code,
        name       = name,
        full_name  = full_name or name,
        branch_id  = branch_id,
        department = department,
        position   = position,
        store_role = store_role_value,
        job_role   = primary_role,
        job_roles  = json.dumps(role_list, ensure_ascii=False),
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
    require_branch_manager_or_admin(db, current_user)
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
    profile["branch_id"] = default_branch_id_for_write(db, current_user, profile["branch_id"])
    ensure_branch_access(db, current_user, profile["branch_id"])
    if current_user.role != "admin" and profile["store_role"] != "staff":
        raise HTTPException(403, "Chỉ admin được gán Cửa hàng trưởng/Cửa hàng phó")
    _ensure_store_role_slot(db, profile["branch_id"], profile["store_role"])
    emp = Employee(
        emp_code   = emp_code, name=name,
        full_name  = profile["full_name"] or name,
        branch_id  = profile["branch_id"],
        department = profile["department"],
        position   = profile["position"],
        store_role = profile["store_role"],
        job_role   = profile["job_role"],
        job_roles  = json.dumps(profile["job_roles"], ensure_ascii=False),
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
    branch_id = _optional_int(payload.get("branch_id"))
    if branch_id is None:
        raise HTTPException(400, "Vui lòng chọn chi nhánh")
    branch = db.query(Branch).filter_by(id=branch_id, is_active=True).first()
    if not branch:
        raise HTTPException(404, "Chi nhánh không tồn tại hoặc đã ngừng hoạt động")

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
    profile["store_role"] = "staff"
    emp = Employee(
        emp_code   = emp_code,
        name       = name,
        full_name  = profile["full_name"] or name,
        branch_id  = profile["branch_id"],
        department = profile["department"],
        position   = profile["position"],
        store_role = profile["store_role"],
        job_role   = profile["job_role"],
        job_roles  = json.dumps(profile["job_roles"], ensure_ascii=False),
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


@router.post("/{emp_id}/transfer-branch")
def transfer_employee_branch(
    emp_id: int,
    body: TransferBranchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_admin(current_user)
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")

    target_branch = db.query(Branch).filter_by(id=body.branch_id, is_active=True).first()
    if not target_branch:
        raise HTTPException(404, "Chi nhánh không tồn tại hoặc đã ngừng hoạt động")

    try:
        effective_date = date.fromisoformat(body.effective_date) if body.effective_date else date.today()
    except Exception:
        raise HTTPException(400, "effective_date phải định dạng YYYY-MM-DD")
    if effective_date > date.today():
        raise HTTPException(400, "Không thể đặt ngày chuyển trong tương lai")

    from_branch_id = emp.branch_id
    if from_branch_id == target_branch.id:
        raise HTTPException(400, "Nhân viên đã thuộc chi nhánh này")

    store_role = emp.store_role or "staff"
    if emp.is_active:
        _ensure_store_role_slot(db, target_branch.id, store_role, emp.id)

    from_branch_id, cancelled_count = transfer_employee_to_branch(
        db,
        emp,
        target_branch,
        effective_date=effective_date,
        changed_by=current_user.email or current_user.full_name or "",
        note=(body.note or "").strip(),
    )
    db.commit()
    db.refresh(emp)
    return {
        "success": True,
        "employee": _emp_dict(emp),
        "from_branch_id": from_branch_id,
        "to_branch_id": target_branch.id,
        "effective_date": effective_date.isoformat(),
        "cancelled_assignments": cancelled_count,
    }


@router.post("/{emp_id}/face-session")
async def create_employee_face_session(
    emp_id: int,
    body: FaceSessionCreateRequest | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    _require_employee_access(db, current_user, emp)
    if not emp.branch_id:
        raise HTTPException(400, "Nhân viên chưa thuộc chi nhánh nào")

    now = datetime.now()
    (
        db.query(FaceEnrollmentSession)
        .filter(
            FaceEnrollmentSession.employee_id == emp.id,
            FaceEnrollmentSession.status.in_(FACE_SESSION_ACTIVE_STATUSES),
        )
        .update({"status": "cancelled", "updated_at": now}, synchronize_session=False)
    )

    ticket, token_hash = generate_face_ticket()
    action = "update_face" if emp.emp_code in face_engine.embeddings else "register_face"
    kiosk_id = (body.kiosk_id if body else None) or ""
    session = FaceEnrollmentSession(
        employee_id=emp.id,
        branch_id=emp.branch_id,
        token_hash=token_hash,
        action=action,
        status="pending",
        kiosk_id=kiosk_id,
        created_by_id=current_user.id,
        expires_at=now + timedelta(minutes=FACE_SESSION_TTL_MINUTES),
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    online = await list_online_kiosks(emp.branch_id)
    selected = None
    if kiosk_id:
        selected = next((row for row in online if row["kiosk_id"] == kiosk_id), None)
    elif len(online) == 1:
        selected = online[0]
        session.kiosk_id = selected["kiosk_id"]
        db.commit()
        db.refresh(session)

    register_path = f"/register?ticket={ticket}&branch_id={emp.branch_id}"
    sent = False
    if selected:
        sent = await send_to_kiosk(selected["kiosk_id"], {
            "type": "open_register_ticket",
            "ticket": ticket,
            "register_path": register_path,
            "action": action,
            "action_label": face_action_label(action),
            "employee": _emp_dict(emp),
        })

    return {
        "success": True,
        "ticket": ticket,
        "register_path": register_path,
        "sent_to_kiosk": sent,
        "kiosk": selected,
        "online_kiosks": online,
        "session": {
            "id": session.id,
            "action": action,
            "action_label": face_action_label(action),
            "status": session.status,
            "expires_at": session.expires_at.isoformat(),
        },
        "employee": _emp_dict(emp),
    }


@router.put("/{emp_id}")
def update_employee(emp_id: int, data: EmployeeUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    _require_employee_access(db, current_user, emp)
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
    if "job_roles" in update_data:
        update_data["job_roles"] = json.dumps(normalize_job_roles(update_data["job_roles"]), ensure_ascii=False)
    if "store_role" in update_data:
        update_data["store_role"] = normalize_store_role(update_data["store_role"])
        if current_user.role != "admin" and update_data["store_role"] != (emp.store_role or "staff"):
            raise HTTPException(403, "Chỉ admin được đổi cấp bậc quản lý cửa hàng")
    if "job_role" in update_data and "job_roles" not in update_data:
        role_list = _employee_job_roles(emp)
        if update_data["job_role"] and update_data["job_role"] not in role_list:
            role_list.insert(0, update_data["job_role"])
        update_data["job_roles"] = json.dumps(role_list, ensure_ascii=False)
    if "phone" in update_data:
        update_data["phone"] = _clean_phone(update_data["phone"])
    if "branch_id" in update_data:
        if update_data["branch_id"] != emp.branch_id:
            raise HTTPException(400, "Vui lòng dùng chức năng Chuyển cửa hàng để đổi chi nhánh")
        update_data.pop("branch_id", None)
    next_branch_id = update_data.get("branch_id", emp.branch_id)
    next_store_role = update_data.get("store_role", emp.store_role or "staff")
    next_active = update_data.get("is_active", emp.is_active)
    if next_active:
        _ensure_store_role_slot(db, next_branch_id, next_store_role, emp.id)
    ensure_branch_access(db, current_user, next_branch_id)
    _ensure_manager_can_change_employee_status(current_user, emp, update_data)

    allowed = {
        "name", "full_name", "branch_id", "department", "position", "store_role", "job_role", "job_roles",
        "employment_type", "hourly_rate", "base_salary", "email", "phone",
        "status", "is_active",
    }
    for field, val in update_data.items():
        if field in allowed and (val is not None or field == "branch_id"):
            setattr(emp, field, val)
    if "is_active" in update_data or "status" in update_data:
        emp.deactivated_at = None if emp.is_active else datetime.now()
    db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp)}


# ── DELETE /api/employees/{id} ───────────────────────────────────
@router.delete("/{emp_id}")
def delete_employee(emp_id: int, hard: bool = False, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if hard:
        require_admin(current_user)
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    _require_employee_access(db, current_user, emp)
    if hard:
        face_engine.delete(emp.emp_code)
        db.delete(emp)
    else:
        _ensure_manager_can_deactivate_employee(current_user, emp)
        emp.is_active = False
        emp.status = "inactive"
        emp.deactivated_at = datetime.now()
    db.commit()
    return {"success": True, "message": "Đã xóa nhân viên"}
