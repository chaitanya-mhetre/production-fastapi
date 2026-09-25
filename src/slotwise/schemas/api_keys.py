from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from slotwise.schemas.common import ORMModel
from slotwise.security.permissions import Permission


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Permission] = Field(min_length=1)
    rate_limit_per_min: int = Field(default=600, ge=1, le=100_000)


class ApiKeyOut(ORMModel):
    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    rate_limit_per_min: int
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedOut(ApiKeyOut):
    key: str = Field(description="Shown once. Store it now; only a hash is kept server-side.")
