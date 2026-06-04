"""Shared leave rules for requests, scheduling, and personal calendar."""

from __future__ import annotations

from datetime import date
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.leave import LeaveRequest
from app.models.shift import Shift, ShiftAssignment

LEAVE_ASSIGNMENT_STATUS = "leave_approved"
ACTIVE_LEAVE_STATUSES = ("pending", "approved")


def leave_entry_scope(entry: dict) -> str:
    return "shift" if entry.get("scope") == "shift" else "day"


def assignment_ids_from_entry(entry: dict) -> list[int]:
    raw_ids = entry.get("assignment_ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    result = []
    for raw in raw_ids:
        try:
            result.append(int(raw))
        except (TypeError, ValueError):
            continue
    return result


def leave_request_scope(req: LeaveRequest) -> str:
    for entry in req.get_dates():
        if leave_entry_scope(entry) == "shift":
            return "shift"
    return "day"


def leave_scope_label(scope: str) -> str:
    return "Nghỉ theo ca" if scope == "shift" else "Nghỉ cả ngày"


def _active_leave_requests(db: Session, emp_code: str, statuses: Iterable[str] = ACTIVE_LEAVE_STATUSES):
    return (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.emp_code == emp_code,
            LeaveRequest.status.in_(list(statuses)),
            LeaveRequest.request_type.in_(["leave", "remote"]),
        )
        .all()
    )


def active_leave_summary(db: Session, emp_code: str) -> dict:
    day_dates: dict[str, list[LeaveRequest]] = {}
    shift_assignment_ids: dict[int, list[LeaveRequest]] = {}
    shift_dates: dict[str, list[int]] = {}
    for req in _active_leave_requests(db, emp_code):
        for entry in req.get_dates():
            date_str = str(entry.get("date") or "")
            if not date_str:
                continue
            if leave_entry_scope(entry) == "shift":
                ids = assignment_ids_from_entry(entry)
                shift_dates.setdefault(date_str, []).extend(ids)
                for assignment_id in ids:
                    shift_assignment_ids.setdefault(assignment_id, []).append(req)
            else:
                day_dates.setdefault(date_str, []).append(req)
    return {
        "day_dates": day_dates,
        "shift_assignment_ids": shift_assignment_ids,
        "shift_dates": shift_dates,
    }


def has_approved_day_leave(db: Session, emp_code: str, work_date: date | str) -> bool:
    date_str = work_date.isoformat() if isinstance(work_date, date) else str(work_date)
    for req in _active_leave_requests(db, emp_code, statuses=("approved",)):
        for entry in req.get_dates():
            if entry.get("date") == date_str and leave_entry_scope(entry) == "day":
                return True
    return False


def ensure_can_assign_employee(db: Session, emp_code: str, work_date: date | str, existing_assignment: ShiftAssignment | None = None) -> None:
    if has_approved_day_leave(db, emp_code, work_date):
        raise ValueError("Nhân viên đã được duyệt nghỉ cả ngày, không thể xếp ca")
    if existing_assignment and existing_assignment.status == LEAVE_ASSIGNMENT_STATUS:
        raise ValueError("Ca này đã được duyệt nghỉ phép, không thể mở lại bằng phân ca")


def validate_leave_conflicts(
    db: Session,
    emp_code: str,
    requested_dates: list[dict],
    *,
    leave_scope: str,
) -> None:
    summary = active_leave_summary(db, emp_code)
    conflicts = []
    for entry in requested_dates:
        date_str = str(entry.get("date") or "")
        if not date_str:
            continue
        day_reqs = summary["day_dates"].get(date_str, [])
        if leave_scope == "day":
            if day_reqs or summary["shift_dates"].get(date_str):
                conflicts.append(date_str)
            continue
        if day_reqs:
            conflicts.append(date_str)
            continue
        for assignment_id in assignment_ids_from_entry(entry):
            if assignment_id in summary["shift_assignment_ids"]:
                conflicts.append(f"ca #{assignment_id}")
    if conflicts:
        raise ValueError("Đã có đơn nghỉ đang chờ duyệt hoặc đã duyệt cho: " + ", ".join(sorted(set(conflicts))))


def assignment_label(assignment: ShiftAssignment, shift: Shift | None = None) -> str:
    if not shift:
        return f"Ca #{assignment.shift_id} ngày {assignment.work_date.isoformat()}"
    return f"{shift.name} {shift.work_start}-{shift.work_end} ngày {assignment.work_date.isoformat()}"


def request_assignment_labels(db: Session, req: LeaveRequest) -> list[str]:
    labels = []
    for entry in req.get_dates():
        for assignment_id in assignment_ids_from_entry(entry):
            assignment = db.query(ShiftAssignment).filter_by(id=assignment_id).first()
            if not assignment:
                labels.append(f"Ca #{assignment_id}")
                continue
            shift = db.query(Shift).filter_by(id=assignment.shift_id).first()
            labels.append(assignment_label(assignment, shift))
    return labels


def apply_approved_leave_to_assignments(db: Session, req: LeaveRequest, reviewed_by: str = "") -> int:
    count = 0
    scope = leave_request_scope(req)
    dates = req.get_dates()
    note_suffix = f"Nghỉ phép đã duyệt bởi {reviewed_by}".strip()
    if scope == "shift":
        assignment_ids = []
        for entry in dates:
            assignment_ids.extend(assignment_ids_from_entry(entry))
        if assignment_ids:
            rows = (
                db.query(ShiftAssignment)
                .filter(
                    ShiftAssignment.id.in_(assignment_ids),
                    ShiftAssignment.emp_code == req.emp_code,
                    ShiftAssignment.status != "cancelled",
                )
                .all()
            )
            for row in rows:
                row.status = LEAVE_ASSIGNMENT_STATUS
                if note_suffix and note_suffix not in (row.note or ""):
                    row.note = f"{row.note} | {note_suffix}".strip(" |")
                count += 1
        return count

    date_strings = [str(entry.get("date") or "") for entry in dates if entry.get("date")]
    if not date_strings:
        return 0
    rows = (
        db.query(ShiftAssignment)
        .filter(
            ShiftAssignment.emp_code == req.emp_code,
            ShiftAssignment.work_date.in_([date.fromisoformat(d) for d in date_strings]),
            ShiftAssignment.status != "cancelled",
        )
        .all()
    )
    for row in rows:
        row.status = LEAVE_ASSIGNMENT_STATUS
        if note_suffix and note_suffix not in (row.note or ""):
            row.note = f"{row.note} | {note_suffix}".strip(" |")
        count += 1
    return count
