"""
app/api/v1/shifts.py
Endpoints quản lý ca làm việc và phân công ca.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.shift_service import (
    list_shifts, get_shift, create_shift, update_shift, delete_shift,
    assign_shift, bulk_assign_shift, delete_assignment,
    get_assignments_by_emp, get_assignments_by_date,
    get_shift_for_employee,
)
from app.services.ai_shift_planner import (
    apply_shift_plan_draft,
    create_shift_plan_draft,
    get_shift_plan_draft,
)

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


# ── Schemas ──────────────────────────────────────────────────────

class ShiftCreate(BaseModel):
    branch_id:  Optional[int] = None
    name:       str
    code:       Optional[str] = None
    work_start: str   # "HH:MM"
    work_end:   str
    required_position: Optional[str] = ""
    late_threshold_minutes: int = 15
    early_checkin_minutes:  int = 30
    auto_checkout_minutes:  int = 180
    break_minutes:          int = 0
    is_overnight: Optional[bool] = None
    note: Optional[str] = ""

    @field_validator("work_start", "work_end")
    @classmethod
    def validate_time(cls, v):
        try:
            h, m = v.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            raise ValueError("Định dạng giờ phải là HH:MM (ví dụ: 08:30)")
        return v

    @field_validator("code")
    @classmethod
    def validate_code(cls, v):
        if not v:
            return None
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("code chỉ chứa chữ cái, số, dấu _ và -")
        return v.lower()


class ShiftUpdate(BaseModel):
    branch_id:  Optional[int]  = None
    name:       Optional[str]  = None
    work_start: Optional[str]  = None
    work_end:   Optional[str]  = None
    required_position: Optional[str] = None
    late_threshold_minutes: Optional[int] = None
    early_checkin_minutes:  Optional[int] = None
    auto_checkout_minutes:  Optional[int] = None
    break_minutes:          Optional[int] = None
    is_overnight:           Optional[bool] = None
    note:       Optional[str]  = None
    is_active:  Optional[bool] = None


class AssignRequest(BaseModel):
    emp_code:  str
    shift_id:  int
    work_date: str    # "YYYY-MM-DD"
    note:      Optional[str] = ""

    @field_validator("work_date")
    @classmethod
    def validate_date(cls, v):
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("work_date phải định dạng YYYY-MM-DD")
        return v


class BulkAssignRequest(BaseModel):
    emp_codes:  list[str]
    shift_id:   int
    from_date:  str    # "YYYY-MM-DD"
    to_date:    str
    note:       Optional[str] = ""

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date(cls, v):
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Ngày phải định dạng YYYY-MM-DD")
        return v


class AIPlanRequest(BaseModel):
    from_date: str
    to_date: str
    instructions: Optional[str] = ""
    default_min_staff: int = 1
    min_staff_per_shift: Optional[dict[str, int]] = None
    emp_codes: Optional[list[str]] = None
    use_ai: bool = True

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date(cls, v):
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Ngày phải định dạng YYYY-MM-DD")
        return v

    @field_validator("default_min_staff")
    @classmethod
    def validate_min_staff(cls, v):
        if v < 0 or v > 20:
            raise ValueError("Số nhân viên tối thiểu mỗi ca phải trong khoảng 0-20")
        return v


# ── Helpers ──────────────────────────────────────────────────────

def _require_manager(user: User):
    if user.role not in ("admin", "manager"):
        raise HTTPException(403, "Yêu cầu quyền manager hoặc admin")


def _date_range(from_date: str, to_date: str) -> list[date]:
    """Sinh list ngày từ from_date đến to_date (inclusive)."""
    start = date.fromisoformat(from_date)
    end   = date.fromisoformat(to_date)
    if end < start:
        raise HTTPException(400, "to_date phải >= from_date")
    if (end - start).days > 365:
        raise HTTPException(400, "Khoảng thời gian tối đa 1 năm")
    days = []
    cur  = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


# ── GET /api/shifts ──────────────────────────────────────────────

@router.get("")
def api_list_shifts(
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return list_shifts(db, active_only=active_only)


# ── POST /api/shifts ─────────────────────────────────────────────

@router.post("", status_code=201)
def api_create_shift(
    body: ShiftCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    return create_shift(body.model_dump(), db)


# ── GET /api/shifts/my-shift?date=YYYY-MM-DD ─────────────────────

@router.get("/my-shift")
def api_get_my_shift(
    work_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Nhân viên tự xem ca của mình hôm nay (hoặc ngày bất kỳ).
    Cần liên kết User.email với Employee.email.
    """
    from app.models.employee import Employee
    emp = db.query(Employee).filter_by(email=current_user.email, is_active=True).first()
    if not emp:
        raise HTTPException(404, "Tài khoản chưa được liên kết với hồ sơ nhân viên")

    d = date.fromisoformat(work_date) if work_date else date.today()
    return get_shift_for_employee(emp.emp_code, d, db)


# ── AI shift planning ────────────────────────────────────────────

@router.post("/ai-plan", status_code=201)
def api_create_ai_plan(
    body: AIPlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    fd = date.fromisoformat(body.from_date)
    td = date.fromisoformat(body.to_date)
    if td < fd:
        raise HTTPException(400, "to_date phải >= from_date")
    if (td - fd).days > 31:
        raise HTTPException(400, "AI chỉ lập nháp tối đa 32 ngày mỗi lần")
    min_by_shift = {}
    for key, value in (body.min_staff_per_shift or {}).items():
        try:
            min_by_shift[int(key)] = max(int(value), 0)
        except Exception:
            raise HTTPException(400, f"shift_id không hợp lệ: {key}")
    try:
        return create_shift_plan_draft(
            db=db,
            from_date=fd,
            to_date=td,
            created_by=current_user.email,
            instructions=body.instructions or "",
            default_min_staff=body.default_min_staff,
            min_staff_per_shift=min_by_shift,
            emp_codes=body.emp_codes or None,
            use_ai=body.use_ai,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/ai-plan/{draft_id}")
def api_get_ai_plan(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    result = get_shift_plan_draft(draft_id, db)
    if not result:
        raise HTTPException(404, "Không tìm thấy bản nháp")
    return result


@router.post("/ai-plan/{draft_id}/apply")
def api_apply_ai_plan(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    try:
        return apply_shift_plan_draft(draft_id, current_user.email, db)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ── PUT /api/shifts/{id} ─────────────────────────────────────────

@router.put("/{shift_id}")
def api_update_shift(
    shift_id: int,
    body: ShiftUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    result = update_shift(shift_id, body.model_dump(exclude_none=True), db)
    if not result:
        raise HTTPException(404, "Không tìm thấy ca làm việc")
    return result


# ── DELETE /api/shifts/{id} ──────────────────────────────────────

@router.delete("/{shift_id}", status_code=204)
def api_delete_shift(
    shift_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    if not delete_shift(shift_id, db):
        raise HTTPException(404, "Không tìm thấy ca làm việc")


# ── GET /api/shifts/{id} ─────────────────────────────────────────

@router.get("/{shift_id}")
def api_get_shift(
    shift_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = get_shift(shift_id, db)
    if not result:
        raise HTTPException(404, "Không tìm thấy ca làm việc")
    return result


# ── POST /api/shifts/assignments ─────────────────────────────────

@router.post("/assignments", status_code=201)
def api_assign_shift(
    body: AssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phân công ca cho 1 nhân viên vào 1 ngày cụ thể."""
    _require_manager(current_user)
    try:
        return assign_shift(
            emp_code    = body.emp_code,
            shift_id    = body.shift_id,
            work_date   = date.fromisoformat(body.work_date),
            assigned_by = current_user.email,
            note        = body.note or "",
            db          = db,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


# ── POST /api/shifts/assignments/bulk ────────────────────────────

@router.post("/assignments/bulk", status_code=201)
def api_bulk_assign(
    body: BulkAssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phân công ca hàng loạt: nhiều nhân viên × khoảng ngày."""
    _require_manager(current_user)
    if not body.emp_codes:
        raise HTTPException(400, "Danh sách nhân viên không được rỗng")

    days  = _date_range(body.from_date, body.to_date)
    try:
        count = bulk_assign_shift(
            emp_codes   = body.emp_codes,
            shift_id    = body.shift_id,
            dates       = days,
            assigned_by = current_user.email,
            db          = db,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"assigned": count, "message": f"Đã phân công {count} ca thành công"}


# ── GET /api/shifts/assignments/employee/{emp_code} ──────────────

@router.get("/assignments/employee/{emp_code}")
def api_get_emp_assignments(
    emp_code: str,
    from_date: str,    # query param, YYYY-MM-DD
    to_date:   str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xem lịch ca của 1 nhân viên trong khoảng thời gian."""
    try:
        fd = date.fromisoformat(from_date)
        td = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    return get_assignments_by_emp(emp_code, fd, td, db)


# ── GET /api/shifts/assignments/date/{work_date} ─────────────────

@router.get("/assignments/date/{work_date}")
def api_get_date_assignments(
    work_date: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xem tất cả phân công ca trong 1 ngày."""
    _require_manager(current_user)
    try:
        d = date.fromisoformat(work_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    return get_assignments_by_date(d, db)


# ── DELETE /api/shifts/assignments/{id} ──────────────────────────

@router.delete("/assignments/{assignment_id}", status_code=204)
def api_delete_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_manager(current_user)
    if not delete_assignment(assignment_id, db):
        raise HTTPException(404, "Không tìm thấy phân công ca")


