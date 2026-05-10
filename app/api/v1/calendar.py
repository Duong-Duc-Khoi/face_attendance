"""
app/api/v1/calendar.py
Endpoints quản lý lịch làm việc.
"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.calendar import WorkCalendar
from app.models.user import User
from app.services.work_calendar import get_calendar_month, get_calendar_day

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

VALID_DAY_TYPES = ("full", "half_am", "half_pm", "off", "holiday", "overtime")


# ── GET /api/calendar?year=&month= ──────────────────────────────

@router.get("")
def get_calendar(
    year: int = 0, month: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = date.today()
    y = year  or today.year
    m = month or today.month
    days = get_calendar_month(y, m, db)
    return {
        "year": y, "month": m,
        "days": days,
        "defaults": {
            "work_days":  settings.WORK_DAYS,
            "work_start": settings.WORK_START,
            "work_end":   settings.WORK_END,
            "late_threshold_minutes": settings.LATE_THRESHOLD_MINUTES,
            "half_day_cutoff": settings.HALF_DAY_CUTOFF,
        }
    }


# ── GET /api/calendar/config ─────────────────────────────────────

@router.get("/config")
def get_config(current_user: User = Depends(get_current_user)):
    return {
        "work_days":              settings.WORK_DAYS,
        "work_start":             settings.WORK_START,
        "work_end":               settings.WORK_END,
        "late_threshold_minutes": settings.LATE_THRESHOLD_MINUTES,
        "half_day_cutoff":        settings.HALF_DAY_CUTOFF,
        "notify_leave_cancel":    settings.NOTIFY_LEAVE_CANCEL,
    }


# ── POST /api/calendar/config — Cập nhật cấu hình mặc định ──────

@router.post("/config")
def update_config(payload: dict,
                  db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(403, "Chỉ Admin mới được sửa cấu hình")

    import os, re
    env_path = None
    for candidate in ["../.env", ".env", "../../.env"]:
        if os.path.exists(candidate):
            env_path = candidate
            break

    if not env_path:
        raise HTTPException(500, "Không tìm thấy file .env để lưu cấu hình")

    mapping = {
        "work_days":              "WORK_DAYS",
        "work_start":             "WORK_START",
        "work_end":               "WORK_END",
        "late_threshold_minutes": "LATE_THRESHOLD_MINUTES",
        "half_day_cutoff":        "HALF_DAY_CUTOFF",
        "notify_leave_cancel":    "NOTIFY_LEAVE_CANCEL",
    }

    with open(env_path, "r") as f:
        lines = f.readlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        replaced = False
        for field, env_key in mapping.items():
            if field in payload and line.startswith(env_key + "="):
                val = payload[field]
                new_lines.append(f"{env_key}={val}\n")
                replaced = True
                updated_keys.add(env_key)
                break
        if not replaced:
            new_lines.append(line)

    # Thêm key chưa có trong .env
    for field, env_key in mapping.items():
        if field in payload and env_key not in updated_keys:
            new_lines.append(f"{env_key}={payload[field]}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)

    # Reload settings runtime
    for field, env_key in mapping.items():
        if field in payload:
            val = payload[field]
            if hasattr(settings, field):
                try:
                    attr_type = type(getattr(settings, field))
                    setattr(settings, field, attr_type(val))
                except Exception:
                    setattr(settings, field, val)
            env_name = field.upper()
            if hasattr(settings, env_name):
                try:
                    attr_type = type(getattr(settings, env_name))
                    setattr(settings, env_name, attr_type(val))
                except Exception:
                    setattr(settings, env_name, val)

    return {"success": True, "message": "Đã lưu cấu hình"}


# ── POST /api/calendar/day — Tạo/sửa 1 ngày đặc biệt ───────────

@router.post("/day")
def upsert_day(payload: dict,
               db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(403, "Chỉ Manager/Admin mới được sửa lịch")

    d_str    = payload.get("date", "")
    day_type = payload.get("day_type", "")
    if not d_str:
        raise HTTPException(400, "Thiếu trường 'date'")
    if day_type not in VALID_DAY_TYPES:
        raise HTTPException(400, f"day_type phải là một trong: {VALID_DAY_TYPES}")

    try:
        d = date.fromisoformat(d_str)
    except Exception:
        raise HTTPException(400, "Ngày không hợp lệ")

    existing = db.query(WorkCalendar).filter_by(date=d).first()
    if existing:
        existing.day_type   = day_type
        existing.work_start = payload.get("work_start") or None
        existing.work_end   = payload.get("work_end")   or None
        existing.label      = payload.get("label", "")
        existing.created_by = current_user.email
        db.commit()
        db.refresh(existing)
        return {"success": True, "day": _cal_dict(existing)}
    else:
        cal = WorkCalendar(
            date       = d,
            day_type   = day_type,
            work_start = payload.get("work_start") or None,
            work_end   = payload.get("work_end")   or None,
            label      = payload.get("label", ""),
            created_by = current_user.email,
        )
        db.add(cal)
        db.commit()
        db.refresh(cal)
        return {"success": True, "day": _cal_dict(cal)}


# ── POST /api/calendar/batch — Tạo nhiều ngày cùng lúc ──────────

@router.post("/batch")
def batch_upsert(payload: dict,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(403, "Không có quyền")

    entries = payload.get("days", [])
    if not entries:
        raise HTTPException(400, "Danh sách ngày trống")

    saved = []
    for entry in entries:
        d_str    = entry.get("date", "")
        day_type = entry.get("day_type", "full")
        if day_type not in VALID_DAY_TYPES:
            continue
        try:
            d = date.fromisoformat(d_str)
        except Exception:
            continue

        existing = db.query(WorkCalendar).filter_by(date=d).first()
        if existing:
            existing.day_type   = day_type
            existing.work_start = entry.get("work_start") or None
            existing.work_end   = entry.get("work_end")   or None
            existing.label      = entry.get("label", "")
            existing.created_by = current_user.email
        else:
            db.add(WorkCalendar(
                date       = d,
                day_type   = day_type,
                work_start = entry.get("work_start") or None,
                work_end   = entry.get("work_end")   or None,
                label      = entry.get("label", ""),
                created_by = current_user.email,
            ))
        saved.append(d_str)

    db.commit()
    return {"success": True, "saved": saved}


# ── DELETE /api/calendar/day/{date} — Xóa override ──────────────

@router.delete("/day/{date_str}")
def delete_day(date_str: str,
               db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(403, "Không có quyền")
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        raise HTTPException(400, "Ngày không hợp lệ")

    cal = db.query(WorkCalendar).filter_by(date=d).first()
    if not cal:
        raise HTTPException(404, "Không có cài đặt đặc biệt cho ngày này")
    db.delete(cal)
    db.commit()
    return {"success": True, "message": f"Đã xóa override ngày {date_str}"}


def _cal_dict(c: WorkCalendar) -> dict:
    return {
        "id":         c.id,
        "date":       c.date.isoformat(),
        "day_type":   c.day_type,
        "work_start": c.work_start,
        "work_end":   c.work_end,
        "label":      c.label,
        "created_by": c.created_by,
    }
