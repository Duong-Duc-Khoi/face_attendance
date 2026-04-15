import os
import cv2
import base64
import numpy as np
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from app.database import SessionLocal, Employee
from app.face_engine import face_engine
from app.notify import notify_account_created
router = APIRouter(prefix="/api/employees", tags=["employees"])
import secrets
import string
import smtplib
from email.mime.text import MIMEText
from app.database import SessionLocal, Employee, Account  # thêm Account
from passlib.context import CryptContext

pwd_ctx = CryptContext(schemes=["bcrypt"])

def _gen_password(length=6) -> str:
    """Gen mật khẩu tạm: chữ hoa + thường + số, VD: Nv3xKp8mQa"""
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

# ── Schemas ──
class EmployeeCreate(BaseModel):
    emp_code:   str
    name:       str
    department: Optional[str] = ""
    position:   Optional[str] = ""
    email:      Optional[str] = ""
    phone:      Optional[str] = ""


class EmployeeUpdate(BaseModel):
    name:       Optional[str] = None
    department: Optional[str] = None
    position:   Optional[str] = None
    email:      Optional[str] = None
    phone:      Optional[str] = None
    is_active:  Optional[bool] = None


# ──────────────────────────────────────────
# GET /api/employees  — Danh sách nhân viên
# ──────────────────────────────────────────
@router.get("")
def list_employees(active_only: bool = True):
    db = SessionLocal()
    try:
        q = db.query(Employee)
        if active_only:
            q = q.filter_by(is_active=True)
        emps = q.order_by(Employee.name).all()
        return [_emp_dict(e) for e in emps]
    finally:
        db.close()


# ──────────────────────────────────────────
# GET /api/employees/{id}
# ──────────────────────────────────────────
@router.get("/{emp_id}")
def get_employee(emp_id: int):
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if not emp:
            raise HTTPException(404, "Nhân viên không tồn tại")
        return _emp_dict(emp)
    finally:
        db.close()


# ──────────────────────────────────────────
# POST /api/employees  — Tạo nhân viên + đăng ký khuôn mặt
# ──────────────────────────────────────────
@router.post("")
async def create_employee(
    emp_code:   str = Form(...),
    name:       str = Form(...),
    department: str = Form(""),
    position:   str = Form(""),
    email:      str = Form(""),
    phone:      str = Form(""),
    images:     list[UploadFile] = File(...)   # Danh sách ảnh từ form
):
    db = SessionLocal()
    try:
        # Kiểm tra mã NV trùng
        if db.query(Employee).filter_by(emp_code=emp_code).first():
            raise HTTPException(400, f"Mã nhân viên '{emp_code}' đã tồn tại")

        # Đọc ảnh upload → numpy array
        cv_images = []
        for upload in images:
            data = await upload.read()
            nparr = np.frombuffer(data, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv_images.append(img)

        if not cv_images:
            raise HTTPException(400, "Không có ảnh hợp lệ")

        # Đăng ký khuôn mặt với AI
        result = face_engine.register(emp_code, cv_images)
        if not result["success"]:
            raise HTTPException(400, result["message"])

        # Lưu nhân viên vào DB
        avatar_url = f"/data/faces/{emp_code}/0.jpg"
        emp = Employee(
            emp_code   = emp_code,
            name       = name,
            department = department,
            position   = position,
            email      = email,
            phone      = phone,
            face_path  = f"data/faces/{emp_code}",
            avatar_url = avatar_url,
        )
        db.add(emp); db.commit(); db.refresh(emp)

        return {
            "success":  True,
            "employee": _emp_dict(emp),
            "message":  result["message"],
        }
    finally:
        db.close()


# ──────────────────────────────────────────
# POST /api/employees/register-from-camera
# Đăng ký từ ảnh base64 chụp trực tiếp trên kiosk
# ──────────────────────────────────────────
@router.post("/register-from-camera")
async def register_from_camera(payload: dict):
    """
    Payload: { emp_code, name, department, position, email, phone, frames: [base64,...] }
    """
    db = SessionLocal()
    try:
        emp_code = payload.get("emp_code", "").strip()
        name     = payload.get("name", "").strip()
        frames   = payload.get("frames", [])   # list base64 JPEG strings

        if not emp_code or not name:
            raise HTTPException(400, "Thiếu mã nhân viên hoặc tên")
        if db.query(Employee).filter_by(emp_code=emp_code).first():
            raise HTTPException(400, f"Mã '{emp_code}' đã tồn tại")
        if not frames:
            raise HTTPException(400, "Không có ảnh nào")

        # Decode base64 → numpy array
        cv_images = []
        for b64 in frames:
            try:
                b64_data = b64.split(",")[-1]   # Bỏ "data:image/jpeg;base64,"
                img_bytes = base64.b64decode(b64_data)
                nparr     = np.frombuffer(img_bytes, np.uint8)
                img       = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if img is not None:
                    cv_images.append(img)
            except Exception:
                continue

        result = face_engine.register(emp_code, cv_images)
        if not result["success"]:
            raise HTTPException(400, result["message"])

        emp = Employee(
            emp_code   = emp_code,
            name       = name,
            department = payload.get("department", ""),
            position   = payload.get("position", ""),
            email      = payload.get("email", ""),
            phone      = payload.get("phone", ""),
            face_path  = f"data/faces/{emp_code}",
            avatar_url = f"/data/faces/{emp_code}/0.jpg",
        )
        db.add(emp); db.commit(); db.refresh(emp)
        email = payload.get("email", "").strip()
        if email:
            temp_pw = _gen_password()
            account = Account(
                employee_id          = emp.id,
                username             = email,
                hashed_password      = pwd_ctx.hash(temp_pw),
                role                 = "employee",
                must_change_password = True,
            )
            db.add(account); db.commit()
            _send_email(email, name, emp_code, temp_pw)

        return {"success": True, "employee": _emp_dict(emp), "message": result["message"]}
    finally:
        db.close()


# ──────────────────────────────────────────
# PUT /api/employees/{id}  — Cập nhật thông tin
# ──────────────────────────────────────────
@router.put("/{emp_id}")
def update_employee(emp_id: int, data: EmployeeUpdate):
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if not emp:
            raise HTTPException(404, "Nhân viên không tồn tại")
        for field, val in data.dict(exclude_none=True).items():
            setattr(emp, field, val)
        db.commit(); db.refresh(emp)
        return {"success": True, "employee": _emp_dict(emp)}
    finally:
        db.close()


# ──────────────────────────────────────────
# DELETE /api/employees/{id}  — Xóa / vô hiệu hóa
# ──────────────────────────────────────────
@router.delete("/{emp_id}")
def delete_employee(emp_id: int, hard: bool = False):
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if not emp:
            raise HTTPException(404, "Nhân viên không tồn tại")
        if hard:
            face_engine.delete(emp.emp_code)
            db.delete(emp)
        else:
            emp.is_active = False   # Soft delete
        db.commit()
        return {"success": True, "message": "Đã xóa nhân viên"}
    finally:
        db.close()


def _emp_dict(e: Employee) -> dict:
    return {
        "id":          e.id,
        "emp_code":    e.emp_code,
        "name":        e.name,
        "department":  e.department,
        "position":    e.position,
        "email":       e.email,
        "phone":       e.phone,
        "avatar_url":  e.avatar_url,
        "is_active":   e.is_active,
        "created_at":  e.created_at.strftime("%d/%m/%Y") if e.created_at else "",
    }
