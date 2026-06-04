"""Personal dashboard APIs."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.attendance import AttendanceSession
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.models.user import User
from app.services.leave_policy import LEAVE_ASSIGNMENT_STATUS, active_leave_summary
from app.services.work_calendar import get_calendar_day, get_day_status

router = APIRouter(prefix="/api/me", tags=["me"])


def _find_emp_by_user(user: User, db: Session) -> Employee | None:
    emp = db.query(Employee).filter_by(user_id=user.id, is_active=True).first()
    if emp:
        return emp
    return db.query(Employee).filter_by(email=user.email, is_active=True).first()


def _status_from_requests(requests: list) -> str:
    if any(req.status == "approved" for req in requests):
        return "approved"
    if any(req.status == "pending" for req in requests):
        return "pending"
    return ""


@router.get("/schedule")
def my_schedule(
    from_date: str,
    to_date: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    emp = _find_emp_by_user(current_user, db)
    if not emp:
        raise HTTPException(404, "Tài khoản chưa được liên kết với nhân viên")
    try:
        fd = date.fromisoformat(from_date)
        td = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(400, "Định dạng ngày phải là YYYY-MM-DD")
    if td < fd:
        raise HTTPException(400, "to_date phải >= from_date")
    if (td - fd).days > 62:
        raise HTTPException(400, "Khoảng xem lịch tối đa 63 ngày")

    assignments = (
        db.query(ShiftAssignment)
        .filter(
            ShiftAssignment.emp_code == emp.emp_code,
            ShiftAssignment.work_date >= fd,
            ShiftAssignment.work_date <= td,
            ShiftAssignment.status != "cancelled",
        )
        .order_by(ShiftAssignment.work_date, ShiftAssignment.id)
        .all()
    )
    shift_ids = {row.shift_id for row in assignments}
    shifts = {
        row.id: row
        for row in db.query(Shift).filter(Shift.id.in_(shift_ids)).all()
    } if shift_ids else {}
    branch_ids = {row.branch_id for row in assignments if row.branch_id}
    if emp.branch_id:
        branch_ids.add(emp.branch_id)
    branches = {
        row.id: row
        for row in db.query(Branch).filter(Branch.id.in_(branch_ids)).all()
    } if branch_ids else {}
    assignment_ids = [row.id for row in assignments]
    sessions = {
        row.shift_assignment_id: row
        for row in db.query(AttendanceSession)
        .filter(AttendanceSession.shift_assignment_id.in_(assignment_ids))
        .all()
    } if assignment_ids else {}
    leave_summary = active_leave_summary(db, emp.emp_code)

    rows_by_date: dict[str, list[ShiftAssignment]] = {}
    for assignment in assignments:
        rows_by_date.setdefault(assignment.work_date.isoformat(), []).append(assignment)

    days = []
    cur = fd
    while cur <= td:
        date_str = cur.isoformat()
        day_assignments = []
        for assignment in rows_by_date.get(date_str, []):
            shift = shifts.get(assignment.shift_id)
            branch = branches.get(assignment.branch_id or emp.branch_id)
            session = sessions.get(assignment.id)
            leave_requests = leave_summary["shift_assignment_ids"].get(assignment.id, [])
            leave_status = _status_from_requests(leave_requests)
            if assignment.status == LEAVE_ASSIGNMENT_STATUS:
                leave_status = "approved"
            day_assignments.append({
                "id": assignment.id,
                "shift_id": assignment.shift_id,
                "shift_name": shift.name if shift else "Ca",
                "work_start": shift.work_start if shift else "",
                "work_end": shift.work_end if shift else "",
                "branch_id": assignment.branch_id,
                "branch_name": branch.name if branch else "",
                "status": assignment.status,
                "note": assignment.note or "",
                "leave_status": leave_status,
                "attendance": {
                    "status": session.status if session else "",
                    "check_in_at": session.check_in_at.isoformat() if session and session.check_in_at else "",
                    "check_out_at": session.check_out_at.isoformat() if session and session.check_out_at else "",
                    "late_minutes": session.late_minutes if session else 0,
                    "early_leave_minutes": session.early_leave_minutes if session else 0,
                },
            })

        day_leave_requests = leave_summary["day_dates"].get(date_str, [])
        day_leave_status = _status_from_requests(day_leave_requests)
        branch_id = day_assignments[0]["branch_id"] if day_assignments else emp.branch_id
        cal = get_calendar_day(cur, db, branch_id)
        status = get_day_status(emp.emp_code, cur, db)
        days.append({
            **cal,
            "date": date_str,
            "day_leave_status": day_leave_status,
            "day_status": status.get("status", ""),
            "day_label": status.get("label", ""),
            "day_detail": status.get("detail", ""),
            "assignments": day_assignments,
        })
        cur += timedelta(days=1)

    return {
        "employee": {
            "id": emp.id,
            "emp_code": emp.emp_code,
            "name": emp.name,
            "branch_id": emp.branch_id,
        },
        "from_date": fd.isoformat(),
        "to_date": td.isoformat(),
        "days": days,
    }
