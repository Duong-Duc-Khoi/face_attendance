"""
Mobile attendance endpoints.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.services.mobile_attendance import mobile_context, process_mobile_attempt

router = APIRouter(prefix="/api/mobile/attendance", tags=["mobile-attendance"])


class MobileAttendanceAttemptRequest(BaseModel):
    branch_id: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    device_id: str = ""
    frames: list[str] = Field(default_factory=list)


@router.get("/context")
def api_mobile_attendance_context(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return mobile_context(db, current_user)


@router.post("/attempt")
def api_mobile_attendance_attempt(
    body: MobileAttendanceAttemptRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    meta = {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
        "sec_ch_ua_mobile": request.headers.get("sec-ch-ua-mobile", ""),
    }
    return process_mobile_attempt(db, current_user, body.model_dump(), meta)
