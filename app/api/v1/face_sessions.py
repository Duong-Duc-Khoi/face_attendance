"""Face enrollment ticket APIs used by dashboard and kiosk."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.face_enrollment import FaceEnrollmentSession
from app.services.branch_scope import ensure_branch_access, require_branch_manager_or_admin
from app.services.face_engine import face_engine
from app.services.face_enrollment import (
    FACE_SESSION_ACTIVE_STATUSES,
    decode_base64_frames,
    face_action_label,
    hash_face_ticket,
)
from app.services.kiosk_registry import list_online_kiosks

router = APIRouter(prefix="/api", tags=["face-enrollment"])


class FaceSessionCompleteRequest(BaseModel):
    frames: list[str] = Field(default_factory=list)


def _branch_dict(branch: Branch | None) -> dict | None:
    if not branch:
        return None
    return {
        "id": branch.id,
        "name": branch.name,
        "address": branch.address,
    }


def _employee_dict(emp: Employee, branch: Branch | None = None) -> dict:
    return {
        "id": emp.id,
        "branch_id": emp.branch_id,
        "emp_code": emp.emp_code,
        "name": emp.name,
        "full_name": emp.full_name or emp.name,
        "department": emp.department,
        "position": emp.position,
        "email": emp.email,
        "phone": emp.phone,
        "avatar_url": emp.avatar_url,
        "has_face": emp.emp_code in face_engine.embeddings,
        "branch": _branch_dict(branch),
    }


def _session_payload(session: FaceEnrollmentSession, emp: Employee, branch: Branch | None) -> dict:
    return {
        "id": session.id,
        "action": session.action,
        "action_label": face_action_label(session.action),
        "status": session.status,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "employee": _employee_dict(emp, branch),
    }


def _load_active_session(db: Session, ticket: str) -> tuple[FaceEnrollmentSession, Employee, Branch | None]:
    session = (
        db.query(FaceEnrollmentSession)
        .filter_by(token_hash=hash_face_ticket(ticket.strip()))
        .first()
    )
    if not session:
        raise HTTPException(404, "Phiên đăng ký khuôn mặt không tồn tại")
    now = datetime.now()
    if session.expires_at and session.expires_at < now and session.status in FACE_SESSION_ACTIVE_STATUSES:
        session.status = "expired"
        session.updated_at = now
        db.commit()
    if session.status == "expired":
        raise HTTPException(410, "Phiên đăng ký khuôn mặt đã hết hạn")
    if session.status == "completed":
        raise HTTPException(400, "Phiên đăng ký khuôn mặt đã hoàn tất")
    if session.status not in FACE_SESSION_ACTIVE_STATUSES:
        raise HTTPException(400, "Phiên đăng ký khuôn mặt không còn hiệu lực")

    emp = db.query(Employee).filter_by(id=session.employee_id).first()
    if not emp:
        raise HTTPException(404, "Nhân viên không tồn tại")
    branch = db.query(Branch).filter_by(id=emp.branch_id).first() if emp.branch_id else None
    return session, emp, branch


@router.get("/kiosks/online")
async def online_kiosks(
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    allowed = require_branch_manager_or_admin(db, current_user)
    if branch_id is not None:
        ensure_branch_access(db, current_user, branch_id)
    rows = await list_online_kiosks(branch_id)
    if current_user.role != "admin":
        rows = [row for row in rows if row.get("branch_id") in allowed]
    return {"kiosks": rows, "total": len(rows)}


@router.get("/face-sessions/{ticket}")
def get_face_session(ticket: str, db: Session = Depends(get_db)):
    session, emp, branch = _load_active_session(db, ticket)
    now = datetime.now()
    if session.status == "pending":
        session.status = "in_progress"
        session.started_at = now
        session.updated_at = now
        db.commit()
        db.refresh(session)
    return _session_payload(session, emp, branch)


@router.post("/face-sessions/{ticket}/complete")
def complete_face_session(
    ticket: str,
    body: FaceSessionCompleteRequest,
    db: Session = Depends(get_db),
):
    session, emp, branch = _load_active_session(db, ticket)
    if len(body.frames) < 3:
        raise HTTPException(400, "Cần chụp ít nhất 3 góc khuôn mặt")
    cv_images = decode_base64_frames(body.frames)
    if not cv_images:
        raise HTTPException(400, "Không có ảnh khuôn mặt hợp lệ")

    result = face_engine.register(emp.emp_code, cv_images)
    if not result["success"]:
        raise HTTPException(400, result["message"])

    now = datetime.now()
    emp.face_path = f"data/faces/{emp.emp_code}"
    emp.avatar_url = f"/data/faces/{emp.emp_code}/0.jpg"
    emp.updated_at = now
    session.status = "completed"
    session.completed_at = now
    session.updated_at = now
    db.commit()
    db.refresh(emp)
    db.refresh(session)
    return {
        "success": True,
        "message": f"{face_action_label(session.action)} thành công cho {emp.name} ({emp.emp_code})",
        "employee": _employee_dict(emp, branch),
        "session": _session_payload(session, emp, branch),
    }
