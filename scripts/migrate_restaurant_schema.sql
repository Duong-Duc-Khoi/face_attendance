-- Restaurant attendance schema migration for PostgreSQL.
-- Safe to run more than once. Review against a database backup first.

BEGIN;

CREATE TABLE IF NOT EXISTS branches (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL UNIQUE,
    address VARCHAR(255) DEFAULT '',
    phone VARCHAR(30) DEFAULT '',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

ALTER TABLE employees ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS branch_id INTEGER;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS full_name VARCHAR(100) DEFAULT '';
ALTER TABLE employees ADD COLUMN IF NOT EXISTS hire_date DATE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_employees_user_id ON employees(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_employees_branch_id ON employees(branch_id);
CREATE INDEX IF NOT EXISTS ix_employees_status ON employees(status);

ALTER TABLE shifts DROP CONSTRAINT IF EXISTS shifts_code_key;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS branch_id INTEGER;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS early_checkin_minutes INTEGER DEFAULT 30;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS auto_checkout_minutes INTEGER DEFAULT 180;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS break_minutes INTEGER DEFAULT 0;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS is_overnight BOOLEAN DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS ux_shifts_branch_code ON shifts(COALESCE(branch_id, 0), code);
CREATE INDEX IF NOT EXISTS ix_shifts_branch_id ON shifts(branch_id);

ALTER TABLE shift_assignments DROP CONSTRAINT IF EXISTS uq_emp_date;
ALTER TABLE shift_assignments ADD COLUMN IF NOT EXISTS employee_id INTEGER;
ALTER TABLE shift_assignments ADD COLUMN IF NOT EXISTS branch_id INTEGER;
ALTER TABLE shift_assignments ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'scheduled';
ALTER TABLE shift_assignments ADD COLUMN IF NOT EXISTS assigned_by_id INTEGER;
ALTER TABLE shift_assignments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS ux_shift_assignments_emp_date_shift
    ON shift_assignments(emp_code, work_date, shift_id);
CREATE INDEX IF NOT EXISTS ix_shift_assignments_employee_id ON shift_assignments(employee_id);
CREATE INDEX IF NOT EXISTS ix_shift_assignments_branch_id ON shift_assignments(branch_id);
CREATE INDEX IF NOT EXISTS ix_shift_assignments_status ON shift_assignments(status);

ALTER TABLE work_calendar DROP CONSTRAINT IF EXISTS work_calendar_date_key;
ALTER TABLE work_calendar ADD COLUMN IF NOT EXISTS branch_id INTEGER;
ALTER TABLE work_calendar ADD COLUMN IF NOT EXISTS created_by_id INTEGER;
ALTER TABLE work_calendar ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS ux_calendar_branch_date ON work_calendar(COALESCE(branch_id, 0), date);
CREATE INDEX IF NOT EXISTS ix_work_calendar_branch_id ON work_calendar(branch_id);

CREATE TABLE IF NOT EXISTS attendance_sessions (
    id SERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL,
    branch_id INTEGER,
    shift_assignment_id INTEGER,
    shift_id INTEGER,
    work_date DATE NOT NULL,
    check_in_at TIMESTAMP,
    check_out_at TIMESTAMP,
    status VARCHAR(30) DEFAULT 'open',
    check_in_status VARCHAR(30) DEFAULT '',
    check_out_status VARCHAR(30) DEFAULT '',
    late_minutes INTEGER DEFAULT 0,
    early_leave_minutes INTEGER DEFAULT 0,
    overtime_minutes INTEGER DEFAULT 0,
    worked_minutes INTEGER DEFAULT 0,
    break_minutes INTEGER DEFAULT 0,
    source VARCHAR(20) DEFAULT 'face',
    note TEXT DEFAULT '',
    created_by_id INTEGER,
    updated_by_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_attendance_session_assignment
    ON attendance_sessions(shift_assignment_id)
    WHERE shift_assignment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_attendance_sessions_employee_id ON attendance_sessions(employee_id);
CREATE INDEX IF NOT EXISTS ix_attendance_sessions_branch_id ON attendance_sessions(branch_id);
CREATE INDEX IF NOT EXISTS ix_attendance_sessions_shift_id ON attendance_sessions(shift_id);
CREATE INDEX IF NOT EXISTS ix_attendance_sessions_work_date ON attendance_sessions(work_date);
CREATE INDEX IF NOT EXISTS ix_attendance_sessions_status ON attendance_sessions(status);

CREATE TABLE IF NOT EXISTS attendance_events (
    id SERIAL PRIMARY KEY,
    session_id INTEGER,
    employee_id INTEGER NOT NULL,
    branch_id INTEGER,
    event_type VARCHAR(30) NOT NULL,
    event_time TIMESTAMP DEFAULT NOW(),
    confidence DOUBLE PRECISION DEFAULT 0.0,
    capture_path VARCHAR(255) DEFAULT '',
    face_bbox TEXT DEFAULT '',
    image_hash VARCHAR(64) DEFAULT '',
    device_id VARCHAR(64) DEFAULT '',
    source VARCHAR(20) DEFAULT 'face',
    created_by_id INTEGER,
    note TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_attendance_events_session_id ON attendance_events(session_id);
CREATE INDEX IF NOT EXISTS ix_attendance_events_employee_id ON attendance_events(employee_id);
CREATE INDEX IF NOT EXISTS ix_attendance_events_branch_id ON attendance_events(branch_id);
CREATE INDEX IF NOT EXISTS ix_attendance_events_event_type ON attendance_events(event_type);
CREATE INDEX IF NOT EXISTS ix_attendance_events_event_time ON attendance_events(event_time);

ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS employee_id INTEGER;
ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS reviewed_by_id INTEGER;
ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
CREATE INDEX IF NOT EXISTS ix_leave_requests_employee_id ON leave_requests(employee_id);
CREATE INDEX IF NOT EXISTS ix_leave_requests_reviewed_by_id ON leave_requests(reviewed_by_id);

CREATE TABLE IF NOT EXISTS leave_request_days (
    id SERIAL PRIMARY KEY,
    leave_request_id INTEGER NOT NULL,
    date DATE NOT NULL,
    half_day VARCHAR(10)
);
CREATE INDEX IF NOT EXISTS ix_leave_request_days_leave_request_id ON leave_request_days(leave_request_id);
CREATE INDEX IF NOT EXISTS ix_leave_request_days_date ON leave_request_days(date);

CREATE TABLE IF NOT EXISTS shift_plan_drafts (
    id SERIAL PRIMARY KEY,
    branch_id INTEGER,
    from_date DATE NOT NULL,
    to_date DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'draft',
    source VARCHAR(20) DEFAULT 'heuristic',
    prompt TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    warnings TEXT DEFAULT '[]',
    created_by VARCHAR(150) DEFAULT '',
    applied_by VARCHAR(150) DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    applied_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_shift_plan_drafts_branch_id ON shift_plan_drafts(branch_id);
CREATE INDEX IF NOT EXISTS ix_shift_plan_drafts_from_date ON shift_plan_drafts(from_date);
CREATE INDEX IF NOT EXISTS ix_shift_plan_drafts_to_date ON shift_plan_drafts(to_date);
CREATE INDEX IF NOT EXISTS ix_shift_plan_drafts_status ON shift_plan_drafts(status);

CREATE TABLE IF NOT EXISTS shift_plan_draft_assignments (
    id SERIAL PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    emp_code VARCHAR(20) NOT NULL,
    shift_id INTEGER NOT NULL,
    work_date DATE NOT NULL,
    reason TEXT DEFAULT '',
    validation_status VARCHAR(20) DEFAULT 'valid',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shift_plan_draft_item
    ON shift_plan_draft_assignments(draft_id, emp_code, work_date, shift_id);
CREATE INDEX IF NOT EXISTS ix_shift_plan_draft_assignments_draft_id ON shift_plan_draft_assignments(draft_id);
CREATE INDEX IF NOT EXISTS ix_shift_plan_draft_assignments_emp_code ON shift_plan_draft_assignments(emp_code);
CREATE INDEX IF NOT EXISTS ix_shift_plan_draft_assignments_shift_id ON shift_plan_draft_assignments(shift_id);
CREATE INDEX IF NOT EXISTS ix_shift_plan_draft_assignments_work_date ON shift_plan_draft_assignments(work_date);

CREATE TABLE IF NOT EXISTS ai_provider_settings (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(30) NOT NULL UNIQUE,
    api_key_encrypted TEXT DEFAULT '',
    model VARCHAR(100) DEFAULT '',
    is_enabled BOOLEAN DEFAULT FALSE,
    updated_by VARCHAR(150) DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_provider_settings_provider ON ai_provider_settings(provider);

UPDATE employees SET full_name = name WHERE COALESCE(full_name, '') = '';
UPDATE employees SET status = CASE WHEN is_active THEN 'active' ELSE 'inactive' END
WHERE status IS NULL OR status = '';
UPDATE shifts SET is_overnight = TRUE WHERE work_end <= work_start;

INSERT INTO shifts (name, code, work_start, work_end, late_threshold_minutes, early_checkin_minutes, auto_checkout_minutes, break_minutes, is_overnight, is_active, note, created_at, updated_at)
SELECT 'Ca sang', 'morning', '06:00', '11:00', 10, 30, 180, 0, FALSE, TRUE, '', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM shifts WHERE code = 'morning' AND branch_id IS NULL);

INSERT INTO shifts (name, code, work_start, work_end, late_threshold_minutes, early_checkin_minutes, auto_checkout_minutes, break_minutes, is_overnight, is_active, note, created_at, updated_at)
SELECT 'Ca trua', 'lunch', '10:00', '15:00', 10, 30, 180, 30, FALSE, TRUE, '', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM shifts WHERE code = 'lunch' AND branch_id IS NULL);

INSERT INTO shifts (name, code, work_start, work_end, late_threshold_minutes, early_checkin_minutes, auto_checkout_minutes, break_minutes, is_overnight, is_active, note, created_at, updated_at)
SELECT 'Ca toi', 'evening', '16:00', '22:00', 10, 30, 180, 30, FALSE, TRUE, '', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM shifts WHERE code = 'evening' AND branch_id IS NULL);

INSERT INTO shifts (name, code, work_start, work_end, late_threshold_minutes, early_checkin_minutes, auto_checkout_minutes, break_minutes, is_overnight, is_active, note, created_at, updated_at)
SELECT 'Ca dem', 'night', '22:00', '06:00', 10, 30, 180, 30, TRUE, TRUE, '', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM shifts WHERE code = 'night' AND branch_id IS NULL);

COMMIT;
