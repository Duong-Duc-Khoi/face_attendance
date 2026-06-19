"""Payroll-facing attendance rules.

AttendanceLog is raw evidence. AttendanceSession and ShiftAssignment are the
payroll interpretation layer.
"""
from __future__ import annotations

from datetime import date

from app.models.attendance import AttendanceSession
from app.models.shift import Shift, ShiftAssignment
from app.services.leave_policy import LEAVE_ASSIGNMENT_STATUS
from app.services.shift_service import shift_window


def session_counts_as_work(session: AttendanceSession | None) -> bool:
    if not session:
        return False
    if (
        session.review_type == "overtime"
        and session.review_status in ("pending_review", "rejected")
        and session.check_in_at
        and session.check_out_at
    ):
        return session.status == "completed"
    if session.review_status in ("pending_review", "rejected"):
        return False
    if session.status == "open":
        return False
    if session.status in ("missing_checkout", "missing_checkin"):
        return bool(session.review_status == "approved" and session.check_in_at and session.check_out_at)
    if session.status in ("absent", "cancelled"):
        return False
    return bool(session.check_in_at and session.check_out_at)


def session_needs_review(session: AttendanceSession | None) -> bool:
    return bool(session and session.review_status == "pending_review")


def confirmed_absent(session: AttendanceSession | None) -> bool:
    return bool(
        session
        and session.status == "absent"
        and session.review_status in ("approved", "rejected")
    )


def missing_checkout_recorded(session: AttendanceSession | None) -> bool:
    return bool(
        session
        and session.review_status == "approved"
        and (session.status == "missing_checkout" or session.review_type == "missing_checkout")
    )


def missing_checkin_recorded(session: AttendanceSession | None) -> bool:
    return bool(
        session
        and session.review_status == "approved"
        and (session.status == "missing_checkin" or session.review_type == "missing_checkin")
    )


def payable_work_minutes(session: AttendanceSession | None) -> int:
    if not session_counts_as_work(session):
        return 0
    if session.check_in_at and session.check_out_at:
        worked = max(0, int((session.check_out_at - session.check_in_at).total_seconds() / 60))
    else:
        worked = max(0, int(session.worked_minutes or 0))
    if session.review_type == "overtime" and session.review_status in ("pending_review", "rejected"):
        worked -= max(0, int(session.overtime_minutes or 0))
    return max(0, worked)


def recorded_overtime_minutes(session: AttendanceSession | None) -> int:
    if not session or session.review_status in ("pending_review", "rejected"):
        return 0
    if session.review_type == "overtime" and session.review_status != "approved":
        return 0
    return max(0, int(session.overtime_minutes or 0))


def shift_paid_minutes(work_date: date, shift: Shift | None) -> int:
    if not shift:
        return 0
    start, end, _from, _until = shift_window(work_date, shift)
    return max(0, int((end - start).total_seconds() / 60))


def leave_credit_for_assignment(assignment: ShiftAssignment, shift: Shift | None) -> tuple[int, int]:
    if assignment.status != LEAVE_ASSIGNMENT_STATUS:
        return 0, 0
    return 1, shift_paid_minutes(assignment.work_date, shift)


def review_status_label(value: str | None) -> str:
    return {
        "none": "Không cần duyệt",
        "pending_review": "Chờ duyệt",
        "approved": "Đã duyệt",
        "rejected": "Từ chối",
    }.get(value or "none", value or "Không cần duyệt")


def session_status_label(session: AttendanceSession | None) -> str:
    if not session:
        return "Chưa có chấm công"
    return {
        "open": "Đang mở",
        "completed": "Hoàn tất",
        "missing_checkout": "Quên checkout",
        "missing_checkin": "Thiếu check-in",
        "absent": "Vắng",
        "cancelled": "Đã hủy",
    }.get(session.status or "", session.status or "Chưa xác định")
