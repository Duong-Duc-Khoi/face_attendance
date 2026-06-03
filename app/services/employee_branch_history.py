"""Helpers for resolving employee branch membership over time."""

from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceEvent, AttendanceLog, AttendanceSession
from app.models.employee import Employee
from app.models.employee_branch_history import EmployeeBranchHistory
from app.models.shift import ShiftAssignment

BASELINE_DATE = date(1970, 1, 1)


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _identity_clauses(employee_id: int | None, emp_code: str | None):
    clauses = []
    if employee_id:
        clauses.append(EmployeeBranchHistory.employee_id == employee_id)
    if emp_code:
        clauses.append(EmployeeBranchHistory.emp_code == emp_code)
    return clauses


def history_query_for_employee(db: Session, emp: Employee):
    return db.query(EmployeeBranchHistory).filter(
        or_(*_identity_clauses(emp.id, emp.emp_code))
    )


def branch_for_employee_on(
    db: Session,
    *,
    employee_id: int | None = None,
    emp_code: str | None = None,
    at: date | datetime | None = None,
) -> int | None:
    day = _as_date(at or date.today())
    clauses = _identity_clauses(employee_id, emp_code)
    if clauses:
        row = (
            db.query(EmployeeBranchHistory)
            .filter(
                or_(*clauses),
                EmployeeBranchHistory.effective_from <= day,
                or_(
                    EmployeeBranchHistory.effective_to.is_(None),
                    EmployeeBranchHistory.effective_to >= day,
                ),
            )
            .order_by(EmployeeBranchHistory.effective_from.desc(), EmployeeBranchHistory.id.desc())
            .first()
        )
        if row:
            return row.branch_id

    emp = None
    if employee_id:
        emp = db.query(Employee).filter_by(id=employee_id).first()
    if not emp and emp_code:
        emp = db.query(Employee).filter_by(emp_code=emp_code).first()
    return emp.branch_id if emp else None


def branch_for_log(db: Session, log: AttendanceLog, event: AttendanceEvent | None = None) -> int | None:
    if event and event.branch_id:
        return event.branch_id
    if event and event.session_id:
        session = db.query(AttendanceSession).filter_by(id=event.session_id).first()
        if session and session.branch_id:
            return session.branch_id
    if log.employee_id:
        matched_event = event or (
            db.query(AttendanceEvent)
            .filter(
                AttendanceEvent.employee_id == log.employee_id,
                AttendanceEvent.event_type == log.check_type,
                AttendanceEvent.event_time == log.timestamp,
            )
            .order_by(AttendanceEvent.id.desc())
            .first()
        )
        if matched_event and matched_event.branch_id:
            return matched_event.branch_id
        if matched_event and matched_event.session_id:
            session = db.query(AttendanceSession).filter_by(id=matched_event.session_id).first()
            if session and session.branch_id:
                return session.branch_id

    return branch_for_employee_on(
        db,
        employee_id=log.employee_id,
        emp_code=log.emp_code,
        at=log.timestamp,
    )


def filter_logs_by_branch_ids(
    db: Session,
    logs: list[AttendanceLog],
    branch_ids: list[int] | None,
) -> list[AttendanceLog]:
    if branch_ids is None:
        return logs
    allowed = set(branch_ids)
    return [log for log in logs if branch_for_log(db, log) in allowed]


def ensure_transfer_history(
    db: Session,
    emp: Employee,
    *,
    to_branch_id: int,
    effective_date: date,
    changed_by: str = "",
    note: str = "",
) -> None:
    prev_day = effective_date - timedelta(days=1)
    q = history_query_for_employee(db, emp)
    existing_count = q.count()

    if existing_count == 0 and emp.branch_id is not None and prev_day >= BASELINE_DATE:
        db.add(EmployeeBranchHistory(
            employee_id=emp.id,
            emp_code=emp.emp_code,
            branch_id=emp.branch_id,
            effective_from=BASELINE_DATE,
            effective_to=prev_day,
            changed_by=changed_by,
            note="Baseline trước khi chuyển cửa hàng",
        ))

    for row in q.filter(EmployeeBranchHistory.effective_from < effective_date).all():
        if row.effective_to is None or row.effective_to >= effective_date:
            row.effective_to = prev_day

    for row in q.filter(EmployeeBranchHistory.effective_from >= effective_date).all():
        db.delete(row)

    db.add(EmployeeBranchHistory(
        employee_id=emp.id,
        emp_code=emp.emp_code,
        branch_id=to_branch_id,
        effective_from=effective_date,
        effective_to=None,
        changed_by=changed_by,
        note=note or "Chuyển cửa hàng",
    ))


def transfer_employee_to_branch(
    db: Session,
    emp: Employee,
    target_branch,
    *,
    effective_date: date,
    changed_by: str = "",
    note: str = "",
) -> tuple[int | None, int]:
    """Move an employee to another branch and cancel old-branch future assignments."""
    from_branch_id = emp.branch_id
    to_branch_id = target_branch.id
    if from_branch_id == to_branch_id:
        return from_branch_id, 0

    ensure_transfer_history(
        db,
        emp,
        to_branch_id=to_branch_id,
        effective_date=effective_date,
        changed_by=changed_by,
        note=note,
    )

    cancelled = []
    if from_branch_id is not None:
        cancelled = (
            db.query(ShiftAssignment)
            .filter(
                ShiftAssignment.emp_code == emp.emp_code,
                ShiftAssignment.branch_id == from_branch_id,
                ShiftAssignment.work_date >= effective_date,
                ShiftAssignment.status != "cancelled",
            )
            .all()
        )
        cancel_note = f"Chuyển cửa hàng sang {target_branch.name} từ {effective_date.isoformat()}"
        for assignment in cancelled:
            assignment.status = "cancelled"
            assignment.note = f"{assignment.note} | {cancel_note}".strip(" |")

    emp.branch_id = to_branch_id
    return from_branch_id, len(cancelled)
