-- Destructive cleanup for restaurant operating calendar.
-- This intentionally removes legacy company-style calendar fields.

UPDATE work_calendar
SET day_type = 'off'
WHERE day_type = 'closed';

UPDATE work_calendar
SET day_type = 'full'
WHERE day_type IN ('half_am', 'half_pm', 'overtime', 'special_open');

ALTER TABLE work_calendar
    DROP CONSTRAINT IF EXISTS ck_work_calendar_day_type;

ALTER TABLE work_calendar
    ADD CONSTRAINT ck_work_calendar_day_type
    CHECK (day_type IN ('full', 'off', 'holiday')) NOT VALID;

ALTER TABLE work_calendar
    DROP COLUMN IF EXISTS work_start,
    DROP COLUMN IF EXISTS work_end;

ALTER TABLE work_calendar
    VALIDATE CONSTRAINT ck_work_calendar_day_type;
