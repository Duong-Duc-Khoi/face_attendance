"""Seed demo data for branches, employees, accounts, shifts, and attendance.

Run from the project root:
    python scripts/seed_demo_data.py

The script is idempotent for demo employees whose codes start with TP, OCD,
LTN, or DTH. It resets their schedule and attendance data, then recreates a
clean 14-day dataset ending yesterday.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, init_db
from app.core.security import hash_password
from app.models.attendance import (
    AttendanceAuditFinding,
    AttendanceCorrectionAudit,
    AttendanceEvent,
    AttendanceEvidence,
    AttendanceLog,
    AttendanceSession,
)
from app.models.branch import Branch
from app.models.calendar import WorkCalendarConfig
from app.models.employee import Employee
from app.models.employee_branch_history import EmployeeBranchHistory
from app.models.shift import Shift, ShiftAssignment
from app.models.user import User
from app.services.shift_service import shift_window


VIETNAM_TZ = timezone(timedelta(hours=7))
DEFAULT_PASSWORD = "Demo@123456"
ADMIN_EMAIL = "admin.demo@faceattend.local"
SEED_EMP_PREFIXES = ("TP", "OCD", "LTN", "DTH")
BASE_HIRE_DATE = date(2025, 1, 6)


@dataclass(frozen=True)
class BranchSeed:
    prefix: str
    name: str
    address: str
    phone: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class EmployeeTemplate:
    title: str
    store_role: str
    system_role: str
    employment_type: str
    job_role: str
    job_roles: tuple[str, ...]
    base_salary: Decimal | None
    hourly_rate: Decimal


BRANCHES: tuple[BranchSeed, ...] = (
    BranchSeed(
        prefix="TP",
        name="Thái Phiên",
        address="12 Thái Phiên, Hai Bà Trưng, Hà Nội",
        phone="02439910001",
        names=(
            "Nguyễn Minh An",
            "Trần Thu Hà",
            "Phạm Quốc Huy",
            "Lê Ngọc Mai",
            "Võ Đức Long",
            "Đỗ Thảo Linh",
            "Bùi Anh Khoa",
            "Hoàng Mỹ Duyên",
            "Đặng Tuấn Kiệt",
            "Vũ Bảo Ngọc",
        ),
    ),
    BranchSeed(
        prefix="OCD",
        name="Ô Chợ Dừa",
        address="85 Ô Chợ Dừa, Đống Đa, Hà Nội",
        phone="02439910002",
        names=(
            "Nguyễn Hải Nam",
            "Phạm Thu Trang",
            "Trần Gia Bảo",
            "Lê Phương Anh",
            "Đỗ Minh Quân",
            "Vũ Khánh Linh",
            "Hoàng Tuấn Anh",
            "Bùi Ngọc Ánh",
            "Đặng Hà My",
            "Phan Nhật Minh",
        ),
    ),
    BranchSeed(
        prefix="LTN",
        name="Lê Thanh Nghị",
        address="42 Lê Thanh Nghị, Hai Bà Trưng, Hà Nội",
        phone="02439910003",
        names=(
            "Lê Anh Dũng",
            "Nguyễn Mai Chi",
            "Trần Đức Mạnh",
            "Phạm Hồng Nhung",
            "Đỗ Quốc Việt",
            "Vũ Thanh Hằng",
            "Bùi Minh Đức",
            "Hoàng Lan Anh",
            "Đặng Quang Huy",
            "Phan Bảo Trâm",
        ),
    ),
    BranchSeed(
        prefix="DTH",
        name="Đinh Tiên Hoàng",
        address="24 Đinh Tiên Hoàng, Hoàn Kiếm, Hà Nội",
        phone="02439910004",
        names=(
            "Trần Minh Khôi",
            "Nguyễn Thu Giang",
            "Phạm Anh Tuấn",
            "Lê Bảo Châu",
            "Đỗ Hải Đăng",
            "Vũ Minh Tâm",
            "Bùi Thùy Dương",
            "Hoàng Đức Anh",
            "Đặng Phương Linh",
            "Phan Gia Hân",
        ),
    ),
)


EMPLOYEE_TEMPLATES: tuple[EmployeeTemplate, ...] = (
    EmployeeTemplate(
        title="Cửa hàng trưởng",
        store_role="store_manager",
        system_role="manager",
        employment_type="full_time",
        job_role="cashier",
        job_roles=("cashier", "barista", "table_service"),
        base_salary=Decimal("15000000"),
        hourly_rate=Decimal("80000"),
    ),
    EmployeeTemplate(
        title="Cửa hàng phó",
        store_role="assistant_manager",
        system_role="manager",
        employment_type="full_time",
        job_role="barista",
        job_roles=("cashier", "barista", "table_service"),
        base_salary=Decimal("12000000"),
        hourly_rate=Decimal("70000"),
    ),
    EmployeeTemplate(
        title="Full-time Thu ngân",
        store_role="staff",
        system_role="staff",
        employment_type="full_time",
        job_role="cashier",
        job_roles=("cashier", "table_service"),
        base_salary=Decimal("9000000"),
        hourly_rate=Decimal("50000"),
    ),
    EmployeeTemplate(
        title="Full-time Pha chế",
        store_role="staff",
        system_role="staff",
        employment_type="full_time",
        job_role="barista",
        job_roles=("barista", "cashier"),
        base_salary=Decimal("9200000"),
        hourly_rate=Decimal("52000"),
    ),
    EmployeeTemplate(
        title="Full-time Phục vụ",
        store_role="staff",
        system_role="staff",
        employment_type="full_time",
        job_role="table_service",
        job_roles=("table_service", "barista"),
        base_salary=Decimal("8500000"),
        hourly_rate=Decimal("48000"),
    ),
    EmployeeTemplate(
        title="Part-time Pha chế",
        store_role="staff",
        system_role="staff",
        employment_type="part_time",
        job_role="barista",
        job_roles=("barista",),
        base_salary=None,
        hourly_rate=Decimal("35000"),
    ),
    EmployeeTemplate(
        title="Part-time Phục vụ",
        store_role="staff",
        system_role="staff",
        employment_type="part_time",
        job_role="table_service",
        job_roles=("table_service",),
        base_salary=None,
        hourly_rate=Decimal("32000"),
    ),
    EmployeeTemplate(
        title="Part-time Thu ngân",
        store_role="staff",
        system_role="staff",
        employment_type="part_time",
        job_role="cashier",
        job_roles=("cashier", "table_service"),
        base_salary=None,
        hourly_rate=Decimal("34000"),
    ),
    EmployeeTemplate(
        title="Part-time Phục vụ",
        store_role="staff",
        system_role="staff",
        employment_type="part_time",
        job_role="table_service",
        job_roles=("table_service",),
        base_salary=None,
        hourly_rate=Decimal("32000"),
    ),
    EmployeeTemplate(
        title="Part-time Pha chế",
        store_role="staff",
        system_role="staff",
        employment_type="part_time",
        job_role="barista",
        job_roles=("barista", "cashier"),
        base_salary=None,
        hourly_rate=Decimal("35000"),
    ),
)


SHIFT_DEFINITIONS: tuple[dict, ...] = (
    {
        "code": "morning",
        "name": "Ca sáng",
        "work_start": "08:00",
        "work_end": "12:00",
        "break_minutes": 0,
    },
    {
        "code": "afternoon",
        "name": "Ca chiều",
        "work_start": "12:00",
        "work_end": "17:00",
        "break_minutes": 30,
    },
    {
        "code": "evening",
        "name": "Ca tối",
        "work_start": "17:00",
        "work_end": "22:00",
        "break_minutes": 30,
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed complete demo attendance data.")
    parser.add_argument("--from-date", default="", help="Start date, YYYY-MM-DD.")
    parser.add_argument("--to-date", default="", help="End date, YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=14, help="Number of days when date range is omitted.")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Password for demo accounts.")
    parser.add_argument(
        "--keep-passwords",
        action="store_true",
        help="Do not reset passwords for existing demo accounts.",
    )
    return parser.parse_args()


def resolve_date_range(args: argparse.Namespace) -> tuple[date, date]:
    if bool(args.from_date) != bool(args.to_date):
        raise SystemExit("Use both --from-date and --to-date, or neither.")
    if args.from_date and args.to_date:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
    else:
        if args.days < 1 or args.days > 62:
            raise SystemExit("--days must be between 1 and 62.")
        end = datetime.now(VIETNAM_TZ).date() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
    if end < start:
        raise SystemExit("--to-date must be greater than or equal to --from-date.")
    if (end - start).days > 62:
        raise SystemExit("The seed date range is limited to 63 days.")
    return start, end


def daterange(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def demo_emp_codes() -> list[str]:
    return [
        f"{branch.prefix}{idx:03d}"
        for branch in BRANCHES
        for idx in range(1, len(EMPLOYEE_TEMPLATES) + 1)
    ]


def demo_email(emp_code: str) -> str:
    return f"{emp_code.lower()}@demo.faceattend.local"


def reset_pk_sequence(db: Session, table_name: str) -> None:
    if not table_name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe table name: {table_name}")
    db.execute(
        text(
            f"""
            SELECT setval(
                pg_get_serial_sequence(:table_name, 'id'),
                GREATEST(COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1, 1),
                false
            )
            """
        ),
        {"table_name": table_name},
    )


def reset_demo_sequences(db: Session) -> None:
    for model in (
        Branch,
        WorkCalendarConfig,
        User,
        Employee,
        EmployeeBranchHistory,
        Shift,
        ShiftAssignment,
        AttendanceSession,
        AttendanceLog,
        AttendanceEvent,
        AttendanceEvidence,
        AttendanceAuditFinding,
        AttendanceCorrectionAudit,
    ):
        reset_pk_sequence(db, model.__tablename__)


def phone_for(branch_index: int, employee_index: int) -> str:
    return f"09{branch_index + 1:02d}{employee_index + 1:02d}{employee_index + 17:04d}"


def find_branch(db: Session, name: str) -> Branch | None:
    return (
        db.query(Branch)
        .filter(func.lower(Branch.name) == name.lower())
        .order_by(Branch.id.asc())
        .first()
    )


def upsert_branch(db: Session, seed: BranchSeed) -> Branch:
    branch = find_branch(db, seed.name)
    if not branch:
        branch = Branch(
            name=seed.name,
            address=seed.address,
            phone=seed.phone,
            is_active=True,
        )
        db.add(branch)
        db.flush()
        return branch

    branch.name = seed.name
    branch.is_active = True
    if not branch.address:
        branch.address = seed.address
    if not branch.phone:
        branch.phone = seed.phone
    return branch


def upsert_work_calendar_config(db: Session, branch_id: int, admin_user: User) -> None:
    config = db.query(WorkCalendarConfig).filter_by(branch_id=branch_id).first()
    if not config:
        config = WorkCalendarConfig(branch_id=branch_id)
        db.add(config)
    config.work_days = "1,2,3,4,5,6,7"
    config.created_by = config.created_by or ADMIN_EMAIL
    config.created_by_id = config.created_by_id or admin_user.id


def upsert_global_admin(db: Session, password_hash: str, keep_passwords: bool) -> User:
    user = db.query(User).filter_by(email=ADMIN_EMAIL).first()
    if not user:
        user = User(email=ADMIN_EMAIL)
        db.add(user)
    user.full_name = "Quản trị Demo"
    if not keep_passwords or not user.hashed_password:
        user.hashed_password = password_hash
    user.role = "admin"
    user.is_active = True
    user.is_email_verified = True
    user.is_approved = True
    db.flush()
    return user


def upsert_user(
    db: Session,
    *,
    email: str,
    full_name: str,
    role: str,
    password_hash: str,
    keep_passwords: bool,
) -> User:
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(email=email)
        db.add(user)
    user.full_name = full_name
    if not keep_passwords or not user.hashed_password:
        user.hashed_password = password_hash
    user.role = role
    user.is_active = True
    user.is_email_verified = True
    user.is_approved = True
    db.flush()
    return user


def clear_demo_time_data(db: Session, emp_codes: list[str]) -> None:
    existing = db.query(Employee).filter(Employee.emp_code.in_(emp_codes)).all()
    employee_ids = [emp.id for emp in existing if emp.id]

    if employee_ids:
        db.query(AttendanceEvidence).filter(
            or_(
                AttendanceEvidence.emp_code.in_(emp_codes),
                AttendanceEvidence.employee_id.in_(employee_ids),
            )
        ).delete(synchronize_session=False)
        db.query(AttendanceAuditFinding).filter(
            or_(
                AttendanceAuditFinding.emp_code.in_(emp_codes),
                AttendanceAuditFinding.employee_id.in_(employee_ids),
            )
        ).delete(synchronize_session=False)
        db.query(AttendanceCorrectionAudit).filter(
            or_(
                AttendanceCorrectionAudit.emp_code.in_(emp_codes),
                AttendanceCorrectionAudit.employee_id.in_(employee_ids),
            )
        ).delete(synchronize_session=False)
        db.query(AttendanceEvent).filter(AttendanceEvent.employee_id.in_(employee_ids)).delete(
            synchronize_session=False
        )
        db.query(AttendanceSession).filter(AttendanceSession.employee_id.in_(employee_ids)).delete(
            synchronize_session=False
        )
    else:
        db.query(AttendanceEvidence).filter(AttendanceEvidence.emp_code.in_(emp_codes)).delete(
            synchronize_session=False
        )
        db.query(AttendanceAuditFinding).filter(AttendanceAuditFinding.emp_code.in_(emp_codes)).delete(
            synchronize_session=False
        )
        db.query(AttendanceCorrectionAudit).filter(AttendanceCorrectionAudit.emp_code.in_(emp_codes)).delete(
            synchronize_session=False
        )

    db.query(AttendanceLog).filter(AttendanceLog.emp_code.in_(emp_codes)).delete(
        synchronize_session=False
    )
    db.query(ShiftAssignment).filter(ShiftAssignment.emp_code.in_(emp_codes)).delete(
        synchronize_session=False
    )
    db.query(EmployeeBranchHistory).filter(EmployeeBranchHistory.emp_code.in_(emp_codes)).delete(
        synchronize_session=False
    )
    db.flush()


def upsert_employee(
    db: Session,
    *,
    branch: Branch,
    branch_seed: BranchSeed,
    branch_index: int,
    employee_index: int,
    template: EmployeeTemplate,
    password_hash: str,
    keep_passwords: bool,
) -> Employee:
    emp_code = f"{branch_seed.prefix}{employee_index + 1:03d}"
    full_name = branch_seed.names[employee_index]
    email = demo_email(emp_code)
    user = upsert_user(
        db,
        email=email,
        full_name=full_name,
        role=template.system_role,
        password_hash=password_hash,
        keep_passwords=keep_passwords,
    )

    linked = (
        db.query(Employee)
        .filter(Employee.user_id == user.id, Employee.emp_code != emp_code)
        .first()
    )
    if linked:
        linked.user_id = None

    employee = db.query(Employee).filter_by(emp_code=emp_code).first()
    if not employee:
        employee = Employee(emp_code=emp_code)
        db.add(employee)

    employee.user_id = user.id
    employee.branch_id = branch.id
    employee.name = full_name
    employee.full_name = full_name
    employee.department = "Vận hành cửa hàng"
    employee.position = template.title
    employee.store_role = template.store_role
    employee.job_role = template.job_role
    employee.job_roles = json.dumps(list(template.job_roles), ensure_ascii=False)
    employee.employment_type = template.employment_type
    employee.hourly_rate = template.hourly_rate
    employee.base_salary = template.base_salary
    employee.email = email
    employee.phone = phone_for(branch_index, employee_index)
    employee.face_path = ""
    employee.avatar_url = ""
    employee.hire_date = BASE_HIRE_DATE + timedelta(days=employee_index * 7 + branch_index)
    employee.status = "active"
    employee.is_active = True
    employee.deactivated_at = None
    db.flush()

    db.add(
        EmployeeBranchHistory(
            employee_id=employee.id,
            emp_code=employee.emp_code,
            branch_id=branch.id,
            effective_from=BASE_HIRE_DATE,
            effective_to=None,
            changed_by=ADMIN_EMAIL,
            note="Seed dữ liệu demo ban đầu",
        )
    )
    return employee


def upsert_branch_shifts(db: Session, branch: Branch) -> dict[str, Shift]:
    shifts: dict[str, Shift] = {}
    for item in SHIFT_DEFINITIONS:
        shift = db.query(Shift).filter_by(branch_id=branch.id, code=item["code"]).first()
        if not shift:
            shift = Shift(branch_id=branch.id, code=item["code"])
            db.add(shift)
        shift.name = item["name"]
        shift.work_start = item["work_start"]
        shift.work_end = item["work_end"]
        shift.required_position = ""
        shift.late_threshold_minutes = 15
        shift.early_checkin_minutes = 30
        shift.auto_checkout_minutes = 120
        shift.break_minutes = item["break_minutes"]
        shift.is_overnight = False
        shift.is_active = True
        shift.note = "Ca demo chuẩn theo chi nhánh"
        db.flush()
        shifts[item["code"]] = shift
    return shifts


def rotated(items: list[Employee], start: int) -> list[Employee]:
    if not items:
        return []
    offset = start % len(items)
    return items[offset:] + items[:offset]


def staff_for_day(employees: list[Employee], day_index: int) -> dict[str, list[Employee]]:
    manager = employees[0]
    assistant = employees[1]
    staff = employees[2:]
    evening_lead = manager if day_index % 2 == 0 else assistant
    evening_support = rotated(staff, day_index * 2)[:2]
    used_codes = {evening_lead.emp_code, *(emp.emp_code for emp in evening_support)}
    available = [emp for emp in rotated(employees, day_index + 3) if emp.emp_code not in used_codes]
    return {
        "morning": [available[0]],
        "afternoon": available[1:4],
        "evening": [evening_lead, *evening_support],
    }


def attendance_times(work_date: date, shift: Shift, serial: int) -> tuple[datetime, datetime]:
    start, end, _checkin_from, _checkout_until = shift_window(work_date, shift)
    checkin_offsets = (-8, -5, -3, 0, 2, 4, 6, 8, 10, 12)
    checkout_offsets = (0, 2, 4, 5, 7, 8, 9, 10, 12, 14)
    check_in_at = start + timedelta(minutes=checkin_offsets[serial % len(checkin_offsets)])
    check_out_at = end + timedelta(minutes=checkout_offsets[(serial * 3) % len(checkout_offsets)])
    return check_in_at, check_out_at


def session_values(
    assignment: ShiftAssignment,
    shift: Shift,
    check_in_at: datetime,
    check_out_at: datetime,
) -> dict:
    shift_start, shift_end, _checkin_from, _checkout_until = shift_window(assignment.work_date, shift)
    raw_late = max(0, int((check_in_at - shift_start).total_seconds() / 60))
    grace = shift.late_threshold_minutes or 0
    early_leave = max(0, int((shift_end - check_out_at).total_seconds() / 60))
    raw_overtime = max(0, int((check_out_at - shift_end).total_seconds() / 60))
    overtime = raw_overtime if raw_overtime > 30 else 0
    gross = int((check_out_at - check_in_at).total_seconds() / 60)
    worked = max(0, gross)
    return {
        "status": "completed",
        "check_in_status": "late" if raw_late > grace else "on_time",
        "check_out_status": "early_leave" if early_leave else ("overtime" if overtime else "normal"),
        "late_minutes": max(0, raw_late - grace),
        "early_leave_minutes": early_leave,
        "overtime_minutes": overtime,
        "worked_minutes": worked,
        "break_minutes": shift.break_minutes or 0,
        "review_status": "pending_review" if overtime else "none",
        "review_type": "overtime" if overtime else "",
    }


def create_assignment_with_attendance(
    db: Session,
    *,
    employee: Employee,
    branch: Branch,
    shift: Shift,
    work_date: date,
    serial: int,
    admin_user: User,
) -> ShiftAssignment:
    assignment = ShiftAssignment(
        employee_id=employee.id,
        branch_id=branch.id,
        emp_code=employee.emp_code,
        shift_id=shift.id,
        work_date=work_date,
        status="scheduled",
        note="",
        assigned_by=ADMIN_EMAIL,
        assigned_by_id=admin_user.id,
    )
    db.add(assignment)
    db.flush()

    check_in_at, check_out_at = attendance_times(work_date, shift, serial)
    values = session_values(assignment, shift, check_in_at, check_out_at)
    session = AttendanceSession(
        employee_id=employee.id,
        branch_id=branch.id,
        shift_assignment_id=assignment.id,
        shift_id=shift.id,
        work_date=work_date,
        check_in_at=check_in_at,
        check_out_at=check_out_at,
        status=values["status"],
        check_in_status=values["check_in_status"],
        check_out_status=values["check_out_status"],
        late_minutes=values["late_minutes"],
        early_leave_minutes=values["early_leave_minutes"],
        overtime_minutes=values["overtime_minutes"],
        worked_minutes=values["worked_minutes"],
        break_minutes=values["break_minutes"],
        source="manual",
        note="",
        review_status=values["review_status"],
        review_type=values["review_type"],
        created_by_id=admin_user.id,
        updated_by_id=admin_user.id,
    )
    db.add(session)
    db.flush()

    for check_type, timestamp in (("check_in", check_in_at), ("check_out", check_out_at)):
        log = AttendanceLog(
            employee_id=employee.id,
            emp_code=employee.emp_code,
            emp_name=employee.name,
            department=employee.position,
            check_type=check_type,
            timestamp=timestamp,
            confidence=0.98,
            capture_path="",
            note="",
        )
        db.add(log)
        db.flush()
        db.add(
            AttendanceEvent(
                session_id=session.id,
                employee_id=employee.id,
                branch_id=branch.id,
                event_type=check_type,
                event_time=timestamp,
                confidence=0.98,
                capture_path="",
                face_bbox="",
                image_hash="",
                device_id=f"seed-{branch.id}",
                source="manual",
                created_by_id=admin_user.id,
                note="",
            )
        )
    return assignment


def seed_schedule_and_attendance(
    db: Session,
    *,
    branch: Branch,
    employees: list[Employee],
    shifts: dict[str, Shift],
    days: list[date],
    admin_user: User,
) -> int:
    created = 0
    serial = 0
    for day_index, work_date in enumerate(days):
        plan = staff_for_day(employees, day_index)
        for shift_code in ("morning", "afternoon", "evening"):
            for employee in plan[shift_code]:
                create_assignment_with_attendance(
                    db,
                    employee=employee,
                    branch=branch,
                    shift=shifts[shift_code],
                    work_date=work_date,
                    serial=serial,
                    admin_user=admin_user,
                )
                serial += 1
                created += 1
    return created


def seed(args: argparse.Namespace) -> dict:
    start_date, end_date = resolve_date_range(args)
    days = daterange(start_date, end_date)
    password_hash = hash_password(args.password)

    init_db()
    db = SessionLocal()
    try:
        emp_codes = demo_emp_codes()
        reset_demo_sequences(db)
        clear_demo_time_data(db, emp_codes)
        reset_demo_sequences(db)
        admin_user = upsert_global_admin(db, password_hash, args.keep_passwords)

        branches_by_prefix: dict[str, Branch] = {}
        employees_by_prefix: dict[str, list[Employee]] = {}
        shifts_by_prefix: dict[str, dict[str, Shift]] = {}

        for branch_index, branch_seed in enumerate(BRANCHES):
            branch = upsert_branch(db, branch_seed)
            branches_by_prefix[branch_seed.prefix] = branch
            upsert_work_calendar_config(db, branch.id, admin_user)
            employees: list[Employee] = []
            for employee_index, template in enumerate(EMPLOYEE_TEMPLATES):
                employees.append(
                    upsert_employee(
                        db,
                        branch=branch,
                        branch_seed=branch_seed,
                        branch_index=branch_index,
                        employee_index=employee_index,
                        template=template,
                        password_hash=password_hash,
                        keep_passwords=args.keep_passwords,
                    )
                )
            employees_by_prefix[branch_seed.prefix] = employees
            shifts_by_prefix[branch_seed.prefix] = upsert_branch_shifts(db, branch)

        assignments = 0
        for branch_seed in BRANCHES:
            assignments += seed_schedule_and_attendance(
                db,
                branch=branches_by_prefix[branch_seed.prefix],
                employees=employees_by_prefix[branch_seed.prefix],
                shifts=shifts_by_prefix[branch_seed.prefix],
                days=days,
                admin_user=admin_user,
            )

        db.commit()
        logs = assignments * 2
        return {
            "from_date": start_date.isoformat(),
            "to_date": end_date.isoformat(),
            "branches": len(BRANCHES),
            "employees": len(emp_codes),
            "accounts": len(emp_codes) + 1,
            "assignments": assignments,
            "attendance_sessions": assignments,
            "attendance_logs": logs,
            "password": args.password,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    summary = seed(parse_args())
    print("Seed demo data completed.")
    print(f"Date range: {summary['from_date']} -> {summary['to_date']}")
    print(f"Branches: {summary['branches']}")
    print(f"Employees: {summary['employees']}")
    print(f"Accounts: {summary['accounts']}")
    print(f"Shift assignments: {summary['assignments']}")
    print(f"Attendance sessions: {summary['attendance_sessions']}")
    print(f"Attendance logs: {summary['attendance_logs']}")
    print(f"Demo password: {summary['password']}")
    print(f"Admin login: {ADMIN_EMAIL}")
    print("Example manager login: tp001@demo.faceattend.local")


if __name__ == "__main__":
    main()
