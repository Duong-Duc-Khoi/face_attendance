"""
Schemas and normalization helpers for store employee profiles.

User.role remains the system authorization role. Employee.job_role is the
primary operating role used by legacy scheduling and reporting. Employee.job_roles
stores all roles an employee can cover in a yogurt pearl milk tea store.
"""

import unicodedata
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


JOB_ROLES = ("security", "cashier", "barista", "table_service")
STORE_ROLES = ("store_manager", "assistant_manager", "staff")
EMPLOYMENT_TYPES = ("full_time", "part_time", "casual")
EMPLOYEE_STATUSES = ("active", "inactive", "terminated")

JOB_ROLE_LABELS = {
    "security": "Bảo vệ",
    "cashier": "Thu ngân",
    "barista": "Pha chế",
    "table_service": "Phục vụ bàn",
    # Legacy aliases kept readable while old records are migrated.
    "server": "Phục vụ bàn",
    "bar": "Pha chế",
    "shift_lead": "Trưởng ca",
}

STORE_ROLE_LABELS = {
    "store_manager": "Cửa hàng trưởng",
    "assistant_manager": "Cửa hàng phó",
    "staff": "Nhân viên",
}

_JOB_ROLE_ALIASES = {
    "bao ve": "security",
    "security": "security",
    "guard": "security",
    "phuc vu": "table_service",
    "phuc vu ban": "table_service",
    "server": "table_service",
    "waiter": "table_service",
    "waitress": "table_service",
    "thu ngan": "cashier",
    "cashier": "cashier",
    "barista": "barista",
    "pha che": "barista",
    "quay pha che": "barista",
    "bar": "barista",
    "bartender": "barista",
    "quan ly ca": "shift_lead",
    "truong ca": "shift_lead",
    "ca truong": "shift_lead",
    "shift lead": "shift_lead",
    "shift_lead": "shift_lead",
}

_STORE_ROLE_ALIASES = {
    "cua hang truong": "store_manager",
    "truong cua hang": "store_manager",
    "quan ly cua hang": "store_manager",
    "store manager": "store_manager",
    "store_manager": "store_manager",
    "cua hang pho": "assistant_manager",
    "pho cua hang": "assistant_manager",
    "assistant manager": "assistant_manager",
    "assistant_manager": "assistant_manager",
    "nhan vien": "staff",
    "staff": "staff",
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


def normalize_job_roles(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = value.replace(";", ",").split(",")
    else:
        raw_items = list(value)
    roles: list[str] = []
    for item in raw_items:
        role = normalize_job_role(str(item or ""))
        if role and role not in roles:
            roles.append(role)
    return roles


def normalize_store_role(value: str | None) -> str:
    cleaned = normalize_text(value or "staff").replace("-", " ").replace("_", " ")
    normalized = _STORE_ROLE_ALIASES.get(cleaned, cleaned.replace(" ", "_"))
    return normalized if normalized in STORE_ROLES else "staff"


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
    store_role: Optional[str] = None
    job_role: Optional[str] = None
    job_roles: Optional[list[str] | str] = None
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

    @field_validator("job_roles")
    @classmethod
    def validate_job_roles(cls, value):
        return normalize_job_roles(value) if value is not None else value

    @field_validator("store_role")
    @classmethod
    def validate_store_role(cls, value):
        return normalize_store_role(value) if value is not None else value

    @field_validator("employment_type")
    @classmethod
    def validate_employment_type(cls, value):
        return normalize_employment_type(value) if value is not None else value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        return normalize_employee_status(value) if value is not None else value
