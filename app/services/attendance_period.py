"""Attendance period locks and correction audit helpers."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.attendance import (
    AttendanceCorrectionAudit,
    AttendanceLog,
    AttendancePeriodLock,
    AttendanceSession,
)


LOCKED_PERIOD_ERROR = "Kỳ công đã chốt, hãy mở khóa kỳ trước khi chỉnh dữ liệu chấm công"


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _overlapping_lock_query(db: Session, branch_id: int | None, from_date: date, to_date: date):
    q = db.query(AttendancePeriodLock).filter(
        AttendancePeriodLock.is_active.is_(True),
        AttendancePeriodLock.from_date <= to_date,
        AttendancePeriodLock.to_date >= from_date,
    )
    if branch_id is not None:
        q = q.filter(
            (AttendancePeriodLock.branch_id == branch_id)
            | (AttendancePeriodLock.branch_id.is_(None))
        )
    return q


def find_active_period_lock(
    db: Session,
    branch_id: int | None,
    from_date: date | datetime | str,
    to_date: date | datetime | str | None = None,
) -> AttendancePeriodLock | None:
    start = _as_date(from_date)
    end = _as_date(to_date) if to_date is not None else start
    return _overlapping_lock_query(db, branch_id, start, end).order_by(AttendancePeriodLock.locked_at.desc()).first()


def ensure_period_unlocked(
    db: Session,
    branch_id: int | None,
    from_date: date | datetime | str,
    to_date: date | datetime | str | None = None,
) -> None:
    lock = find_active_period_lock(db, branch_id, from_date, to_date)
    if lock:
        label = f"{lock.from_date.isoformat()} -> {lock.to_date.isoformat()}"
        raise ValueError(f"{LOCKED_PERIOD_ERROR}: {label}")


def create_period_lock(
    db: Session,
    *,
    branch_id: int | None,
    from_date: date,
    to_date: date,
    locked_by: str = "",
    locked_by_id: int | None = None,
    note: str = "",
) -> AttendancePeriodLock:
    if to_date < from_date:
        raise ValueError("Ngày kết thúc phải sau hoặc bằng ngày bắt đầu")
    existing = find_active_period_lock(db, branch_id, from_date, to_date)
    if existing:
        raise ValueError("Khoảng ngày này đang bị khóa bởi một kỳ công khác")
    row = AttendancePeriodLock(
        branch_id=branch_id,
        from_date=from_date,
        to_date=to_date,
        note=note or "",
        locked_by=locked_by or "",
        locked_by_id=locked_by_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def unlock_period(
    db: Session,
    lock_id: int,
    *,
    unlocked_by: str = "",
    unlocked_by_id: int | None = None,
) -> AttendancePeriodLock | None:
    row = db.query(AttendancePeriodLock).filter_by(id=lock_id).first()
    if not row:
        return None
    row.is_active = False
    row.unlocked_by = unlocked_by or ""
    row.unlocked_by_id = unlocked_by_id
    row.unlocked_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row


def period_lock_to_dict(row: AttendancePeriodLock) -> dict:
    return {
        "id": row.id,
        "branch_id": row.branch_id,
        "from_date": row.from_date.isoformat() if row.from_date else "",
        "to_date": row.to_date.isoformat() if row.to_date else "",
        "note": row.note or "",
        "is_active": bool(row.is_active),
        "locked_by": row.locked_by or "",
        "locked_by_id": row.locked_by_id,
        "locked_at": row.locked_at.isoformat() if row.locked_at else None,
        "unlocked_by": row.unlocked_by or "",
        "unlocked_by_id": row.unlocked_by_id,
        "unlocked_at": row.unlocked_at.isoformat() if row.unlocked_at else None,
    }


def list_period_locks(
    db: Session,
    *,
    branch_ids: list[int] | None = None,
    active_only: bool = True,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[AttendancePeriodLock]:
    q = db.query(AttendancePeriodLock)
    if active_only:
        q = q.filter(AttendancePeriodLock.is_active.is_(True))
    if branch_ids is not None:
        q = q.filter(
            (AttendancePeriodLock.branch_id.in_(branch_ids))
            | (AttendancePeriodLock.branch_id.is_(None))
        )
    if from_date:
        q = q.filter(AttendancePeriodLock.to_date >= from_date)
    if to_date:
        q = q.filter(AttendancePeriodLock.from_date <= to_date)
    return q.order_by(AttendancePeriodLock.from_date.desc(), AttendancePeriodLock.id.desc()).all()


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def log_snapshot(log: AttendanceLog | None) -> dict:
    if not log:
        return {}
    return {
        "id": log.id,
        "employee_id": log.employee_id,
        "emp_code": log.emp_code,
        "emp_name": log.emp_name,
        "department": log.department,
        "check_type": log.check_type,
        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        "confidence": log.confidence,
        "capture_path": log.capture_path,
        "note": log.note,
    }


def session_snapshot(session: AttendanceSession | None) -> dict:
    if not session:
        return {}
    return {
        "id": session.id,
        "employee_id": session.employee_id,
        "branch_id": session.branch_id,
        "shift_assignment_id": session.shift_assignment_id,
        "shift_id": session.shift_id,
        "work_date": session.work_date.isoformat() if session.work_date else None,
        "check_in_at": session.check_in_at.isoformat() if session.check_in_at else None,
        "check_out_at": session.check_out_at.isoformat() if session.check_out_at else None,
        "status": session.status,
        "review_type": session.review_type,
        "review_status": session.review_status,
        "worked_minutes": session.worked_minutes,
    }


def create_correction_audit(
    db: Session,
    *,
    action: str,
    reason: str,
    log: AttendanceLog | None = None,
    session: AttendanceSession | None = None,
    before_data: dict | None = None,
    after_data: dict | None = None,
    affected_session_ids: Iterable[int] | None = None,
    created_by: str = "",
    created_by_id: int | None = None,
    branch_id: int | None = None,
    employee_id: int | None = None,
    emp_code: str = "",
) -> AttendanceCorrectionAudit:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Vui lòng nhập lý do chỉnh sửa công")
    affected = list(dict.fromkeys(int(x) for x in (affected_session_ids or []) if x))
    row = AttendanceCorrectionAudit(
        log_id=log.id if log and log.id else None,
        session_id=session.id if session and session.id else (affected[0] if affected else None),
        employee_id=employee_id or (log.employee_id if log else None) or (session.employee_id if session else None),
        branch_id=branch_id if branch_id is not None else (session.branch_id if session else None),
        emp_code=emp_code or (log.emp_code if log else ""),
        action=action,
        reason=reason,
        before_data=json.dumps(before_data or {}, ensure_ascii=False, default=_json_default),
        after_data=json.dumps(after_data or {}, ensure_ascii=False, default=_json_default),
        affected_session_ids=json.dumps(affected, ensure_ascii=False),
        created_by=created_by or "",
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()
    return row
