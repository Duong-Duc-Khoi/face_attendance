"""
app/api/v1/employees.py
CRUD nhân viên + đăng ký khuôn mặt.

Thay đổi:
  - Import từ app.models, app.core, app.services thay vì app.database/face_engine
  - Schemas Pydantic tách ra app/schemas/employee.py
"""

import base64

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.employee import Employee
from app.services.face_engine import face_engine

router = APIRouter(prefix="/api/employees", tags=["employees"])


# ── Helper ───────────────────────────────────────────────────────
def _emp_dict(e: Employee) -> dict:
    return {
        "id":         e.id,
        "emp_code":   e.emp_code,
        "name":       e.name,
        "department": e.department,
        "position":   e.position,
        "email":      e.email,
        "phone":      e.phone,
        "avatar_url": e.avatar_url,
        "is_active":  e.is_active,
        "created_at": e.created_at.strftime("%d/%m/%Y") if e.created_at else "",
    }


# ── GET /api/employees ───────────────────────────────────────────
@router.get("")
def list_employees(active_only: bool = True, db: Session = Depends(get_db)):
    q = db.query(Employee)
    if active_only:
        q = q.filter_by(is_active=True)
    return [_emp_dict(e) for e in q.order_by(Employee.name).all()]


# ── GET /api/employees/{id} ──────────────────────────────────────
@router.get("/{emp_id}")
def get_employee(emp_id: int, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    return _emp_dict(emp)


# ── POST /api/employees — Tạo nhân viên + upload ảnh ─────────────
@router.post("")
async def create_employee(
    emp_code:   str              = Form(...),
    name:       str              = Form(...),
    department: str              = Form(""),
    position:   str              = Form(""),
    email:      str              = Form(""),
    phone:      str              = Form(""),
    images:     list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    if db.query(Employee).filter_by(emp_code=emp_code).first():
        raise HTTPException(400, f"Mã nhân viên '{emp_code}' đã tồn tại")

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

    emp = Employee(
        emp_code   = emp_code, name=name, department=department,
        position   = position, email=email, phone=phone,
        face_path  = f"data/faces/{emp_code}",
        avatar_url = f"/data/faces/{emp_code}/0.jpg",
    )
    db.add(emp); db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp), "message": result["message"]}


# ── POST /api/employees/register-from-camera ─────────────────────
@router.post("/register-from-camera")
async def register_from_camera(payload: dict, db: Session = Depends(get_db)):
    emp_code = payload.get("emp_code", "").strip()
    name     = payload.get("name", "").strip()
    frames   = payload.get("frames", [])

    if not emp_code or not name:
        raise HTTPException(400, "Thiếu mã nhân viên hoặc tên")
    if db.query(Employee).filter_by(emp_code=emp_code).first():
        raise HTTPException(400, f"Mã '{emp_code}' đã tồn tại")
    if not frames:
        raise HTTPException(400, "Không có ảnh nào")

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

    emp = Employee(
        emp_code   = emp_code, name=name,
        department = payload.get("department", ""),
        position   = payload.get("position", ""),
        email      = payload.get("email", ""),
        phone      = payload.get("phone", ""),
        face_path  = f"data/faces/{emp_code}",
        avatar_url = f"/data/faces/{emp_code}/0.jpg",
    )
    db.add(emp); db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp), "message": result["message"]}


# ── PUT /api/employees/{id} ──────────────────────────────────────
@router.put("/{emp_id}")
def update_employee(emp_id: int, data: dict, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    allowed = {"name", "department", "position", "email", "phone", "is_active"}
    for field, val in data.items():
        if field in allowed and val is not None:
            setattr(emp, field, val)
    db.commit(); db.refresh(emp)
    return {"success": True, "employee": _emp_dict(emp)}


# ── DELETE /api/employees/{id} ───────────────────────────────────
@router.delete("/{emp_id}")
def delete_employee(emp_id: int, hard: bool = False, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter_by(id=emp_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    if hard:
        face_engine.delete(emp.emp_code)
        db.delete(emp)
    else:
        emp.is_active = False
    db.commit()
    return {"success": True, "message": "Đã xóa nhân viên"}
