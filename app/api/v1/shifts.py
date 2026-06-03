"""
app/api/v1/shifts.py
Endpoints quản lý ca làm việc và phân công ca.
"""

from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.models.user import User
from app.schemas.employee import JOB_ROLE_LABELS, normalize_job_role
from app.services.branch_scope import (
    default_branch_id_for_write,
    ensure_branch_access,
    require_branch_manager_or_admin,
    selected_branch_ids,
)
from app.services.shift_service import (
    list_shifts, get_shift, create_shift, update_shift, delete_shift,
    assign_shift, bulk_assign_shift, delete_assignment,
    get_assignments_by_emp, get_assignments_by_date, get_assignments_by_range,
    update_assignment,
    get_shift_for_employee,
    _employee_role_matches_shift,
    _ensure_assignable_workday,
    current_assignment_edit_start,
    ensure_assignment_editable_date,
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


class MultiAssignRequest(BaseModel):
    emp_codes: list[str]
    shift_id: int
    work_date: str
    note: Optional[str] = ""

    @field_validator("work_date")
    @classmethod
    def validate_date(cls, v):
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("work_date phải định dạng YYYY-MM-DD")
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


def _filter_scoped_rows(
    db: Session,
    user: User,
    rows: list[dict],
    branch_id: int | None = None,
    *,
    include_global: bool = False,
) -> list[dict]:
    allowed = selected_branch_ids(db, user, branch_id)
    if allowed is None:
        return rows
    return [
        row for row in rows
        if row.get("branch_id") in allowed or (include_global and row.get("branch_id") is None)
    ]


def _require_admin_branch_for_shift_view(user: User, branch_id: int | None) -> None:
    if user.role == "admin" and branch_id is None:
        raise HTTPException(400, "Cần chọn chi nhánh để xem ca làm")


def _ensure_shift_assignable_scope(db: Session, user: User, shift_id: int) -> None:
    branch_id = _shift_branch_id(db, shift_id)
    if branch_id is not None:
        ensure_branch_access(db, user, branch_id)


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


def _ensure_assignment_dates_editable(dates: list[date] | tuple[date, ...]) -> None:
    for work_date in dates:
        try:
            ensure_assignment_editable_date(work_date)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


def _excel_date_formula(value: date) -> str:
    return f"DATE({value.year},{value.month},{value.day})"


def _parse_assignment_io_dates(
    from_date: str | None,
    to_date: str | None,
    *,
    default_from: date | None = None,
    require_editable: bool = False,
) -> tuple[date, date, list[date]]:
    start_default = default_from or current_assignment_edit_start()
    end_default = start_default + timedelta(days=6)
    try:
        fd = date.fromisoformat(from_date) if from_date else start_default
        td = date.fromisoformat(to_date) if to_date else end_default
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    if td < fd:
        raise HTTPException(400, "to_date phải >= from_date")
    if (td - fd).days > 30:
        raise HTTPException(400, "Khoảng ngày tối đa 31 ngày")
    if require_editable:
        _ensure_assignment_dates_editable([fd, td])
    days = []
    cur = fd
    while cur <= td:
        days.append(cur)
        cur += timedelta(days=1)
    return fd, td, days


def _day_sheet_title(work_date: date) -> str:
    weekday = {1: "T2", 2: "T3", 3: "T4", 4: "T5", 5: "T6", 6: "T7", 7: "CN"}[work_date.isoweekday()]
    return f"{work_date.isoformat()} {weekday}"


def _parse_day_sheet_date(ws) -> date:
    title = str(ws.title or "").strip()
    try:
        sheet_date = date.fromisoformat(title[:10])
    except Exception:
        raise ValueError(f"Sheet '{ws.title}' không đúng tên ngày YYYY-MM-DD")
    label = str(ws.cell(row=1, column=1).value or "").strip().lower()
    if not label.startswith("ngày phân ca"):
        raise ValueError(f"Sheet '{ws.title}' thiếu tiêu đề ngày phân ca ở ô A1")
    title_date_value = ws.cell(row=1, column=2).value
    try:
        title_date = _parse_excel_date(title_date_value, 1)
    except ValueError:
        title_date = None
    if title_date != sheet_date:
        raise ValueError(f"Sheet '{ws.title}' có ngày tiêu đề không khớp tên sheet")
    return sheet_date


def _required_branch_id_for_assignment_io(db: Session, user: User, branch_id: int | None = None) -> int:
    require_branch_manager_or_admin(db, user)
    effective_branch_id = default_branch_id_for_write(db, user, branch_id)
    if effective_branch_id is None:
        raise HTTPException(400, "Cần chọn cửa hàng để nhập/xuất lịch phân ca")
    ensure_branch_access(db, user, effective_branch_id, allow_unassigned_for_admin=False)
    return effective_branch_id


def _role_label(emp: Employee) -> str:
    role = normalize_job_role(emp.job_role or emp.position or "")
    return JOB_ROLE_LABELS.get(role, emp.position or emp.department or role or "Nhân viên")


def _unique_labels(items: list, base_label_fn, fallback_fn) -> dict:
    buckets: dict[str, list] = {}
    for item in items:
        label = base_label_fn(item)
        buckets.setdefault(label, []).append(item)
    labels = {}
    for label, rows in buckets.items():
        if len(rows) == 1:
            labels[rows[0].id] = label
            continue
        for row in rows:
            labels[row.id] = f"{label} - {fallback_fn(row)}"
    return labels


def _employee_label(emp: Employee) -> str:
    return f"{emp.name} - {_role_label(emp)}"


def _employee_label_fallback(emp: Employee) -> str:
    return emp.email or emp.phone or emp.emp_code


def _shift_label(shift: Shift) -> str:
    return f"{shift.name} {shift.work_start}-{shift.work_end}"


def _shift_label_fallback(shift: Shift) -> str:
    return shift.code or f"Ca #{shift.id}"


def _branch_employees(db: Session, branch_id: int) -> list[Employee]:
    return (
        db.query(Employee)
        .filter(Employee.branch_id == branch_id, Employee.is_active == True)
        .order_by(Employee.name, Employee.emp_code)
        .all()
    )


def _branch_shifts(db: Session, branch_id: int) -> list[Shift]:
    return (
        db.query(Shift)
        .filter(
            Shift.is_active == True,
            (Shift.branch_id == branch_id) | (Shift.branch_id.is_(None)),
        )
        .order_by(Shift.branch_id.desc(), Shift.work_start, Shift.name)
        .all()
    )


def _assignment_io_catalog(db: Session, branch_id: int) -> dict:
    employees = _branch_employees(db, branch_id)
    shifts = _branch_shifts(db, branch_id)
    employee_labels = _unique_labels(employees, _employee_label, _employee_label_fallback)
    shift_labels = _unique_labels(shifts, _shift_label, _shift_label_fallback)
    employees_by_code = {emp.emp_code: emp for emp in employees}
    shifts_by_id = {shift.id: shift for shift in shifts}
    employee_label_to_code = {employee_labels[emp.id]: emp.emp_code for emp in employees}
    shift_label_to_id = {shift_labels[shift.id]: shift.id for shift in shifts}
    return {
        "employees": employees,
        "shifts": shifts,
        "employee_labels": employee_labels,
        "shift_labels": shift_labels,
        "employees_by_code": employees_by_code,
        "shifts_by_id": shifts_by_id,
        "employee_label_to_code": employee_label_to_code,
        "shift_label_to_id": shift_label_to_id,
    }


def _parse_excel_date(value, row_num: int) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Dòng {row_num}: Ngày phải có dạng YYYY-MM-DD hoặc DD/MM/YYYY")


def _write_assignment_workbook(
    *,
    db: Session,
    branch_id: int,
    from_date: date | None = None,
    to_date: date | None = None,
    include_assignments: bool = False,
):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        return None

    catalog = _assignment_io_catalog(db, branch_id)
    branch = db.query(Branch).filter_by(id=branch_id).first()
    if not from_date:
        from_date = current_assignment_edit_start()
    if not to_date:
        to_date = from_date + timedelta(days=6)
    _fd, _td, workbook_days = _parse_assignment_io_dates(
        from_date.isoformat(),
        to_date.isoformat(),
        default_from=from_date,
        require_editable=not include_assignments,
    )
    wb = Workbook()
    required_fill = PatternFill("solid", fgColor="FFF2CC")

    lookup = wb.create_sheet("DanhMuc")
    lookup.append(["type", "label", "code_or_id", "name", "branch_id"])
    employee_start = 2
    for emp in catalog["employees"]:
        lookup.append(["employee", catalog["employee_labels"][emp.id], emp.emp_code, emp.name, emp.branch_id])
    employee_end = lookup.max_row
    shift_start = lookup.max_row + 1
    for shift in catalog["shifts"]:
        lookup.append(["shift", catalog["shift_labels"][shift.id], shift.id, shift.name, shift.branch_id or "global"])
    shift_end = lookup.max_row
    lookup.sheet_state = "hidden"

    rows_by_date: dict[str, list[dict]] = {}
    if include_assignments and from_date and to_date:
        rows = [
            row for row in get_assignments_by_range(from_date, to_date, db)
            if row.get("branch_id") == branch_id
        ]
        for row in rows:
            rows_by_date.setdefault(str(row.get("work_date") or ""), []).append(row)

    default_ws = wb.active
    for index, work_date in enumerate(workbook_days):
        ws = default_ws if index == 0 else wb.create_sheet()
        ws.title = _day_sheet_title(work_date)
        ws["A1"] = "Ngày phân ca:"
        ws["B1"] = work_date
        ws["B1"].number_format = "yyyy-mm-dd"
        ws["A1"].font = Font(bold=True)
        ws["B1"].font = Font(bold=True)
        ws.append(["Nhân viên *", "Ca làm *", "Ghi chú"])
        for cell in ws[2]:
            cell.font = Font(bold=True)
            if "*" in str(cell.value):
                cell.fill = required_fill
        ws.freeze_panes = "A3"
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 28
        ws.column_dimensions["C"].width = 34

        for row in rows_by_date.get(work_date.isoformat(), []):
            emp = catalog["employees_by_code"].get(row.get("emp_code"))
            shift = catalog["shifts_by_id"].get(row.get("shift_id"))
            if not emp or not shift:
                continue
            ws.append([
                catalog["employee_labels"][emp.id],
                catalog["shift_labels"][shift.id],
                row.get("note") or "",
            ])

        max_rows = max(ws.max_row + 200, 300)
        if employee_end >= employee_start:
            dv_emp = DataValidation(type="list", formula1=f"=DanhMuc!$B${employee_start}:$B${employee_end}", allow_blank=False)
            ws.add_data_validation(dv_emp)
            dv_emp.add(f"A3:A{max_rows}")
        if shift_end >= shift_start:
            dv_shift = DataValidation(type="list", formula1=f"=DanhMuc!$B${shift_start}:$B${shift_end}", allow_blank=False)
            ws.add_data_validation(dv_shift)
            dv_shift.add(f"B3:B{max_rows}")

    guide = wb.create_sheet("Huong dan")
    guide.append(["Cửa hàng", branch.name if branch else f"#{branch_id}"])
    guide.append(["Khoảng ngày", f"{from_date.isoformat()} đến {to_date.isoformat()}"])
    guide.append(["Cách nhập", "Mỗi sheet là một ngày. Chọn nhân viên và ca làm từ dropdown. Ghi chú có thể bỏ trống."])
    guide.append(["Giới hạn chỉnh sửa", f"Chỉ được nhập/chỉnh lịch từ {current_assignment_edit_start().isoformat()} trở đi. Lịch cũ chỉ dùng để xem/đối soát."])
    guide.append(["Lưu ý", "File được kiểm tra toàn bộ trước khi ghi. Có lỗi thì không nhập dòng nào."])
    for cell in guide[1]:
        cell.font = Font(bold=True)
    return wb


def _save_workbook_response(wb, filename: str):
    settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = settings.EXPORTS_DIR / filename
    wb.save(path)
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


def _workbook_lookup_maps(wb) -> tuple[dict[str, str], dict[str, int]]:
    employee_map: dict[str, str] = {}
    shift_map: dict[str, int] = {}
    if "DanhMuc" not in wb.sheetnames:
        return employee_map, shift_map
    ws = wb["DanhMuc"]
    for row in ws.iter_rows(min_row=2, values_only=True):
        row_type, label, code_or_id = row[0], row[1], row[2]
        if not row_type or not label or code_or_id in (None, ""):
            continue
        if str(row_type).strip() == "employee":
            employee_map[str(label).strip()] = str(code_or_id).strip()
        elif str(row_type).strip() == "shift":
            try:
                shift_map[str(label).strip()] = int(code_or_id)
            except Exception:
                continue
    return employee_map, shift_map


def _resolve_employee_for_import(label: str, catalog: dict, workbook_map: dict[str, str]) -> Employee | None:
    raw = str(label or "").strip()
    code = workbook_map.get(raw) or catalog["employee_label_to_code"].get(raw)
    if code:
        return catalog["employees_by_code"].get(code)
    matches = [
        emp for emp in catalog["employees"]
        if emp.name.strip().lower() == raw.lower() or emp.emp_code.strip().lower() == raw.lower()
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_shift_for_import(label: str, catalog: dict, workbook_map: dict[str, int]) -> Shift | None:
    raw = str(label or "").strip()
    shift_id = workbook_map.get(raw) or catalog["shift_label_to_id"].get(raw)
    if shift_id:
        return catalog["shifts_by_id"].get(shift_id)
    matches = [
        shift for shift in catalog["shifts"]
        if shift.name.strip().lower() == raw.lower() or shift.code.strip().lower() == raw.lower()
    ]
    return matches[0] if len(matches) == 1 else None


def _worksheet_headers(ws, row_num: int) -> dict[str, int]:
    return {
        str(cell.value or "").strip(): idx
        for idx, cell in enumerate(ws[row_num], start=1)
    }


def _collect_legacy_import_rows(ws) -> tuple[list[dict], list[str]]:
    headers = _worksheet_headers(ws, 1)
    required_headers = ["Ngày *", "Nhân viên *", "Ca làm *"]
    missing = [header for header in required_headers if header not in headers]
    if missing:
        return [], [f"Thiếu cột bắt buộc: {', '.join(missing)}"]

    rows = []
    errors = []
    for row_num in range(2, ws.max_row + 1):
        raw_date = ws.cell(row=row_num, column=headers["Ngày *"]).value
        raw_employee = ws.cell(row=row_num, column=headers["Nhân viên *"]).value
        raw_shift = ws.cell(row=row_num, column=headers["Ca làm *"]).value
        note = ""
        if "Ghi chú" in headers:
            note = str(ws.cell(row=row_num, column=headers["Ghi chú"]).value or "").strip()
        if raw_date in (None, "") and raw_employee in (None, "") and raw_shift in (None, "") and not note:
            continue
        try:
            work_date = _parse_excel_date(raw_date, row_num)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        rows.append({
            "source": f"Dòng {row_num}",
            "work_date": work_date,
            "raw_employee": raw_employee,
            "raw_shift": raw_shift,
            "note": note,
        })
    return rows, errors


def _collect_day_sheet_import_rows(wb) -> tuple[list[dict], list[str]]:
    skip_titles = {"DanhMuc", "Huong dan"}
    rows = []
    errors = []
    day_sheet_count = 0
    for ws in wb.worksheets:
        if ws.sheet_state != "visible" or ws.title in skip_titles:
            continue
        day_sheet_count += 1
        try:
            work_date = _parse_day_sheet_date(ws)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        headers = _worksheet_headers(ws, 2)
        required_headers = ["Nhân viên *", "Ca làm *"]
        missing = [header for header in required_headers if header not in headers]
        if missing:
            errors.append(f"Sheet '{ws.title}': thiếu cột bắt buộc: {', '.join(missing)}")
            continue
        for row_num in range(3, ws.max_row + 1):
            raw_employee = ws.cell(row=row_num, column=headers["Nhân viên *"]).value
            raw_shift = ws.cell(row=row_num, column=headers["Ca làm *"]).value
            note = ""
            if "Ghi chú" in headers:
                note = str(ws.cell(row=row_num, column=headers["Ghi chú"]).value or "").strip()
            if raw_employee in (None, "") and raw_shift in (None, "") and not note:
                continue
            rows.append({
                "source": f"Sheet '{ws.title}' dòng {row_num}",
                "work_date": work_date,
                "raw_employee": raw_employee,
                "raw_shift": raw_shift,
                "note": note,
            })
    if day_sheet_count == 0:
        errors.append("File không có sheet ngày phân ca")
    return rows, errors


def _collect_assignment_import_rows(wb) -> tuple[list[dict], list[str]]:
    legacy_ws = wb["Lich phan ca"] if "Lich phan ca" in wb.sheetnames else None
    if legacy_ws is not None:
        legacy_headers = _worksheet_headers(legacy_ws, 1)
        if all(header in legacy_headers for header in ("Ngày *", "Nhân viên *", "Ca làm *")):
            return _collect_legacy_import_rows(legacy_ws)
    active_headers = _worksheet_headers(wb.active, 1)
    if all(header in active_headers for header in ("Ngày *", "Nhân viên *", "Ca làm *")):
        return _collect_legacy_import_rows(wb.active)
    return _collect_day_sheet_import_rows(wb)


def _validate_assignment_import_rows(
    raw_rows: list[dict],
    *,
    catalog: dict,
    workbook_employee_map: dict[str, str],
    workbook_shift_map: dict[str, int],
    db: Session,
) -> tuple[list[dict], list[str]]:
    rows_to_apply = []
    errors = []
    seen_keys = set()
    for raw_row in raw_rows:
        source = raw_row["source"]
        work_date = raw_row.get("work_date")
        raw_employee = raw_row.get("raw_employee")
        raw_shift = raw_row.get("raw_shift")
        if not work_date:
            errors.append(f"{source}: Thiếu Ngày")
            continue
        try:
            ensure_assignment_editable_date(work_date)
        except ValueError as exc:
            errors.append(f"{source}: {exc}")
            continue
        if not raw_employee:
            errors.append(f"{source}: Thiếu Nhân viên")
            continue
        if not raw_shift:
            errors.append(f"{source}: Thiếu Ca làm")
            continue

        emp = _resolve_employee_for_import(str(raw_employee), catalog, workbook_employee_map)
        if not emp:
            errors.append(f"{source}: Không nhận diện được nhân viên '{raw_employee}' trong cửa hàng đang chọn")
            continue
        shift = _resolve_shift_for_import(str(raw_shift), catalog, workbook_shift_map)
        if not shift:
            errors.append(f"{source}: Không nhận diện được ca làm '{raw_shift}' trong cửa hàng đang chọn")
            continue
        key = (emp.emp_code, work_date.isoformat(), shift.id)
        if key in seen_keys:
            errors.append(f"{source}: Trùng phân ca trong file cho {emp.name} ngày {work_date.isoformat()}")
            continue
        seen_keys.add(key)

        try:
            _ensure_assignable_workday(work_date, db, shift.branch_id or emp.branch_id)
            if not _employee_role_matches_shift(emp, shift):
                role = emp.job_role or emp.position or "chưa xác định"
                raise ValueError(f"Vai trò '{role}' không phù hợp với ca yêu cầu '{shift.required_position}'")
        except ValueError as exc:
            errors.append(f"{source}: {exc}")
            continue

        rows_to_apply.append({
            "emp": emp,
            "shift": shift,
            "work_date": work_date,
            "note": raw_row.get("note") or "",
        })
    return rows_to_apply, errors


# ── GET /api/shifts ──────────────────────────────────────────────

@router.get("")
def api_list_shifts(
    active_only: bool = False,
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = list_shifts(db, active_only=active_only)
    if current_user.role in ("admin", "manager"):
        _require_admin_branch_for_shift_view(current_user, branch_id)
        return _filter_scoped_rows(db, current_user, rows, branch_id, include_global=True)
    return rows


# ── POST /api/shifts ─────────────────────────────────────────────

@router.post("", status_code=201)
def api_create_shift(
    body: ShiftCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_branch_manager_or_admin(db, current_user)
    data = body.model_dump()
    data["branch_id"] = default_branch_id_for_write(db, current_user, body.branch_id)
    ensure_branch_access(db, current_user, data["branch_id"], allow_unassigned_for_admin=True)
    return create_shift(data, db)


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
    _ensure_shift_assignable_scope(db, current_user, body.shift_id)
    ensure_branch_access(db, current_user, _employee_branch_id(db, body.emp_code))
    work_date = date.fromisoformat(body.work_date)
    _ensure_assignment_dates_editable([work_date])
    try:
        return assign_shift(
            emp_code    = body.emp_code,
            shift_id    = body.shift_id,
            work_date   = work_date,
            assigned_by = current_user.email,
            note        = body.note or "",
            db          = db,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    _ensure_shift_assignable_scope(db, current_user, body.shift_id)
    for emp_code in body.emp_codes:
        ensure_branch_access(db, current_user, _employee_branch_id(db, emp_code))

    days  = _date_range(body.from_date, body.to_date)
    _ensure_assignment_dates_editable(days)
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
        raise HTTPException(400, str(e))
    return {"assigned": count, "message": f"Đã phân công {count} ca thành công"}


@router.post("/assignments/multi", status_code=201)
def api_multi_assign(
    body: MultiAssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Phân công một ca/ngày cho nhiều nhân viên, dùng cho kéo thả nhóm."""
    require_branch_manager_or_admin(db, current_user)
    if not body.emp_codes:
        raise HTTPException(400, "Danh sách nhân viên không được rỗng")
    _ensure_shift_assignable_scope(db, current_user, body.shift_id)
    work_date = date.fromisoformat(body.work_date)
    _ensure_assignment_dates_editable([work_date])
    results = []
    errors = []
    seen = set()
    for emp_code in body.emp_codes:
        code = str(emp_code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            ensure_branch_access(db, current_user, _employee_branch_id(db, code))
            assignment = assign_shift(
                emp_code=code,
                shift_id=body.shift_id,
                work_date=work_date,
                assigned_by=current_user.email,
                note=body.note or "",
                db=db,
            )
            results.append(assignment)
        except HTTPException as exc:
            errors.append({"emp_code": code, "message": exc.detail})
        except ValueError as exc:
            errors.append({"emp_code": code, "message": str(exc)})
    return {
        "assigned": len(results),
        "errors": errors,
        "assignments": results,
        "message": f"Đã phân công {len(results)} nhân viên" + (f", lỗi {len(errors)}" if errors else ""),
    }


@router.get("/assignments/template")
def api_assignment_template(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    branch_id = _required_branch_id_for_assignment_io(db, current_user, branch_id)
    fd, td, _days = _parse_assignment_io_dates(from_date, to_date, require_editable=True)
    wb = _write_assignment_workbook(db=db, branch_id=branch_id, from_date=fd, to_date=td)
    if wb is None:
        return JSONResponse({"error": "Cài openpyxl: pip install openpyxl"}, status_code=500)
    return _save_workbook_response(wb, f"MauNhapLichPhanCa_{fd.isoformat()}_{td.isoformat()}_branch_{branch_id}.xlsx")


@router.get("/assignments/export")
def api_export_assignments(
    from_date: str,
    to_date: str,
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    branch_id = _required_branch_id_for_assignment_io(db, current_user, branch_id)
    try:
        fd = date.fromisoformat(from_date)
        td = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    if td < fd:
        raise HTTPException(400, "to_date phải >= from_date")
    if (td - fd).days > 30:
        raise HTTPException(400, "Khoảng xuất tối đa 31 ngày")
    wb = _write_assignment_workbook(
        db=db,
        branch_id=branch_id,
        from_date=fd,
        to_date=td,
        include_assignments=True,
    )
    if wb is None:
        return JSONResponse({"error": "Cài openpyxl: pip install openpyxl"}, status_code=500)
    return _save_workbook_response(wb, f"LichPhanCa_{from_date}_{to_date}_branch_{branch_id}.xlsx")


@router.post("/assignments/import")
async def api_import_assignments(
    branch_id: Optional[int] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    branch_id = _required_branch_id_for_assignment_io(db, current_user, branch_id)
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Chỉ hỗ trợ file .xlsx")
    try:
        from openpyxl import load_workbook
    except ImportError:
        return JSONResponse({"error": "Cài openpyxl: pip install openpyxl"}, status_code=500)

    content = await file.read()
    try:
        wb = load_workbook(BytesIO(content), data_only=True)
    except Exception:
        raise HTTPException(400, "File Excel không hợp lệ")

    catalog = _assignment_io_catalog(db, branch_id)
    workbook_employee_map, workbook_shift_map = _workbook_lookup_maps(wb)
    raw_rows, collect_errors = _collect_assignment_import_rows(wb)
    rows_to_apply, validate_errors = _validate_assignment_import_rows(
        raw_rows,
        catalog=catalog,
        workbook_employee_map=workbook_employee_map,
        workbook_shift_map=workbook_shift_map,
        db=db,
    )
    errors = collect_errors + validate_errors

    if errors:
        raise HTTPException(400, {
            "message": "File có lỗi, chưa nhập dữ liệu",
            "errors": errors,
            "total_rows": len(raw_rows),
        })
    if not rows_to_apply:
        raise HTTPException(400, "File không có dòng phân ca để nhập")

    applied = []
    try:
        for row in rows_to_apply:
            applied.append(assign_shift(
                emp_code=row["emp"].emp_code,
                shift_id=row["shift"].id,
                work_date=row["work_date"],
                assigned_by=current_user.email,
                note=row["note"],
                db=db,
                commit=False,
            ))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"Không nhập được file: {exc}")

    return {
        "success": True,
        "total_rows": len(rows_to_apply),
        "success_count": len(applied),
        "error_count": 0,
        "errors": [],
        "message": f"Đã nhập {len(applied)} phân ca",
    }


# ── GET /api/shifts/assignments?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD ──

@router.get("/assignments")
def api_get_range_assignments(
    from_date: str,
    to_date: str,
    branch_id: Optional[int] = None,
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
    _require_admin_branch_for_shift_view(current_user, branch_id)
    return _filter_scoped_rows(db, current_user, get_assignments_by_range(fd, td, db), branch_id)


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
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xem tất cả phân công ca trong 1 ngày."""
    require_branch_manager_or_admin(db, current_user)
    try:
        d = date.fromisoformat(work_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    _require_admin_branch_for_shift_view(current_user, branch_id)
    return _filter_scoped_rows(db, current_user, get_assignments_by_date(d, db), branch_id)


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
    if current_user.role != "staff" and result.get("branch_id") is not None:
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
        _ensure_shift_assignable_scope(db, current_user, body.shift_id)
    if body.work_date is not None:
        _ensure_assignment_dates_editable([date.fromisoformat(body.work_date)])
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
    try:
        if not delete_assignment(assignment_id, db):
            raise HTTPException(404, "Không tìm thấy phân công ca")
    except ValueError as e:
        raise HTTPException(400, str(e))


