"""
app/api/v1/calendar.py
Endpoints quản lý lịch làm việc.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.calendar import WorkCalendar, WorkCalendarConfig
from app.models.user import User
from app.services.branch_scope import default_branch_id_for_write, selected_branch_ids
from app.services.work_calendar import get_calendar_month

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

VALID_DAY_TYPES = ("full", "off", "holiday")


def _pay_multiplier(value) -> Decimal:
    if value in (None, ""):
        return Decimal("1.0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Hệ số lương không hợp lệ")
    if result < 0 or result > 5:
        raise HTTPException(400, "Hệ số lương phải trong khoảng 0-5")
    return result.quantize(Decimal("0.01"))


def _optional_branch_id(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _work_days_value(value) -> str:
    if isinstance(value, list):
        raw_parts = value
    else:
        raw_parts = str(value or "").split(",")
    days: list[int] = []
    for part in raw_parts:
        try:
            day = int(str(part).strip())
        except ValueError:
            raise HTTPException(400, "Ngày mở cửa mặc định không hợp lệ")
        if day < 1 or day > 7:
            raise HTTPException(400, "Ngày mở cửa mặc định chỉ nhận các ngày trong tuần")
        if day in days:
            raise HTTPException(400, "Ngày mở cửa mặc định không được trùng ngày")
        days.append(day)
    if not days:
        raise HTTPException(400, "Cần chọn ít nhất một ngày mở cửa")
    return ",".join(str(day) for day in sorted(days))


def _fallback_work_days() -> str:
    return settings.WORK_DAYS or "1,2,3,4,5,6,7"


def _config_branch_id(
    db: Session,
    current_user: User,
    branch_id: int | None,
    *,
    require_single: bool = False,
) -> int | None:
    ids = selected_branch_ids(db, current_user, branch_id)
    if ids and len(ids) == 1:
        return ids[0]
    if require_single:
        raise HTTPException(400, "Chọn một cửa hàng để lưu lịch mở cửa")
    return None


# ── GET /api/calendar?year=&month= ──────────────────────────────

@router.get("")
def get_calendar(
    year: int = 0, month: int = 0,
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = date.today()
    y = year  or today.year
    m = month or today.month
    ids = selected_branch_ids(db, current_user, branch_id) if current_user.role != "staff" else None
    effective_branch_id = ids[0] if ids and len(ids) == 1 else None
    days = get_calendar_month(y, m, db, effective_branch_id)
    return {
        "year": y, "month": m,
        "branch_id": effective_branch_id,
        "days": days,
        "defaults": {
            "work_days": _config_work_days(db, effective_branch_id),
        }
    }


# ── GET /api/calendar/config ─────────────────────────────────────

def _config_work_days(db: Session, branch_id: int | None) -> str:
    if branch_id is not None:
        cfg = db.query(WorkCalendarConfig).filter_by(branch_id=branch_id).first()
        if cfg and cfg.work_days:
            return cfg.work_days
    return _fallback_work_days()


@router.get("/config")
def get_config(
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    effective_branch_id = _config_branch_id(db, current_user, branch_id)
    cfg = None
    if effective_branch_id is not None:
        cfg = db.query(WorkCalendarConfig).filter_by(branch_id=effective_branch_id).first()
    return {
        "branch_id": effective_branch_id,
        "work_days": (cfg.work_days if cfg else _fallback_work_days()),
        "source": "branch" if cfg else "system",
        "editable": effective_branch_id is not None,
    }


# ── POST /api/calendar/config — Cập nhật cấu hình mặc định ──────

@router.post("/config")
def update_config(payload: dict,
                  db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(403, "Chỉ quản lý mới được sửa lịch mở cửa")

    branch_id = _config_branch_id(
        db,
        current_user,
        _optional_branch_id(payload.get("branch_id")),
        require_single=True,
    )
    work_days = _work_days_value(payload.get("work_days"))
    cfg = db.query(WorkCalendarConfig).filter_by(branch_id=branch_id).first()
    if cfg:
        cfg.work_days = work_days
        cfg.created_by = current_user.email
        cfg.created_by_id = current_user.id
    else:
        cfg = WorkCalendarConfig(
            branch_id=branch_id,
            work_days=work_days,
            created_by=current_user.email,
            created_by_id=current_user.id,
        )
        db.add(cfg)
    db.commit()
    return {
        "success": True,
        "message": "Đã lưu lịch mở cửa",
        "branch_id": branch_id,
        "work_days": work_days,
        "source": "branch",
        "editable": True,
    }


# ── POST /api/calendar/day — Tạo/sửa 1 ngày đặc biệt ───────────

@router.post("/day")
def upsert_day(payload: dict,
               db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(403, "Chỉ Manager/Admin mới được sửa lịch")

    d_str    = payload.get("date", "")
    day_type = payload.get("day_type", "")
    branch_id = default_branch_id_for_write(db, current_user, _optional_branch_id(payload.get("branch_id")))
    if current_user.role != "admin" and branch_id is None:
        raise HTTPException(400, "Cần chọn cửa hàng")
    if not d_str:
        raise HTTPException(400, "Thiếu trường 'date'")
    if day_type not in VALID_DAY_TYPES:
        raise HTTPException(400, f"day_type phải là một trong: {VALID_DAY_TYPES}")

    try:
        d = date.fromisoformat(d_str)
    except Exception:
        raise HTTPException(400, "Ngày không hợp lệ")

    existing = db.query(WorkCalendar).filter_by(date=d, branch_id=branch_id).first()
    if existing:
        existing.day_type   = day_type
        existing.label      = payload.get("label", "")
        existing.pay_multiplier = _pay_multiplier(payload.get("pay_multiplier"))
        existing.salary_note = payload.get("salary_note", "")
        existing.created_by = current_user.email
        existing.created_by_id = current_user.id
        db.commit()
        db.refresh(existing)
        return {"success": True, "day": _cal_dict(existing)}
    else:
        cal = WorkCalendar(
            date       = d,
            branch_id  = branch_id,
            day_type   = day_type,
            label      = payload.get("label", ""),
            pay_multiplier = _pay_multiplier(payload.get("pay_multiplier")),
            salary_note = payload.get("salary_note", ""),
            created_by = current_user.email,
            created_by_id = current_user.id,
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
    branch_id = default_branch_id_for_write(db, current_user, _optional_branch_id(payload.get("branch_id")))
    if current_user.role != "admin" and branch_id is None:
        raise HTTPException(400, "Cần chọn cửa hàng")

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

        existing = db.query(WorkCalendar).filter_by(date=d, branch_id=branch_id).first()
        if existing:
            existing.day_type   = day_type
            existing.label      = entry.get("label", "")
            existing.pay_multiplier = _pay_multiplier(entry.get("pay_multiplier"))
            existing.salary_note = entry.get("salary_note", "")
            existing.created_by = current_user.email
            existing.created_by_id = current_user.id
        else:
            db.add(WorkCalendar(
                date       = d,
                branch_id  = branch_id,
                day_type   = day_type,
                label      = entry.get("label", ""),
                pay_multiplier = _pay_multiplier(entry.get("pay_multiplier")),
                salary_note = entry.get("salary_note", ""),
                created_by = current_user.email,
                created_by_id = current_user.id,
            ))
        saved.append(d_str)

    db.commit()
    return {"success": True, "saved": saved}


# ── DELETE /api/calendar/day/{date} — Xóa override ──────────────

@router.delete("/day/{date_str}")
def delete_day(date_str: str,
               branch_id: int | None = None,
               db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    if current_user.role not in ("admin", "manager"):
        raise HTTPException(403, "Không có quyền")
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        raise HTTPException(400, "Ngày không hợp lệ")

    branch_id = default_branch_id_for_write(db, current_user, branch_id)
    if current_user.role != "admin" and branch_id is None:
        raise HTTPException(400, "Cần chọn cửa hàng")
    cal = db.query(WorkCalendar).filter_by(date=d, branch_id=branch_id).first()
    if not cal:
        raise HTTPException(404, "Không có cài đặt đặc biệt cho ngày này")
    db.delete(cal)
    db.commit()
    return {"success": True, "message": f"Đã xóa override ngày {date_str}"}


def _cal_dict(c: WorkCalendar) -> dict:
    return {
        "id":         c.id,
        "branch_id":  c.branch_id,
        "date":       c.date.isoformat(),
        "day_type":   c.day_type,
        "label":      c.label,
        "pay_multiplier": float(c.pay_multiplier or 1.0),
        "salary_note": c.salary_note or "",
        "created_by": c.created_by,
    }
