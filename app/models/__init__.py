"""
app/models/__init__.py
Import tất cả models tại đây để SQLAlchemy nhận diện khi create_all().
"""

from app.models.base import Base
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.employee_branch_history import EmployeeBranchHistory
from app.models.employee_role import EmployeeRole
from app.models.face_enrollment import FaceEnrollmentSession
from app.models.attendance import (
    AttendanceAuditFinding,
    AttendanceAuditRun,
    AttendanceCorrectionAudit,
    AttendanceEvidence,
    AttendanceEvent,
    AttendanceLog,
    AttendancePeriodLock,
    AttendanceSession,
)
from app.models.user import User, EmailToken, RefreshToken
from app.models.leave import LeaveRequest, LeaveRequestDay
from app.models.calendar import WorkCalendar, WorkCalendarConfig
from app.models.shift import Shift, ShiftAssignment, ShiftPlanDraft, ShiftPlanDraftAssignment
from app.models.integration import AIProviderSetting
__all__ = [
    "Base", "Branch", "Employee", "EmployeeBranchHistory", "EmployeeRole",
    "FaceEnrollmentSession",
    "AttendanceSession", "AttendanceEvent", "AttendanceEvidence",
    "AttendanceAuditRun", "AttendanceAuditFinding", "AttendanceCorrectionAudit",
    "AttendanceLog", "AttendancePeriodLock",
    "User", "EmailToken", "RefreshToken",
    "LeaveRequest", "LeaveRequestDay", "WorkCalendar", "WorkCalendarConfig",
    "Shift", "ShiftAssignment", "ShiftPlanDraft", "ShiftPlanDraftAssignment",
    "AIProviderSetting",
]
