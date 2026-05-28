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
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.models.user import User
from app.services.branch_scope import ensure_branch_access, require_branch_manager_or_admin, scoped_branch_filter
from app.services.shift_service import (
    list_shifts, get_shift, create_shift, update_shift, delete_shift,
    assign_shift, bulk_assign_shift, delete_assignment,
    get_assignments_by_emp, get_assignments_by_date, get_assignments_by_range,
    update_assignment,
    get_shift_for_employee,
)
from app.services.ai_shift_planner import (
    apply_shift_plan_draft,
    create_shift_plan_draft,
    get_shift_plan_draft,
)

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


# ── Schemas ──────────────────────────────────────────────────────

def _validate_time_value(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    try:
        h, m = v.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        raise ValueError("Định dạng giờ phải là HH:MM (ví dụ: 08:30)")
    return v


def _validate_range(v: int, low: int, high: int, label: str) -> int:
    if v < low or v > high:
        raise ValueError(f"{label} phải trong khoảng {low}-{high} phút")
    return v


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
        return _validate_time_value(v)

    @field_validator("late_threshold_minutes")
    @classmethod
    def validate_late_threshold(cls, v):
        return _validate_range(v, 0, 120, "Ngưỡng đi muộn")

    @field_validator("early_checkin_minutes")
    @classmethod
    def validate_early_checkin(cls, v):
        return _validate_range(v, 0, 240, "Cho vào sớm")

    @field_validator("auto_checkout_minutes")
    @classmethod
    def validate_auto_checkout(cls, v):
        return _validate_range(v, 0, 720, "Cho phép chấm ra muộn")

    @field_validator("break_minutes")
    @classmethod
    def validate_break_minutes(cls, v):
        return _validate_range(v, 0, 240, "Nghỉ giữa ca")

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

    @field_validator("work_start", "work_end")
    @classmethod
    def validate_time(cls, v):
        return _validate_time_value(v)

    @field_validator("late_threshold_minutes")
    @classmethod
    def validate_late_threshold(cls, v):
        if v is not None:
            return _validate_range(v, 0, 120, "Ngưỡng đi muộn")
        return v

    @field_validator("early_checkin_minutes")
    @classmethod
    def validate_early_checkin(cls, v):
        if v is not None:
            return _validate_range(v, 0, 240, "Cho vào sớm")
        return v

    @field_validator("auto_checkout_minutes")
    @classmethod
    def validate_auto_checkout(cls, v):
        if v is not None:
            return _validate_range(v, 0, 720, "Cho phép chấm ra muộn")
        return v

    @field_validator("break_minutes")
    @classmethod
    def validate_break_minutes(cls, v):
        if v is not None:
            return _validate_range(v, 0, 240, "Nghỉ giữa ca")
        return v


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


class AssignmentUpdate(BaseModel):
    shift_id: Optional[int] = None
    work_date: Optional[str] = None
    note: Optional[str] = None
    status: Optional[str] = None

    @field_validator("work_date")
    @classmethod
    def validate_date(cls, v):
        if v is None:
            return v
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("work_date phải định dạng YYYY-MM-DD")
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


def _shift_branch_id(db: Session, shift_id: int) -> int | None:
    shift = db.query(Shift).filter_by(id=shift_id).first()
    if not shift:
        raise HTTPException(404, "Không tìm thấy ca làm việc")
    return shift.branch_id


def _assignment_branch_id(db: Session, assignment_id: int) -> int | None:
    assignment = db.query(ShiftAssignment).filter_by(id=assignment_id).first()
    if not assignment:
        raise HTTPException(404, "Không tìm thấy phân công ca")
    return assignment.branch_id


def _employee_branch_id(db: Session, emp_code: str) -> int | None:
    emp = db.query(Employee).filter_by(emp_code=emp_code).first()
    if not emp:
        raise HTTPException(404, f"Không tìm thấy nhân viên {emp_code}")
    return emp.branch_id


def _filter_scoped_rows(db: Session, user: User, rows: list[dict]) -> list[dict]:
    allowed = scoped_branch_filter(db, user)
    if allowed is None:
        return rows
    return [row for row in rows if row.get("branch_id") in allowed]


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
    rows = list_shifts(db, active_only=active_only)
    if current_user.role in ("admin", "manager"):
        return _filter_scoped_rows(db, current_user, rows)
    return rows


# ── POST /api/shifts ─────────────────────────────────────────────

@router.post("", status_code=201)
def api_create_shift(
    body: ShiftCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
    ensure_branch_access(db, current_user, body.branch_id, allow_unassigned_for_admin=True)
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

    try:
        d = date.fromisoformat(work_date) if work_date else date.today()
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    return get_shift_for_employee(emp.emp_code, d, db)


# ── AI shift planning ────────────────────────────────────────────

@router.post("/ai-plan", status_code=201)
def api_create_ai_plan(
    body: AIPlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
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
    require_branch_manager_or_admin(db, current_user)
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
    require_branch_manager_or_admin(db, current_user)
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
    require_branch_manager_or_admin(db, current_user)
    ensure_branch_access(db, current_user, _shift_branch_id(db, shift_id))
    if body.branch_id is not None:
        ensure_branch_access(db, current_user, body.branch_id, allow_unassigned_for_admin=True)
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
    require_branch_manager_or_admin(db, current_user)
    ensure_branch_access(db, current_user, _shift_branch_id(db, shift_id))
    if not delete_shift(shift_id, db):
        raise HTTPException(404, "Không tìm thấy ca làm việc")


# ── POST /api/shifts/assignments ─────────────────────────────────

@router.post("/assignments", status_code=201)
def api_assign_shift(
    body: AssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phân công ca cho 1 nhân viên vào 1 ngày cụ thể."""
    require_branch_manager_or_admin(db, current_user)
    ensure_branch_access(db, current_user, _shift_branch_id(db, body.shift_id))
    ensure_branch_access(db, current_user, _employee_branch_id(db, body.emp_code))
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
    require_branch_manager_or_admin(db, current_user)
    if not body.emp_codes:
        raise HTTPException(400, "Danh sách nhân viên không được rỗng")
    ensure_branch_access(db, current_user, _shift_branch_id(db, body.shift_id))
    for emp_code in body.emp_codes:
        ensure_branch_access(db, current_user, _employee_branch_id(db, emp_code))

    days  = _date_range(body.from_date, body.to_date)
    try:
        count = bulk_assign_shift(
            emp_codes   = body.emp_codes,
            shift_id    = body.shift_id,
            dates       = days,
            assigned_by = current_user.email,
            note        = body.note or "",
            db          = db,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"assigned": count, "message": f"Đã phân công {count} ca thành công"}


# ── GET /api/shifts/assignments?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD ──

@router.get("/assignments")
def api_get_range_assignments(
    from_date: str,
    to_date: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xem tất cả phân công ca trong một khoảng ngày."""
    require_branch_manager_or_admin(db, current_user)
    try:
        fd = date.fromisoformat(from_date)
        td = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    if td < fd:
        raise HTTPException(400, "to_date phải >= from_date")
    if (td - fd).days > 62:
        raise HTTPException(400, "Khoảng xem lịch tối đa 63 ngày")
    return _filter_scoped_rows(db, current_user, get_assignments_by_range(fd, td, db))


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
    if current_user.role != "staff":
        ensure_branch_access(db, current_user, _employee_branch_id(db, emp_code))
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
    require_branch_manager_or_admin(db, current_user)
    try:
        d = date.fromisoformat(work_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    return _filter_scoped_rows(db, current_user, get_assignments_by_date(d, db))


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
    if current_user.role != "staff":
        ensure_branch_access(db, current_user, result.get("branch_id"))
    return result


# ── PUT /api/shifts/assignments/{id} ─────────────────────────────

@router.put("/assignments/{assignment_id}")
def api_update_assignment(
    assignment_id: int,
    body: AssignmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
    ensure_branch_access(db, current_user, _assignment_branch_id(db, assignment_id))
    if body.shift_id is not None:
        ensure_branch_access(db, current_user, _shift_branch_id(db, body.shift_id))
    try:
        result = update_assignment(
            assignment_id,
            body.model_dump(exclude_none=True),
            assigned_by=current_user.email,
            db=db,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not result:
        raise HTTPException(404, "Không tìm thấy phân công ca")
    return result


# ── DELETE /api/shifts/assignments/{id} ──────────────────────────

@router.delete("/assignments/{assignment_id}", status_code=204)
def api_delete_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
    ensure_branch_access(db, current_user, _assignment_branch_id(db, assignment_id))
    if not delete_assignment(assignment_id, db):
        raise HTTPException(404, "Không tìm thấy phân công ca")


