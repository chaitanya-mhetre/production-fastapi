from datetime import datetime
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import BaseModel, EmailStr, Field, field_validator

from slotwise.models import Role, TenantPlan, TenantStatus
from slotwise.schemas.common import ORMModel

_TIMEZONES = available_timezones()


def _check_tz(value: str) -> str:
    if value not in _TIMEZONES:
        raise ValueError(f"unknown timezone {value!r}")
    return value


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    tenant_slug: str | None = None  # None → platform (superadmin) token


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    refresh_token: str | None = None


class TenantCreateIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,48}$")
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "Asia/Kolkata"
    plan: TenantPlan = TenantPlan.FREE
    admin_email: EmailStr
    admin_password: str = Field(min_length=10)
    admin_name: str = Field(min_length=1)

    _tz = field_validator("timezone")(_check_tz)


class TenantUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return _check_tz(value) if value is not None else None


class TenantOut(ORMModel):
    id: UUID
    slug: str
    name: str
    plan: TenantPlan
    timezone: str
    status: TenantStatus
    created_at: datetime


class MemberCreateIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1)
    password: str = Field(min_length=10)
    role: Role


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    role: Role
