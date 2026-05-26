"""
Schemas and normalization helpers for restaurant employee profiles.

User.role remains the system authorization role. Employee.job_role is the
restaurant operating role used by scheduling and reporting.
"""

import unicodedata
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


JOB_ROLES = ("server", "cashier", "kitchen", "bar", "cleaner", "shift_lead")
EMPLOYMENT_TYPES = ("full_time", "part_time", "casual")
EMPLOYEE_STATUSES = ("active", "inactive", "terminated")

JOB_ROLE_LABELS = {
    "server": "Phục vụ",
    "cashier": "Thu ngân",
    "kitchen": "Bếp",
    "bar": "Bar",
    "cleaner": "Tạp vụ",
    "shift_lead": "Quản lý ca",
}

_JOB_ROLE_ALIASES = {
    "phuc vu": "server",
    "server": "server",
    "waiter": "server",
    "waitress": "server",
    "thu ngan": "cashier",
    "cashier": "cashier",
    "bep": "kitchen",
    "dau bep": "kitchen",
    "phu bep": "kitchen",
    "kitchen": "kitchen",
    "cook": "kitchen",
    "chef": "kitchen",
    "bar": "bar",
    "bartender": "bar",
    "pha che": "bar",
    "tap vu": "cleaner",
    "ve sinh": "cleaner",
    "cleaner": "cleaner",
    "quan ly ca": "shift_lead",
    "truong ca": "shift_lead",
    "ca truong": "shift_lead",
    "shift lead": "shift_lead",
    "shift_lead": "shift_lead",
}


def normalize_text(value: str) -> str:
    raw = (value or "").replace("đ", "d").replace("Đ", "D")
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", raw.lower())
        if not unicodedata.combining(ch)
    ).strip()


def normalize_job_role(value: str) -> str:
    cleaned = normalize_text(value).replace("-", " ").replace("_", " ")
    return _JOB_ROLE_ALIASES.get(cleaned, value.strip() if value else "")


def normalize_employment_type(value: str | None) -> str:
    cleaned = normalize_text(value or "full_time").replace("-", "_").replace(" ", "_")
    return cleaned if cleaned in EMPLOYMENT_TYPES else "full_time"


def normalize_employee_status(value: str | None, is_active: bool | None = None) -> str:
    cleaned = normalize_text(value or "").replace("-", "_").replace(" ", "_")
    if cleaned in EMPLOYEE_STATUSES:
        return cleaned
    if is_active is False:
        return "inactive"
    return "active"


def is_active_for_status(status: str) -> bool:
    return status == "active"


class EmployeeUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    full_name: Optional[str] = None
    branch_id: Optional[int] = None
    department: Optional[str] = None
    position: Optional[str] = None
    job_role: Optional[str] = None
    employment_type: Optional[str] = None
    hourly_rate: Optional[Decimal] = None
    base_salary: Optional[Decimal] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("job_role")
    @classmethod
    def validate_job_role(cls, value):
        return normalize_job_role(value or "") if value is not None else value

    @field_validator("employment_type")
    @classmethod
    def validate_employment_type(cls, value):
        return normalize_employment_type(value) if value is not None else value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        return normalize_employee_status(value) if value is not None else value
