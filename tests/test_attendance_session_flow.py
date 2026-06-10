import unittest
from datetime import date, datetime, time
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import database
from app.models import Base
from app.models.attendance import AttendanceCorrectionAudit, AttendanceLog, AttendancePeriodLock, AttendanceSession
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.services.attendance import (
    create_manual_attendance_log,
    delete_attendance_log,
    update_attendance_log,
)
from app.services.shift_service import (
    assign_shift,
    find_open_session_for_time,
    find_shift_assignment_for_checkin,
)
from app.api.v1.reports import (
    AttendanceSessionReviewRequest,
    _session_counts_as_work,
    review_attendance_session,
)
from app.services.attendance_policy import (
    confirmed_absent,
    leave_credit_for_assignment,
    recorded_overtime_minutes,
    session_counts_as_work,
)


class AttendanceSessionFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        database._engine = self.engine
        database._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.db = database.SessionLocal()
        self.work_date = date.today()
        self.branch = Branch(name="Store 1", is_active=True)
        self.employee = Employee(emp_code="NV001", name="Nhan Vien 1", branch_id=1, is_active=True)
        self.db.add(self.branch)
        self.db.flush()
        self.employee.branch_id = self.branch.id
        self.db.add(self.employee)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        database._engine = None
        database._SessionLocal = None

    def _dt(self, value: str) -> datetime:
        return datetime.combine(self.work_date, time.fromisoformat(value))

    def _shift(self, name: str, code: str, start: str, end: str, early: int = 30, auto: int = 180, break_minutes: int = 0) -> Shift:
        shift = Shift(
            branch_id=self.branch.id,
            name=name,
            code=code,
            work_start=start,
            work_end=end,
            early_checkin_minutes=early,
            auto_checkout_minutes=auto,
            late_threshold_minutes=15,
            break_minutes=break_minutes,
            is_active=True,
        )
        self.db.add(shift)
        self.db.commit()
        return shift

    def _assignment(self, shift: Shift) -> ShiftAssignment:
        assignment = ShiftAssignment(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            emp_code=self.employee.emp_code,
            shift_id=shift.id,
            work_date=self.work_date,
            status="scheduled",
        )
        self.db.add(assignment)
        self.db.commit()
        return assignment

    def test_open_session_wins_over_overlapping_next_shift(self):
        first = self._shift("Morning", "morning", "08:00", "12:00", early=120, auto=240)
        second = self._shift("Noon", "noon", "12:00", "16:00", early=180, auto=180)
        first_assignment = self._assignment(first)
        second_assignment = self._assignment(second)
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=first_assignment.id,
            shift_id=first.id,
            work_date=self.work_date,
            check_in_at=self._dt("09:00"),
            status="open",
        )
        self.db.add(session)
        self.db.commit()

        open_session, assignment, shift = find_open_session_for_time(self.employee.emp_code, self._dt("12:30"), self.db)
        self.assertEqual(open_session.id, session.id)
        self.assertEqual(assignment.id, first_assignment.id)
        self.assertEqual(shift.id, first.id)

        checkin_assignment, _checkin_shift = find_shift_assignment_for_checkin(self.employee.emp_code, self._dt("12:30"), self.db)
        self.assertEqual(checkin_assignment.id, second_assignment.id)

    def test_assign_shift_reconciles_existing_logs_into_session(self):
        shift = self._shift("Day", "day", "09:00", "13:00", break_minutes=30)
        self.db.add_all([
            AttendanceLog(
                employee_id=self.employee.id,
                emp_code=self.employee.emp_code,
                emp_name=self.employee.name,
                department="",
                check_type="check_in",
                timestamp=self._dt("08:55"),
                confidence=0.9,
            ),
            AttendanceLog(
                employee_id=self.employee.id,
                emp_code=self.employee.emp_code,
                emp_name=self.employee.name,
                department="",
                check_type="check_out",
                timestamp=self._dt("13:05"),
                confidence=0.9,
            ),
        ])
        self.db.commit()

        assignment = assign_shift(self.employee.emp_code, shift.id, self.work_date, db=self.db)
        session = self.db.query(AttendanceSession).filter_by(shift_assignment_id=assignment["id"]).first()

        self.assertIsNotNone(session)
        self.assertEqual(session.check_in_at, self._dt("08:55"))
        self.assertEqual(session.check_out_at, self._dt("13:05"))
        self.assertEqual(session.worked_minutes, 220)

    def test_manual_attendance_create_update_delete_rebuilds_session(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)

        checkin = create_manual_attendance_log(
            self.employee.emp_code,
            "check_in",
            self._dt("09:05").strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Quên chấm vào",
        )
        checkout = create_manual_attendance_log(
            self.employee.emp_code,
            "check_out",
            self._dt("13:00").strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Quên chấm ra",
        )
        self.db.expire_all()
        session = self.db.query(AttendanceSession).filter_by(shift_assignment_id=assignment.id).first()
        self.assertEqual(session.worked_minutes, 235)

        update_attendance_log(
            checkout["id"],
            timestamp_str=self._dt("12:00").strftime("%Y-%m-%dT%H:%M:%S"),
            updated_by="admin@example.com",
            reason="Sửa giờ ra theo xác nhận quản lý",
        )
        self.db.expire_all()
        session = self.db.query(AttendanceSession).filter_by(shift_assignment_id=assignment.id).first()
        self.assertEqual(session.check_out_at, self._dt("12:00"))
        self.assertEqual(session.worked_minutes, 175)

        delete_result = delete_attendance_log(checkout["id"], deleted_by="admin@example.com", reason="Xóa bản ghi sai")
        self.assertTrue(delete_result["deleted"])
        self.db.expire_all()
        session = self.db.query(AttendanceSession).filter_by(shift_assignment_id=assignment.id).first()
        self.assertEqual(session.check_in_at, self._dt("09:05"))
        self.assertIsNone(session.check_out_at)
        self.assertEqual(session.worked_minutes, 0)
        self.assertEqual(session.status, "open")
        self.assertIsNotNone(checkin)
        self.assertGreaterEqual(self.db.query(AttendanceCorrectionAudit).count(), 4)

    def test_missing_checkout_approved_counts_as_work(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=self.work_date,
            check_in_at=self._dt("09:00"),
            check_out_at=self._dt("13:00"),
            status="missing_checkout",
            review_type="missing_checkout",
            review_status="pending_review",
            worked_minutes=240,
        )
        self.db.add(session)
        self.db.commit()

        self.assertFalse(_session_counts_as_work(session))
        user = SimpleNamespace(id=1, role="admin", full_name="Admin", email="admin@example.com")
        body = AttendanceSessionReviewRequest(review_status="approved", note="OK")
        review_attendance_session(session.id, body, self.db, user)
        self.db.refresh(session)

        self.assertEqual(session.status, "completed")
        self.assertEqual(session.review_type, "missing_checkout")
        self.assertTrue(_session_counts_as_work(session))

    def test_attendance_policy_payroll_rules(self):
        shift = self._shift("Day", "day", "09:00", "13:00", break_minutes=30)
        assignment = self._assignment(shift)
        completed = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=self.work_date,
            check_in_at=self._dt("09:00"),
            check_out_at=self._dt("13:00"),
            status="completed",
            worked_minutes=210,
            overtime_minutes=45,
            review_status="none",
        )
        absent = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_id=shift.id,
            work_date=self.work_date,
            status="absent",
            review_type="absent",
            review_status="approved",
        )

        self.assertTrue(session_counts_as_work(completed))
        self.assertEqual(recorded_overtime_minutes(completed), 45)
        self.assertFalse(session_counts_as_work(absent))
        self.assertTrue(confirmed_absent(absent))

        assignment.status = "leave_approved"
        leave_credit, leave_minutes = leave_credit_for_assignment(assignment, shift)
        self.assertEqual(leave_credit, 1)
        self.assertEqual(leave_minutes, 210)

    def test_manual_correction_requires_reason(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        self._assignment(shift)
        with self.assertRaises(ValueError):
            create_manual_attendance_log(
                self.employee.emp_code,
                "check_in",
                self._dt("09:00").strftime("%Y-%m-%dT%H:%M:%S"),
                created_by="admin@example.com",
            )

    def test_period_lock_blocks_manual_correction_and_assignment(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        self.db.add(AttendancePeriodLock(
            branch_id=self.branch.id,
            from_date=self.work_date,
            to_date=self.work_date,
            locked_by="admin@example.com",
            is_active=True,
        ))
        self.db.commit()

        with self.assertRaises(ValueError):
            create_manual_attendance_log(
                self.employee.emp_code,
                "check_in",
                self._dt("09:00").strftime("%Y-%m-%dT%H:%M:%S"),
                created_by="admin@example.com",
                reason="Bổ sung công sau chốt",
            )
        with self.assertRaises(ValueError):
            assign_shift(self.employee.emp_code, shift.id, self.work_date, db=self.db)


if __name__ == "__main__":
    unittest.main()
