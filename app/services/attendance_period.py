"""Attendance period locks and correction audit helpers."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.attendance import (
    AttendanceCorrectionAudit,
    AttendanceLog,
    AttendancePeriodLock,
    AttendanceSession,
)
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.user import User


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


def _json_loads(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _format_datetime_text(value: str | datetime | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    return dt.strftime("%H:%M %d/%m/%Y")


def _format_date_text(value: str | date | datetime | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        raw = str(value)
        try:
            d = date.fromisoformat(raw[:10])
        except ValueError:
            return raw
    return d.strftime("%d/%m/%Y")


FIELD_LABELS = {
    "id": "ID bản ghi",
    "employee_id": "ID nhân viên",
    "emp_code": "Mã nhân viên",
    "emp_name": "Tên nhân viên",
    "department": "Bộ phận",
    "branch_id": "Chi nhánh",
    "shift_assignment_id": "ID phân ca",
    "shift_id": "ID ca",
    "work_date": "Ngày công",
    "check_type": "Loại chấm công",
    "timestamp": "Thời điểm chấm công",
    "confidence": "Độ chính xác",
    "capture_path": "Ảnh bằng chứng",
    "note": "Ghi chú",
    "check_in_at": "Giờ vào",
    "check_out_at": "Giờ ra",
    "status": "Trạng thái phiên",
    "review_type": "Loại cần duyệt",
    "review_status": "Trạng thái duyệt",
    "worked_minutes": "Số phút làm",
}

VALUE_LABELS = {
    "check_in": "Vào làm",
    "check_out": "Ra về",
    "open": "Đang mở",
    "completed": "Hoàn tất",
    "missing_checkout": "Quên checkout",
    "missing_checkin": "Thiếu check-in",
    "absent": "Vắng",
    "cancelled": "Đã hủy",
    "pending_review": "Chờ duyệt",
    "approved": "Đã duyệt",
    "rejected": "Từ chối",
    "none": "Không cần duyệt",
    "overtime": "Tăng ca",
    "unscheduled": "Ngoài ca",
    "create": "Tạo thủ công",
    "update": "Cập nhật",
    "delete": "Xóa",
    "review": "Duyệt",
    "applied": "Đã áp dụng",
}


def _display_value(field: str, value) -> str:
    if value in (None, ""):
        return "—"
    if field in ("timestamp", "check_in_at", "check_out_at"):
        return _format_datetime_text(value) or "—"
    if field == "work_date":
        return _format_date_text(value) or "—"
    if field == "confidence":
        try:
            return f"{float(value) * 100:.0f}%"
        except (TypeError, ValueError):
            return str(value)
    if field.endswith("_minutes"):
        try:
            return f"{int(value)} phút"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, bool):
        return "Có" if value else "Không"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return VALUE_LABELS.get(str(value), str(value))


def correction_audit_changes(before_data: dict | None, after_data: dict | None) -> list[dict]:
    before_data = before_data or {}
    after_data = after_data or {}
    keys = sorted(set(before_data) | set(after_data))
    changes: list[dict] = []
    for key in keys:
        before = before_data.get(key)
        after = after_data.get(key)
        if before == after:
            continue
        changes.append({
            "field": key,
            "label": FIELD_LABELS.get(key, key),
            "before": before,
            "after": after,
            "before_text": _display_value(key, before),
            "after_text": _display_value(key, after),
        })
    return changes


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


def correction_audit_to_dict(
    row: AttendanceCorrectionAudit,
    *,
    branch_name: str = "",
    employee_name: str = "",
    actor_name: str = "",
) -> dict:
    before_data = _json_loads(row.before_data, {})
    after_data = _json_loads(row.after_data, {})
    affected_session_ids = _json_loads(row.affected_session_ids, [])
    employee_label = (
        employee_name
        or after_data.get("emp_name")
        or before_data.get("emp_name")
        or row.emp_code
        or (f"Nhân viên #{row.employee_id}" if row.employee_id else "")
    )
    return {
        "id": row.id,
        "log_id": row.log_id,
        "session_id": row.session_id,
        "employee_id": row.employee_id,
        "branch_id": row.branch_id,
        "branch_name": branch_name,
        "emp_code": row.emp_code or after_data.get("emp_code") or before_data.get("emp_code") or "",
        "employee_name": employee_label,
        "action": row.action,
        "action_label": VALUE_LABELS.get(row.action, row.action),
        "status": row.status,
        "status_label": VALUE_LABELS.get(row.status, row.status),
        "reason": row.reason or "",
        "before_data": before_data,
        "after_data": after_data,
        "changes": correction_audit_changes(before_data, after_data),
        "affected_session_ids": affected_session_ids if isinstance(affected_session_ids, list) else [],
        "created_by": row.created_by or actor_name,
        "created_by_id": row.created_by_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_at_text": _format_datetime_text(row.created_at),
    }


def _affected_session_conditions(session_id: int):
    sid = int(session_id)
    return [
        AttendanceCorrectionAudit.affected_session_ids == f"[{sid}]",
        AttendanceCorrectionAudit.affected_session_ids.like(f"[{sid},%"),
        AttendanceCorrectionAudit.affected_session_ids.like(f"%, {sid},%"),
        AttendanceCorrectionAudit.affected_session_ids.like(f"%, {sid}]"),
        AttendanceCorrectionAudit.affected_session_ids.like(f"%,{sid},%"),
        AttendanceCorrectionAudit.affected_session_ids.like(f"%,{sid}]"),
    ]


def list_correction_audits(
    db: Session,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    emp_code: str = "",
    employee_id: int | None = None,
    created_by_id: int | None = None,
    created_by: str = "",
    action: str = "",
    log_id: int | None = None,
    session_id: int | None = None,
    branch_ids: list[int] | None = None,
    limit: int = 100,
) -> list[dict]:
    q = db.query(AttendanceCorrectionAudit)
    if from_date:
        q = q.filter(AttendanceCorrectionAudit.created_at >= datetime.combine(from_date, datetime.min.time()))
    if to_date:
        q = q.filter(AttendanceCorrectionAudit.created_at <= datetime.combine(to_date, datetime.max.time()))
    if emp_code:
        q = q.filter(AttendanceCorrectionAudit.emp_code == emp_code)
    if employee_id:
        q = q.filter(AttendanceCorrectionAudit.employee_id == employee_id)
    if created_by_id:
        q = q.filter(AttendanceCorrectionAudit.created_by_id == created_by_id)
    if created_by:
        q = q.filter(AttendanceCorrectionAudit.created_by.ilike(f"%{created_by}%"))
    if action:
        q = q.filter(AttendanceCorrectionAudit.action == action)
    if log_id:
        q = q.filter(AttendanceCorrectionAudit.log_id == log_id)
    if session_id:
        q = q.filter(or_(
            AttendanceCorrectionAudit.session_id == session_id,
            *_affected_session_conditions(session_id),
        ))
    if branch_ids is not None:
        q = q.filter(or_(
            AttendanceCorrectionAudit.branch_id.in_(branch_ids),
            AttendanceCorrectionAudit.branch_id.is_(None),
        ))

    rows = (
        q.order_by(AttendanceCorrectionAudit.created_at.desc(), AttendanceCorrectionAudit.id.desc())
         .limit(min(max(int(limit or 100), 1), 500))
         .all()
    )
    branch_ids_for_rows = {row.branch_id for row in rows if row.branch_id}
    employee_ids = {row.employee_id for row in rows if row.employee_id}
    actor_ids = {row.created_by_id for row in rows if row.created_by_id}
    branches = {
        branch.id: branch.name
        for branch in db.query(Branch).filter(Branch.id.in_(list(branch_ids_for_rows))).all()
    } if branch_ids_for_rows else {}
    employees = {
        emp.id: emp.name
        for emp in db.query(Employee).filter(Employee.id.in_(list(employee_ids))).all()
    } if employee_ids else {}
    actors = {
        user.id: user.full_name or user.email
        for user in db.query(User).filter(User.id.in_(list(actor_ids))).all()
    } if actor_ids else {}
    return [
        correction_audit_to_dict(
            row,
            branch_name=branches.get(row.branch_id, ""),
            employee_name=employees.get(row.employee_id, ""),
            actor_name=actors.get(row.created_by_id, ""),
        )
        for row in rows
    ]


def correction_audit_counts_for_logs(db: Session, log_ids: Iterable[int]) -> dict[int, int]:
    ids = [int(log_id) for log_id in log_ids if log_id]
    if not ids:
        return {}
    rows = (
        db.query(AttendanceCorrectionAudit.log_id)
          .filter(AttendanceCorrectionAudit.log_id.in_(ids))
          .all()
    )
    counts: dict[int, int] = {}
    for (log_id,) in rows:
        if log_id:
            counts[int(log_id)] = counts.get(int(log_id), 0) + 1
    return counts


def correction_audit_counts_for_sessions(db: Session, session_ids: Iterable[int]) -> dict[int, int]:
    ids = [int(session_id) for session_id in session_ids if session_id]
    if not ids:
        return {}
    counts = {session_id: 0 for session_id in ids}
    rows = (
        db.query(AttendanceCorrectionAudit)
          .filter(or_(
              AttendanceCorrectionAudit.session_id.in_(ids),
              *[
                  condition
                  for session_id in ids
                  for condition in _affected_session_conditions(session_id)
              ],
          ))
          .all()
    )
    for row in rows:
        if row.session_id in counts:
            counts[int(row.session_id)] += 1
        affected = _json_loads(row.affected_session_ids, [])
        if isinstance(affected, list):
            for session_id in affected:
                try:
                    sid = int(session_id)
                except (TypeError, ValueError):
                    continue
                if sid in counts and sid != row.session_id:
                    counts[sid] += 1
    return {sid: count for sid, count in counts.items() if count > 0}


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
