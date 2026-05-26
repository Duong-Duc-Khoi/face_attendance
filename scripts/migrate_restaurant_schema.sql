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
ALTER TABLE employees ADD COLUMN IF NOT EXISTS job_role VARCHAR(50) DEFAULT '';
ALTER TABLE employees ADD COLUMN IF NOT EXISTS employment_type VARCHAR(30) DEFAULT 'full_time';
ALTER TABLE employees ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC(12, 2);
ALTER TABLE employees ADD COLUMN IF NOT EXISTS base_salary NUMERIC(12, 2);
ALTER TABLE employees ADD COLUMN IF NOT EXISTS hire_date DATE;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_employees_user_id ON employees(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_employees_branch_id ON employees(branch_id);
CREATE INDEX IF NOT EXISTS ix_employees_job_role ON employees(job_role);
CREATE INDEX IF NOT EXISTS ix_employees_employment_type ON employees(employment_type);
CREATE INDEX IF NOT EXISTS ix_employees_status ON employees(status);

CREATE TABLE IF NOT EXISTS employee_roles (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(255) DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_employee_roles_code ON employee_roles(code);
CREATE INDEX IF NOT EXISTS ix_employee_roles_code ON employee_roles(code);
CREATE INDEX IF NOT EXISTS ix_employee_roles_sort_order ON employee_roles(sort_order);
CREATE INDEX IF NOT EXISTS ix_employee_roles_is_active ON employee_roles(is_active);

ALTER TABLE shifts DROP CONSTRAINT IF EXISTS shifts_code_key;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS branch_id INTEGER;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS early_checkin_minutes INTEGER DEFAULT 30;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS auto_checkout_minutes INTEGER DEFAULT 180;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS break_minutes INTEGER DEFAULT 0;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS is_overnight BOOLEAN DEFAULT FALSE;
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS required_position VARCHAR(100) DEFAULT '';
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

CREATE TABLE IF NOT EXISTS attendance_evidence (
    id SERIAL PRIMARY KEY,
    log_id INTEGER NOT NULL,
    event_id INTEGER,
    session_id INTEGER,
    employee_id INTEGER,
    emp_code VARCHAR(20) DEFAULT '',
    image_path VARCHAR(255) DEFAULT '',
    image_hash VARCHAR(64) DEFAULT '',
    captured_at TIMESTAMP DEFAULT NOW(),
    files_available BOOLEAN DEFAULT TRUE,
    deleted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_log_id ON attendance_evidence(log_id);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_event_id ON attendance_evidence(event_id);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_session_id ON attendance_evidence(session_id);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_employee_id ON attendance_evidence(employee_id);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_emp_code ON attendance_evidence(emp_code);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_captured_at ON attendance_evidence(captured_at);
CREATE INDEX IF NOT EXISTS ix_attendance_evidence_files_available ON attendance_evidence(files_available);

CREATE TABLE IF NOT EXISTS attendance_audit_runs (
    id SERIAL PRIMARY KEY,
    run_type VARCHAR(30) DEFAULT 'daily',
    from_date DATE,
    to_date DATE,
    emp_code VARCHAR(20) DEFAULT '',
    status VARCHAR(20) DEFAULT 'completed',
    source VARCHAR(30) DEFAULT 'heuristic',
    summary TEXT DEFAULT '',
    warnings TEXT DEFAULT '[]',
    total_findings INTEGER DEFAULT 0,
    high_count INTEGER DEFAULT 0,
    medium_count INTEGER DEFAULT 0,
    low_count INTEGER DEFAULT 0,
    created_by VARCHAR(150) DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_runs_run_type ON attendance_audit_runs(run_type);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_runs_from_date ON attendance_audit_runs(from_date);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_runs_to_date ON attendance_audit_runs(to_date);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_runs_emp_code ON attendance_audit_runs(emp_code);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_runs_status ON attendance_audit_runs(status);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_runs_created_at ON attendance_audit_runs(created_at);

CREATE TABLE IF NOT EXISTS attendance_audit_findings (
    id SERIAL PRIMARY KEY,
    audit_run_id INTEGER,
    log_id INTEGER NOT NULL,
    event_id INTEGER,
    evidence_id INTEGER,
    employee_id INTEGER,
    emp_code VARCHAR(20) DEFAULT '',
    emp_name VARCHAR(100) DEFAULT '',
    risk_score DOUBLE PRECISION DEFAULT 0.0,
    risk_level VARCHAR(20) DEFAULT 'low',
    reasons TEXT DEFAULT '[]',
    metrics TEXT DEFAULT '{}',
    source VARCHAR(30) DEFAULT 'heuristic',
    review_status VARCHAR(30) DEFAULT 'pending_review',
    reviewer_note TEXT DEFAULT '',
    reviewed_by VARCHAR(150) DEFAULT '',
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_audit_run_id ON attendance_audit_findings(audit_run_id);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_log_id ON attendance_audit_findings(log_id);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_event_id ON attendance_audit_findings(event_id);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_evidence_id ON attendance_audit_findings(evidence_id);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_employee_id ON attendance_audit_findings(employee_id);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_emp_code ON attendance_audit_findings(emp_code);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_risk_score ON attendance_audit_findings(risk_score);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_risk_level ON attendance_audit_findings(risk_level);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_source ON attendance_audit_findings(source);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_review_status ON attendance_audit_findings(review_status);
CREATE INDEX IF NOT EXISTS ix_attendance_audit_findings_created_at ON attendance_audit_findings(created_at);

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
UPDATE employees SET job_role = position WHERE COALESCE(job_role, '') = '' AND COALESCE(position, '') <> '';
UPDATE employees SET employment_type = 'full_time' WHERE employment_type IS NULL OR employment_type = '';
UPDATE employees SET status = CASE WHEN is_active THEN 'active' ELSE 'inactive' END
WHERE status IS NULL OR status = '';
UPDATE shifts SET is_overnight = TRUE WHERE work_end <= work_start;

INSERT INTO employee_roles (code, name, description, sort_order, is_active, created_at, updated_at)
SELECT 'server', 'Phục vụ', 'Nhân viên phục vụ bàn', 10, TRUE, NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM employee_roles WHERE code = 'server');
INSERT INTO employee_roles (code, name, description, sort_order, is_active, created_at, updated_at)
SELECT 'cashier', 'Thu ngân', 'Nhân viên thu ngân', 20, TRUE, NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM employee_roles WHERE code = 'cashier');
INSERT INTO employee_roles (code, name, description, sort_order, is_active, created_at, updated_at)
SELECT 'kitchen', 'Bếp', 'Bếp chính/phụ bếp', 30, TRUE, NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM employee_roles WHERE code = 'kitchen');
INSERT INTO employee_roles (code, name, description, sort_order, is_active, created_at, updated_at)
SELECT 'bar', 'Bar', 'Pha chế/quầy bar', 40, TRUE, NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM employee_roles WHERE code = 'bar');
INSERT INTO employee_roles (code, name, description, sort_order, is_active, created_at, updated_at)
SELECT 'cleaner', 'Tạp vụ', 'Vệ sinh, dọn dẹp', 50, TRUE, NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM employee_roles WHERE code = 'cleaner');
INSERT INTO employee_roles (code, name, description, sort_order, is_active, created_at, updated_at)
SELECT 'shift_lead', 'Quản lý ca', 'Điều phối vận hành trong ca', 60, TRUE, NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM employee_roles WHERE code = 'shift_lead');

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

-- Normalize relational integrity without forcing a full legacy-data cleanup during app startup.
-- NOT VALID means existing rows are not scanned here, but new/updated rows must satisfy
-- the constraints. After cleaning old data, run scripts/validate_restaurant_constraints.sql.
CREATE OR REPLACE FUNCTION pg_temp.restaurant_fk_exists(
    source_table REGCLASS,
    source_column TEXT,
    target_table REGCLASS,
    target_column TEXT
) RETURNS BOOLEAN
LANGUAGE SQL
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_attribute sa
          ON sa.attrelid = c.conrelid
         AND sa.attnum = ANY(c.conkey)
        JOIN pg_attribute ta
          ON ta.attrelid = c.confrelid
         AND ta.attnum = ANY(c.confkey)
        WHERE c.contype = 'f'
          AND c.conrelid = source_table
          AND c.confrelid = target_table
          AND sa.attname = source_column
          AND ta.attname = target_column
    );
$$;

DO $$
BEGIN
    IF NOT pg_temp.restaurant_fk_exists('employees', 'user_id', 'users', 'id') THEN
        ALTER TABLE employees
            ADD CONSTRAINT fk_employees_user_id
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('employees', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE employees
            ADD CONSTRAINT fk_employees_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('email_tokens', 'user_id', 'users', 'id') THEN
        ALTER TABLE email_tokens
            ADD CONSTRAINT fk_email_tokens_user_id
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('refresh_tokens', 'user_id', 'users', 'id') THEN
        ALTER TABLE refresh_tokens
            ADD CONSTRAINT fk_refresh_tokens_user_id
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('shifts', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE shifts
            ADD CONSTRAINT fk_shifts_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('shift_assignments', 'employee_id', 'employees', 'id') THEN
        ALTER TABLE shift_assignments
            ADD CONSTRAINT fk_shift_assignments_employee_id
            FOREIGN KEY (employee_id) REFERENCES employees(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('shift_assignments', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE shift_assignments
            ADD CONSTRAINT fk_shift_assignments_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('shift_assignments', 'shift_id', 'shifts', 'id') THEN
        ALTER TABLE shift_assignments
            ADD CONSTRAINT fk_shift_assignments_shift_id
            FOREIGN KEY (shift_id) REFERENCES shifts(id)
            ON DELETE RESTRICT NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('shift_assignments', 'assigned_by_id', 'users', 'id') THEN
        ALTER TABLE shift_assignments
            ADD CONSTRAINT fk_shift_assignments_assigned_by_id
            FOREIGN KEY (assigned_by_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('work_calendar', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE work_calendar
            ADD CONSTRAINT fk_work_calendar_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('work_calendar', 'created_by_id', 'users', 'id') THEN
        ALTER TABLE work_calendar
            ADD CONSTRAINT fk_work_calendar_created_by_id
            FOREIGN KEY (created_by_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('attendance_sessions', 'employee_id', 'employees', 'id') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT fk_attendance_sessions_employee_id
            FOREIGN KEY (employee_id) REFERENCES employees(id)
            ON DELETE RESTRICT NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_sessions', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT fk_attendance_sessions_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_sessions', 'shift_assignment_id', 'shift_assignments', 'id') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT fk_attendance_sessions_shift_assignment_id
            FOREIGN KEY (shift_assignment_id) REFERENCES shift_assignments(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_sessions', 'shift_id', 'shifts', 'id') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT fk_attendance_sessions_shift_id
            FOREIGN KEY (shift_id) REFERENCES shifts(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_sessions', 'created_by_id', 'users', 'id') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT fk_attendance_sessions_created_by_id
            FOREIGN KEY (created_by_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_sessions', 'updated_by_id', 'users', 'id') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT fk_attendance_sessions_updated_by_id
            FOREIGN KEY (updated_by_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('attendance_events', 'session_id', 'attendance_sessions', 'id') THEN
        ALTER TABLE attendance_events
            ADD CONSTRAINT fk_attendance_events_session_id
            FOREIGN KEY (session_id) REFERENCES attendance_sessions(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_events', 'employee_id', 'employees', 'id') THEN
        ALTER TABLE attendance_events
            ADD CONSTRAINT fk_attendance_events_employee_id
            FOREIGN KEY (employee_id) REFERENCES employees(id)
            ON DELETE RESTRICT NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_events', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE attendance_events
            ADD CONSTRAINT fk_attendance_events_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_events', 'created_by_id', 'users', 'id') THEN
        ALTER TABLE attendance_events
            ADD CONSTRAINT fk_attendance_events_created_by_id
            FOREIGN KEY (created_by_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('attendance_evidence', 'log_id', 'attendance_logs', 'id') THEN
        ALTER TABLE attendance_evidence
            ADD CONSTRAINT fk_attendance_evidence_log_id
            FOREIGN KEY (log_id) REFERENCES attendance_logs(id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_evidence', 'event_id', 'attendance_events', 'id') THEN
        ALTER TABLE attendance_evidence
            ADD CONSTRAINT fk_attendance_evidence_event_id
            FOREIGN KEY (event_id) REFERENCES attendance_events(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_evidence', 'session_id', 'attendance_sessions', 'id') THEN
        ALTER TABLE attendance_evidence
            ADD CONSTRAINT fk_attendance_evidence_session_id
            FOREIGN KEY (session_id) REFERENCES attendance_sessions(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_evidence', 'employee_id', 'employees', 'id') THEN
        ALTER TABLE attendance_evidence
            ADD CONSTRAINT fk_attendance_evidence_employee_id
            FOREIGN KEY (employee_id) REFERENCES employees(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('attendance_audit_findings', 'audit_run_id', 'attendance_audit_runs', 'id') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT fk_attendance_audit_findings_audit_run_id
            FOREIGN KEY (audit_run_id) REFERENCES attendance_audit_runs(id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_audit_findings', 'log_id', 'attendance_logs', 'id') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT fk_attendance_audit_findings_log_id
            FOREIGN KEY (log_id) REFERENCES attendance_logs(id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_audit_findings', 'event_id', 'attendance_events', 'id') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT fk_attendance_audit_findings_event_id
            FOREIGN KEY (event_id) REFERENCES attendance_events(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_audit_findings', 'evidence_id', 'attendance_evidence', 'id') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT fk_attendance_audit_findings_evidence_id
            FOREIGN KEY (evidence_id) REFERENCES attendance_evidence(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('attendance_audit_findings', 'employee_id', 'employees', 'id') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT fk_attendance_audit_findings_employee_id
            FOREIGN KEY (employee_id) REFERENCES employees(id)
            ON DELETE SET NULL NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('leave_requests', 'employee_id', 'employees', 'id') THEN
        ALTER TABLE leave_requests
            ADD CONSTRAINT fk_leave_requests_employee_id
            FOREIGN KEY (employee_id) REFERENCES employees(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('leave_requests', 'reviewed_by_id', 'users', 'id') THEN
        ALTER TABLE leave_requests
            ADD CONSTRAINT fk_leave_requests_reviewed_by_id
            FOREIGN KEY (reviewed_by_id) REFERENCES users(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('leave_request_days', 'leave_request_id', 'leave_requests', 'id') THEN
        ALTER TABLE leave_request_days
            ADD CONSTRAINT fk_leave_request_days_leave_request_id
            FOREIGN KEY (leave_request_id) REFERENCES leave_requests(id)
            ON DELETE CASCADE NOT VALID;
    END IF;

    IF NOT pg_temp.restaurant_fk_exists('shift_plan_drafts', 'branch_id', 'branches', 'id') THEN
        ALTER TABLE shift_plan_drafts
            ADD CONSTRAINT fk_shift_plan_drafts_branch_id
            FOREIGN KEY (branch_id) REFERENCES branches(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('shift_plan_draft_assignments', 'draft_id', 'shift_plan_drafts', 'id') THEN
        ALTER TABLE shift_plan_draft_assignments
            ADD CONSTRAINT fk_shift_plan_draft_assignments_draft_id
            FOREIGN KEY (draft_id) REFERENCES shift_plan_drafts(id)
            ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT pg_temp.restaurant_fk_exists('shift_plan_draft_assignments', 'shift_id', 'shifts', 'id') THEN
        ALTER TABLE shift_plan_draft_assignments
            ADD CONSTRAINT fk_shift_plan_draft_assignments_shift_id
            FOREIGN KEY (shift_id) REFERENCES shifts(id)
            ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_employees_status') THEN
        ALTER TABLE employees
            ADD CONSTRAINT ck_employees_status
            CHECK (status IN ('active', 'inactive', 'terminated')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_employees_employment_type') THEN
        ALTER TABLE employees
            ADD CONSTRAINT ck_employees_employment_type
            CHECK (employment_type IN ('full_time', 'part_time', 'casual')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_shift_assignments_status') THEN
        ALTER TABLE shift_assignments
            ADD CONSTRAINT ck_shift_assignments_status
            CHECK (status IN ('scheduled', 'swapped', 'cancelled')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_work_calendar_day_type') THEN
        ALTER TABLE work_calendar
            ADD CONSTRAINT ck_work_calendar_day_type
            CHECK (day_type IN ('full', 'half_am', 'half_pm', 'off', 'holiday', 'overtime', 'closed', 'special_open')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_sessions_status') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT ck_attendance_sessions_status
            CHECK (status IN ('open', 'completed', 'missing_checkout', 'absent', 'cancelled')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_sessions_check_in_status') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT ck_attendance_sessions_check_in_status
            CHECK (check_in_status IN ('', 'on_time', 'late', 'early', 'manual', 'auto')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_sessions_check_out_status') THEN
        ALTER TABLE attendance_sessions
            ADD CONSTRAINT ck_attendance_sessions_check_out_status
            CHECK (check_out_status IN ('', 'normal', 'early_leave', 'overtime', 'manual', 'auto')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_events_event_type') THEN
        ALTER TABLE attendance_events
            ADD CONSTRAINT ck_attendance_events_event_type
            CHECK (event_type IN ('check_in', 'check_out', 'break_start', 'break_end', 'manual_edit', 'auto_checkout')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_logs_check_type') THEN
        ALTER TABLE attendance_logs
            ADD CONSTRAINT ck_attendance_logs_check_type
            CHECK (check_type IN ('check_in', 'check_out')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_audit_findings_risk_level') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT ck_attendance_audit_findings_risk_level
            CHECK (risk_level IN ('clear', 'low', 'medium', 'high')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attendance_audit_findings_review_status') THEN
        ALTER TABLE attendance_audit_findings
            ADD CONSTRAINT ck_attendance_audit_findings_review_status
            CHECK (review_status IN ('pending_review', 'reviewed', 'dismissed', 'confirmed')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_leave_requests_request_type') THEN
        ALTER TABLE leave_requests
            ADD CONSTRAINT ck_leave_requests_request_type
            CHECK (request_type IN ('leave', 'remote')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_leave_requests_status') THEN
        ALTER TABLE leave_requests
            ADD CONSTRAINT ck_leave_requests_status
            CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_leave_request_days_half_day') THEN
        ALTER TABLE leave_request_days
            ADD CONSTRAINT ck_leave_request_days_half_day
            CHECK (half_day IS NULL OR half_day IN ('am', 'pm')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_shift_plan_drafts_status') THEN
        ALTER TABLE shift_plan_drafts
            ADD CONSTRAINT ck_shift_plan_drafts_status
            CHECK (status IN ('draft', 'applied')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_shift_plan_draft_assignments_validation_status') THEN
        ALTER TABLE shift_plan_draft_assignments
            ADD CONSTRAINT ck_shift_plan_draft_assignments_validation_status
            CHECK (validation_status IN ('valid', 'warning', 'invalid')) NOT VALID;
    END IF;
END $$;

COMMIT;
