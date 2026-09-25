from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from slotwise.schemas.common import ORMModel


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(pattern=r"^\+?[0-9]{7,15}$")
    email: EmailStr | None = None


class CustomerOut(ORMModel):
    id: UUID
    name: str
    phone: str
    email: str | None
    created_at: datetime
