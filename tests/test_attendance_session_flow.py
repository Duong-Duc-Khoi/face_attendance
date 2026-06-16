import asyncio
import unittest
from io import BytesIO
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import database
from app.models import Base
from app.models.attendance import AttendanceCorrectionAudit, AttendanceEvent, AttendanceLog, AttendancePeriodLock, AttendanceSession
from app.models.branch import Branch
from app.models.employee import Employee
from app.models.shift import Shift, ShiftAssignment
from app.services.attendance import (
    create_absent_session_manual_logs,
    create_manual_attendance_log,
    delete_attendance_log,
    get_logs_by_date,
    process_attendance,
    update_attendance_log,
)
from app.services.shift_service import (
    assign_shift,
    delete_assignment,
    delete_shift,
    find_open_session_for_time,
    find_shift_assignment_for_checkin,
    update_assignment,
)
from app.api.v1.reports import (
    AttendanceSessionReviewRequest,
    _session_counts_as_work,
    review_attendance_session,
)
from app.api.v1.shifts import (
    BulkAssignRequest,
    api_bulk_assign,
    api_import_assignments,
)
from app.api.v1.leave import approve_leave, submit_leave
from app.models.leave import LeaveRequest
from app.api.v1.employees import TransferBranchRequest, transfer_employee_branch
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

    def _admin_user(self):
        return SimpleNamespace(id=1, role="admin", full_name="Admin", email="admin@example.com")

    def _employee_with_role(self, emp_code: str, name: str, job_role: str) -> Employee:
        emp = Employee(
            emp_code=emp_code,
            name=name,
            branch_id=self.branch.id,
            job_role=job_role,
            is_active=True,
        )
        self.db.add(emp)
        self.db.commit()
        return emp

    def _xlsx_upload(self, rows: list[tuple[str, str]]) -> UploadFile:
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl is not installed")
        wb = Workbook()
        ws = wb.active
        ws.title = "Lich phan ca"
        ws.append(["Ngày *", "Nhân viên *", "Ca làm *", "Ghi chú"])
        for emp_code, shift_code in rows:
            ws.append([self.work_date.isoformat(), emp_code, shift_code, ""])
        stream = BytesIO()
        wb.save(stream)
        stream.seek(0)
        return UploadFile(filename="lich_phan_ca.xlsx", file=stream)

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

    def test_unscheduled_checkin_creates_pending_session(self):
        result = process_attendance(self.employee.emp_code, 0.93, "", self.branch.id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["check_type"], "check_in")
        self.assertEqual(result["reason"], "no_active_shift_assignment_logged")
        session = (
            self.db.query(AttendanceSession)
            .filter_by(employee_id=self.employee.id, review_type="unscheduled")
            .first()
        )
        self.assertIsNotNone(session)
        self.assertEqual(session.status, "open")
        self.assertEqual(session.check_in_status, "unscheduled")
        self.assertEqual(session.review_status, "pending_review")

    def test_unscheduled_approval_attaches_existing_shift_assignment(self):
        target_shift = self._shift("Afternoon", "afternoon", "15:00", "19:00")
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_id=None,
            work_date=self.work_date,
            check_in_at=self._dt("15:00"),
            check_out_at=self._dt("18:30"),
            status="completed",
            check_in_status="unscheduled",
            check_out_status="unscheduled",
            review_status="pending_review",
            review_type="unscheduled",
            worked_minutes=210,
        )
        self.db.add(session)
        self.db.commit()
        user = self._admin_user()

        with self.assertRaises(HTTPException) as ctx:
            review_attendance_session(
                session.id,
                AttendanceSessionReviewRequest(review_status="approved", note="OK", attach_unscheduled_shift=True),
                self.db,
                user,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("chọn một ca", str(ctx.exception.detail))

        result = review_attendance_session(
            session.id,
            AttendanceSessionReviewRequest(
                review_status="approved",
                note="OK",
                attach_unscheduled_shift=True,
                shift_id=target_shift.id,
            ),
            self.db,
            user,
        )
        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        assignment = self.db.query(ShiftAssignment).filter_by(id=result["assignment_id"]).first()
        shift = self.db.query(Shift).filter_by(id=fresh.shift_id).first()

        self.assertEqual(fresh.review_status, "approved")
        self.assertEqual(fresh.shift_assignment_id, assignment.id)
        self.assertEqual(fresh.shift_id, target_shift.id)
        self.assertEqual(assignment.status, "unscheduled_approved")
        self.assertEqual(assignment.emp_code, self.employee.emp_code)
        self.assertEqual(shift.id, target_shift.id)
        self.assertEqual(shift.work_start, "15:00")
        self.assertEqual(shift.work_end, "19:00")
        self.assertEqual(self.db.query(Shift).count(), 1)
        with self.assertRaises(ValueError):
            delete_assignment(assignment.id, self.db)
        with self.assertRaises(ValueError):
            delete_shift(shift.id, self.db)

    def test_unscheduled_review_items_include_existing_shift_candidates(self):
        shift = self._shift("Afternoon", "afternoon", "15:00", "19:00")
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_id=None,
            work_date=self.work_date,
            check_in_at=self._dt("15:00"),
            check_out_at=self._dt("18:30"),
            status="completed",
            check_in_status="unscheduled",
            check_out_status="unscheduled",
            review_status="pending_review",
            review_type="unscheduled",
            worked_minutes=210,
        )
        self.db.add(session)
        self.db.commit()

        result = review_attendance_session(
            session.id,
            AttendanceSessionReviewRequest(
                review_status="pending_review",
                note="",
            ),
            self.db,
            self._admin_user(),
        )

        candidates = result["session"].get("shift_candidates") or []
        self.assertEqual(candidates[0]["id"], shift.id)
        self.assertTrue(candidates[0]["in_attendance_window"])

    def test_manual_checkout_pairs_open_unscheduled_session(self):
        shift = self._shift("Afternoon", "afternoon", "15:00", "19:00")
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            work_date=self.work_date,
            check_in_at=self._dt("15:00"),
            status="open",
            check_in_status="unscheduled",
            review_status="pending_review",
            review_type="unscheduled",
        )
        self.db.add(session)
        self.db.commit()

        checkout = create_manual_attendance_log(
            self.employee.emp_code,
            "check_out",
            self._dt("18:30").strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Bổ sung checkout ngoài ca",
        )

        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        event = self.db.query(AttendanceEvent).filter_by(session_id=session.id, event_type="check_out").first()
        result = review_attendance_session(
            session.id,
            AttendanceSessionReviewRequest(review_status="pending_review", note=""),
            self.db,
            self._admin_user(),
        )

        self.assertEqual(fresh.status, "completed")
        self.assertEqual(fresh.check_out_at, self._dt("18:30"))
        self.assertEqual(fresh.check_out_status, "unscheduled")
        self.assertEqual(fresh.review_status, "pending_review")
        self.assertEqual(fresh.worked_minutes, 210)
        self.assertIsNotNone(event)
        self.assertEqual(result["session"]["shift_candidates"][0]["id"], shift.id)
        self.assertIsNotNone(checkout)

    def test_manual_checkout_does_not_pair_before_checkin_or_after_24h(self):
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            work_date=self.work_date,
            check_in_at=self._dt("15:00"),
            status="open",
            check_in_status="unscheduled",
            review_status="pending_review",
            review_type="unscheduled",
        )
        self.db.add(session)
        self.db.commit()

        create_manual_attendance_log(
            self.employee.emp_code,
            "check_out",
            self._dt("14:00").strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Checkout trước giờ vào",
        )
        create_manual_attendance_log(
            self.employee.emp_code,
            "check_out",
            (self._dt("15:00") + timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Checkout quá xa giờ vào",
        )

        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        self.assertEqual(fresh.status, "open")
        self.assertIsNone(fresh.check_out_at)
        self.assertEqual(fresh.worked_minutes, 0)

    def test_manual_unscheduled_checkout_update_and_delete_resync_session(self):
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            work_date=self.work_date,
            check_in_at=self._dt("15:00"),
            status="open",
            check_in_status="unscheduled",
            review_status="pending_review",
            review_type="unscheduled",
        )
        self.db.add(session)
        self.db.commit()
        checkout = create_manual_attendance_log(
            self.employee.emp_code,
            "check_out",
            self._dt("18:30").strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Bổ sung checkout ngoài ca",
        )

        update_attendance_log(
            checkout["id"],
            timestamp_str=self._dt("18:00").strftime("%Y-%m-%dT%H:%M:%S"),
            updated_by="admin@example.com",
            reason="Sửa checkout ngoài ca",
        )
        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        self.assertEqual(fresh.check_out_at, self._dt("18:00"))
        self.assertEqual(fresh.worked_minutes, 180)

        update_attendance_log(
            checkout["id"],
            check_type="check_in",
            updated_by="admin@example.com",
            reason="Đổi nhầm loại log",
        )
        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        self.assertEqual(fresh.status, "open")
        self.assertIsNone(fresh.check_out_at)
        self.assertEqual(fresh.worked_minutes, 0)
        linked_event = self.db.query(AttendanceEvent).filter_by(session_id=session.id, event_type="check_in").first()
        self.assertIsNone(linked_event)

        update_attendance_log(
            checkout["id"],
            check_type="check_out",
            timestamp_str=self._dt("18:15").strftime("%Y-%m-%dT%H:%M:%S"),
            updated_by="admin@example.com",
            reason="Đổi lại checkout",
        )
        delete_attendance_log(checkout["id"], deleted_by="admin@example.com", reason="Xóa checkout ngoài ca")
        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        self.assertEqual(fresh.status, "open")
        self.assertIsNone(fresh.check_out_at)
        self.assertEqual(fresh.worked_minutes, 0)

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

    def test_assignment_with_attendance_cannot_be_deleted_or_rescheduled(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        next_shift = self._shift("Evening", "evening", "16:00", "20:00")
        assignment = self._assignment(shift)
        self.db.add(AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=self.work_date,
            check_in_at=self._dt("09:00"),
            status="open",
        ))
        self.db.commit()

        with self.assertRaises(ValueError) as delete_ctx:
            delete_assignment(assignment.id, self.db)
        self.assertIn("đã có chấm công", str(delete_ctx.exception).lower())

        with self.assertRaises(ValueError) as update_ctx:
            update_assignment(assignment.id, {"shift_id": next_shift.id}, db=self.db)
        self.assertIn("đã có chấm công", str(update_ctx.exception).lower())

        result = update_assignment(assignment.id, {"note": "Đổi ghi chú"}, assigned_by="admin@example.com", db=self.db)
        self.db.refresh(assignment)
        self.assertEqual(assignment.note, "Đổi ghi chú")
        self.assertTrue(result["attendance_locked"])
        self.assertEqual(assignment.shift_id, shift.id)
        self.assertEqual(assignment.status, "scheduled")

    def test_assignment_with_only_attendance_log_is_locked(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)
        self.db.add(AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            check_type="check_in",
            timestamp=self._dt("09:00"),
        ))
        self.db.commit()

        rows = self.db.query(ShiftAssignment).filter_by(id=assignment.id).all()
        result = assign_shift(self.employee.emp_code, shift.id, self.work_date, note="Refresh", db=self.db)
        self.assertEqual(len(rows), 1)
        self.assertTrue(result["attendance_locked"])
        with self.assertRaises(ValueError):
            delete_assignment(assignment.id, self.db)

    def test_shift_in_use_cannot_be_deactivated(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        self._assignment(shift)

        with self.assertRaises(ValueError):
            delete_shift(shift.id, self.db)

        self.db.query(ShiftAssignment).filter_by(shift_id=shift.id).delete()
        self.db.commit()
        self.assertTrue(delete_shift(shift.id, self.db))
        self.db.refresh(shift)
        self.assertFalse(shift.is_active)

    def test_checkout_only_near_shift_end_creates_missing_checkin_session(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)

        checkout = create_manual_attendance_log(
            self.employee.emp_code,
            "check_out",
            self._dt("13:00").strftime("%Y-%m-%dT%H:%M:%S"),
            created_by="admin@example.com",
            reason="Nhân viên chỉ chấm cuối ca",
        )

        self.db.expire_all()
        session = self.db.query(AttendanceSession).filter_by(shift_assignment_id=assignment.id).first()
        self.assertIsNotNone(session)
        self.assertIsNone(session.check_in_at)
        self.assertEqual(session.check_out_at, self._dt("13:00"))
        self.assertEqual(session.status, "missing_checkin")
        self.assertEqual(session.review_type, "missing_checkin")
        self.assertEqual(session.review_status, "pending_review")
        self.assertEqual(session.worked_minutes, 0)
        self.assertIsNotNone(checkout)

        delete_attendance_log(checkout["id"], deleted_by="admin@example.com", reason="Xóa checkout cuối ca sai")
        self.db.expire_all()
        session = self.db.query(AttendanceSession).filter_by(shift_assignment_id=assignment.id).first()
        self.assertIsNone(session.check_out_at)
        self.assertEqual(session.status, "open")
        self.assertEqual(session.review_type, "")
        self.assertEqual(session.review_status, "none")

    def test_missing_checkin_review_can_edit_only_checkin(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=self.work_date,
            check_out_at=self._dt("13:00"),
            status="missing_checkin",
            check_out_status="normal",
            review_type="missing_checkin",
            review_status="pending_review",
            worked_minutes=0,
        )
        checkout_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            check_type="check_out",
            timestamp=self._dt("13:00"),
            note="Chấm ra cuối ca - thiếu check-in",
        )
        self.db.add_all([session, checkout_log])
        self.db.commit()

        review_attendance_session(
            session.id,
            AttendanceSessionReviewRequest(
                review_status="approved",
                note="Xác nhận giờ vào thực tế",
                check_in_at=self._dt("09:05").strftime("%Y-%m-%dT%H:%M:%S"),
            ),
            self.db,
            self._admin_user(),
        )

        self.db.refresh(session)
        checkin_log = self.db.query(AttendanceLog).filter_by(emp_code=self.employee.emp_code, check_type="check_in").first()
        self.assertEqual(session.check_in_at, self._dt("09:05"))
        self.assertEqual(session.check_out_at, self._dt("13:00"))
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.check_in_status, "manual")
        self.assertEqual(session.worked_minutes, 235)
        self.assertIsNotNone(checkin_log)
        self.assertEqual(checkin_log.timestamp, self._dt("09:05"))
        self.assertTrue(_session_counts_as_work(session))

        delete_attendance_log(checkin_log.id, deleted_by="admin@example.com", reason="Xóa giờ vào bổ sung sai")
        self.db.expire_all()
        session = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        self.assertIsNone(session.check_in_at)
        self.assertEqual(session.check_out_at, self._dt("13:00"))
        self.assertEqual(session.status, "missing_checkin")
        self.assertEqual(session.review_status, "pending_review")
        self.assertFalse(_session_counts_as_work(session))

    def test_absent_review_can_create_checkin_checkout_logs(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)
        session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=self.work_date,
            status="absent",
            review_type="absent",
            review_status="pending_review",
            note="Vắng ca Day - chờ quản lý xác nhận",
        )
        self.db.add(session)
        self.db.commit()

        result = create_absent_session_manual_logs(
            session.id,
            self._dt("09:00").strftime("%Y-%m-%dT%H:%M:%S"),
            self._dt("13:00").strftime("%Y-%m-%dT%H:%M:%S"),
            note="Nhân viên có đi làm, bổ sung từ xác nhận quản lý",
            created_by="admin@example.com",
            reason="Xác nhận ca vắng bằng log bù",
        )

        self.db.expire_all()
        fresh = self.db.query(AttendanceSession).filter_by(id=session.id).first()
        logs = self.db.query(AttendanceLog).filter_by(emp_code=self.employee.emp_code).all()
        self.assertEqual(len(logs), 2)
        self.assertEqual(result["session_status"], "completed")
        self.assertEqual(fresh.status, "completed")
        self.assertEqual(fresh.review_type, "")
        self.assertEqual(fresh.review_status, "none")
        self.assertEqual(fresh.check_in_at, self._dt("09:00"))
        self.assertEqual(fresh.check_out_at, self._dt("13:00"))
        self.assertTrue(session_counts_as_work(fresh))

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

    def test_missing_checkout_review_can_edit_only_checkout(self):
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
            check_out_status="auto",
            review_type="missing_checkout",
            review_status="pending_review",
            worked_minutes=240,
        )
        checkout_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            check_type="check_out",
            timestamp=self._dt("13:00"),
            note="Tự động chấm ra theo giờ kết thúc ca - nhân viên quên check out",
        )
        self.db.add_all([session, checkout_log])
        self.db.commit()

        review_attendance_session(
            session.id,
            AttendanceSessionReviewRequest(
                review_status="approved",
                note="Xác nhận giờ ra thực tế",
                check_out_at=self._dt("12:45").strftime("%Y-%m-%dT%H:%M:%S"),
            ),
            self.db,
            self._admin_user(),
        )
        self.db.refresh(session)
        self.db.refresh(checkout_log)

        self.assertEqual(session.check_in_at, self._dt("09:00"))
        self.assertEqual(session.check_out_at, self._dt("12:45"))
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.check_out_status, "manual")
        self.assertEqual(session.worked_minutes, 225)
        self.assertEqual(checkout_log.timestamp, self._dt("12:45"))

    def test_missing_checkout_rejected_marks_checkin_and_checkout_not_counted(self):
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
            check_out_status="auto",
            review_type="missing_checkout",
            review_status="pending_review",
            worked_minutes=240,
        )
        checkin_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            check_type="check_in",
            timestamp=self._dt("09:00"),
        )
        checkout_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            check_type="check_out",
            timestamp=self._dt("13:00"),
            note="Tự động chấm ra theo giờ kết thúc ca - nhân viên quên check out",
        )
        self.db.add_all([session, checkin_log, checkout_log])
        self.db.commit()

        review_attendance_session(
            session.id,
            AttendanceSessionReviewRequest(review_status="rejected", note="Không xác nhận ca này"),
            self.db,
            self._admin_user(),
        )
        self.db.refresh(session)
        logs = {row["check_type"]: row for row in get_logs_by_date(self.work_date.isoformat(), self.employee.emp_code, None)}

        self.assertEqual(session.status, "missing_checkout")
        self.assertEqual(session.review_status, "rejected")
        self.assertFalse(_session_counts_as_work(session))
        self.assertEqual(logs["check_in"]["session_payroll_status"], "not_counted")
        self.assertEqual(logs["check_out"]["session_payroll_status"], "not_counted")
        self.assertEqual(logs["check_in"]["session_status_text"], "Không tính công")
        self.assertEqual(logs["check_out"]["session_status_text"], "Không tính công")

    def test_attendance_logs_include_session_payroll_status(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        assignment = self._assignment(shift)
        open_shift = self._shift("Evening", "evening", "16:00", "20:00")
        open_assignment = ShiftAssignment(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            emp_code=self.employee.emp_code,
            shift_id=open_shift.id,
            work_date=self.work_date,
            status="scheduled",
        )
        self.db.add(open_assignment)
        self.db.flush()
        counted_session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=assignment.id,
            shift_id=shift.id,
            work_date=self.work_date,
            check_in_at=self._dt("09:00"),
            check_out_at=self._dt("13:00"),
            status="completed",
            review_status="none",
            worked_minutes=240,
        )
        pending_session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_id=None,
            work_date=self.work_date,
            check_in_at=self._dt("15:00"),
            status="open",
            review_status="pending_review",
            review_type="unscheduled",
        )
        open_session = AttendanceSession(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            shift_assignment_id=open_assignment.id,
            shift_id=open_shift.id,
            work_date=self.work_date,
            check_in_at=self._dt("16:00"),
            status="open",
            review_status="none",
        )
        self.db.add_all([counted_session, pending_session, open_session])
        self.db.flush()
        counted_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            department="",
            check_type="check_in",
            timestamp=self._dt("09:00"),
            confidence=0.9,
        )
        pending_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            department="",
            check_type="check_in",
            timestamp=self._dt("15:00"),
            confidence=0.9,
        )
        open_log = AttendanceLog(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            department="",
            check_type="check_in",
            timestamp=self._dt("16:00"),
            confidence=0.9,
        )
        self.db.add_all([counted_log, pending_log, open_log])
        self.db.flush()
        self.db.add_all([
            AttendanceEvent(
                session_id=counted_session.id,
                employee_id=self.employee.id,
                branch_id=self.branch.id,
                event_type="check_in",
                event_time=self._dt("09:00"),
                confidence=0.9,
            ),
            AttendanceEvent(
                session_id=pending_session.id,
                employee_id=self.employee.id,
                branch_id=self.branch.id,
                event_type="check_in",
                event_time=self._dt("15:00"),
                confidence=0.9,
            ),
            AttendanceEvent(
                session_id=open_session.id,
                employee_id=self.employee.id,
                branch_id=self.branch.id,
                event_type="check_in",
                event_time=self._dt("16:00"),
                confidence=0.9,
            ),
        ])
        self.db.commit()

        rows = get_logs_by_date(self.work_date.isoformat(), self.employee.emp_code)
        by_id = {row["id"]: row for row in rows}

        self.assertEqual(by_id[counted_log.id]["session_payroll_status"], "counted")
        self.assertEqual(by_id[counted_log.id]["session_status_text"], "Đã tính công")
        self.assertEqual(by_id[pending_log.id]["session_payroll_status"], "pending_review")
        self.assertEqual(by_id[pending_log.id]["session_status_text"], "Chờ duyệt")
        self.assertEqual(by_id[open_log.id]["session_payroll_status"], "open")
        self.assertEqual(by_id[open_log.id]["session_status_text"], "Chưa đủ check-out")

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
        completed.status = "open"
        self.assertFalse(session_counts_as_work(completed))
        completed.status = "completed"
        self.assertFalse(session_counts_as_work(absent))
        self.assertTrue(confirmed_absent(absent))

        assignment.status = "leave_approved"
        leave_credit, leave_minutes = leave_credit_for_assignment(assignment, shift)
        self.assertEqual(leave_credit, 1)
        self.assertEqual(leave_minutes, 210)

    def test_leave_request_creation_only_accepts_shift_scope(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        work_date = date.today() + timedelta(days=1)
        assignment = ShiftAssignment(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            emp_code=self.employee.emp_code,
            shift_id=shift.id,
            work_date=work_date,
            status="scheduled",
        )
        self.db.add(assignment)
        self.db.commit()

        admin = self._admin_user()
        day_payload = {
            "request_type": "leave",
            "emp_code": self.employee.emp_code,
            "leave_scope": "day",
            "dates": [{"date": work_date.isoformat(), "half": None}],
            "reason": "Xin nghỉ cả ngày",
        }
        with self.assertRaises(HTTPException) as ctx:
            submit_leave(day_payload, db=self.db, current_user=admin)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("nghỉ theo ca", str(ctx.exception.detail))

        shift_payload = {
            "request_type": "leave",
            "emp_code": self.employee.emp_code,
            "leave_scope": "shift",
            "dates": [{
                "date": work_date.isoformat(),
                "scope": "shift",
                "assignment_ids": [assignment.id],
            }],
            "reason": "Xin nghỉ ca",
        }
        result = submit_leave(shift_payload, db=self.db, current_user=admin)

        self.assertTrue(result["success"])
        self.assertEqual(result["request"]["leave_scope"], "shift")
        self.assertEqual(result["request"]["dates"][0]["assignment_ids"], [assignment.id])
        self.db.refresh(assignment)
        self.assertEqual(assignment.status, "leave_approved")

    def test_approve_shift_leave_updates_assignment(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        work_date = date.today() + timedelta(days=1)
        assignment = ShiftAssignment(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            emp_code=self.employee.emp_code,
            shift_id=shift.id,
            work_date=work_date,
            status="scheduled",
        )
        self.db.add(assignment)
        self.db.flush()
        req = LeaveRequest(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            department="",
            emp_email="staff@example.com",
            request_type="leave",
            reason="Xin nghỉ ca",
            status="pending",
            submitted_at=datetime.now(),
        )
        req.set_dates([{
            "date": work_date.isoformat(),
            "half": None,
            "scope": "shift",
            "assignment_ids": [assignment.id],
        }])
        self.db.add(req)
        self.db.commit()

        result = approve_leave(req.id, {"note": "OK"}, db=self.db, current_user=self._admin_user())

        self.assertTrue(result["success"])
        self.assertEqual(result["request"]["status"], "approved")
        self.db.refresh(assignment)
        self.assertEqual(assignment.status, "leave_approved")

    def test_approve_shift_leave_locked_period_returns_400(self):
        shift = self._shift("Day", "day", "09:00", "13:00")
        work_date = date.today() + timedelta(days=1)
        assignment = ShiftAssignment(
            employee_id=self.employee.id,
            branch_id=self.branch.id,
            emp_code=self.employee.emp_code,
            shift_id=shift.id,
            work_date=work_date,
            status="scheduled",
        )
        self.db.add(assignment)
        self.db.flush()
        req = LeaveRequest(
            employee_id=self.employee.id,
            emp_code=self.employee.emp_code,
            emp_name=self.employee.name,
            department="",
            emp_email="staff@example.com",
            request_type="leave",
            reason="Xin nghỉ ca",
            status="pending",
            submitted_at=datetime.now(),
        )
        req.set_dates([{
            "date": work_date.isoformat(),
            "half": None,
            "scope": "shift",
            "assignment_ids": [assignment.id],
        }])
        self.db.add_all([req, AttendancePeriodLock(
            branch_id=self.branch.id,
            from_date=work_date,
            to_date=work_date,
            locked_by="admin@example.com",
            is_active=True,
        )])
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            approve_leave(req.id, {"note": "OK"}, db=self.db, current_user=self._admin_user())

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Kỳ công đã chốt", str(ctx.exception.detail))
        fresh_assignment = self.db.query(ShiftAssignment).filter_by(id=assignment.id).first()
        fresh_req = self.db.query(LeaveRequest).filter_by(id=req.id).first()
        self.assertEqual(fresh_assignment.status, "scheduled")
        self.assertEqual(fresh_req.status, "pending")

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

    def test_bulk_assign_partial_success_keeps_valid_rows(self):
        self.employee.job_role = "cashier"
        self.db.add(self.employee)
        self.db.commit()
        bad_employee = self._employee_with_role("NV002", "Nhan Vien Sai Vai Tro", "barista")
        shift = self._shift("Cashier", "cashier", "09:00", "13:00")
        shift.required_position = "cashier"
        self.db.add(shift)
        self.db.commit()

        result = api_bulk_assign(
            BulkAssignRequest(
                emp_codes=[self.employee.emp_code, bad_employee.emp_code],
                shift_id=shift.id,
                from_date=self.work_date.isoformat(),
                to_date=self.work_date.isoformat(),
            ),
            db=self.db,
            current_user=self._admin_user(),
        )

        self.assertEqual(result["assigned"], 1)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["errors"][0]["emp_code"], bad_employee.emp_code)
        rows = self.db.query(ShiftAssignment).filter_by(shift_id=shift.id, work_date=self.work_date).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].emp_code, self.employee.emp_code)

    def test_import_assignments_partial_success_keeps_valid_rows(self):
        self.employee.job_role = "cashier"
        self.db.add(self.employee)
        self.db.commit()
        bad_employee = self._employee_with_role("NV002", "Nhan Vien Sai Vai Tro", "barista")
        shift = self._shift("Cashier", "cashier", "09:00", "13:00")
        shift.required_position = "cashier"
        self.db.add(shift)
        self.db.commit()

        upload = self._xlsx_upload([
            (self.employee.emp_code, shift.code),
            (bad_employee.emp_code, shift.code),
        ])
        result = asyncio.run(api_import_assignments(
            branch_id=self.branch.id,
            file=upload,
            db=self.db,
            current_user=self._admin_user(),
        ))

        self.assertTrue(result["success"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertIn("Dòng 3", str(result["errors"][0]))
        rows = self.db.query(ShiftAssignment).filter_by(shift_id=shift.id, work_date=self.work_date).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].emp_code, self.employee.emp_code)

    def test_import_assignments_all_row_errors_returns_2xx_body_without_saving(self):
        bad_employee = self._employee_with_role("NV002", "Nhan Vien Sai Vai Tro", "barista")
        shift = self._shift("Cashier", "cashier", "09:00", "13:00")
        shift.required_position = "cashier"
        self.db.add(shift)
        self.db.commit()

        upload = self._xlsx_upload([(bad_employee.emp_code, shift.code)])
        result = asyncio.run(api_import_assignments(
            branch_id=self.branch.id,
            file=upload,
            db=self.db,
            current_user=self._admin_user(),
        ))

        self.assertFalse(result["success"])
        self.assertFalse(result["partial_success"])
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertIn("Dòng 2", str(result["errors"][0]))
        self.assertEqual(self.db.query(ShiftAssignment).filter_by(shift_id=shift.id).count(), 0)

    def test_inactive_employee_cannot_transfer_branch(self):
        target_branch = Branch(name="Store 2", is_active=True)
        self.db.add(target_branch)
        self.employee.is_active = False
        self.employee.status = "inactive"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            transfer_employee_branch(
                self.employee.id,
                TransferBranchRequest(branch_id=target_branch.id),
                db=self.db,
                current_user=self._admin_user(),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("đã nghỉ", str(ctx.exception.detail))
        self.db.refresh(self.employee)
        self.assertEqual(self.employee.branch_id, self.branch.id)


if __name__ == "__main__":
    unittest.main()
