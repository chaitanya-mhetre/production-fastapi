from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from slotwise.models import BookingStatus
from slotwise.schemas.common import ORMModel


class BookingCreateIn(BaseModel):
    service_id: UUID
    staff_id: UUID
    customer_id: UUID
    start: AwareDatetime
    notes: str | None = Field(default=None, max_length=2000)


class BookingRescheduleIn(BaseModel):
    start: AwareDatetime
    version: int = Field(ge=1, description="The version you last read (optimistic locking)")


class BookingCancelIn(BaseModel):
    version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=500)


class BookingOut(ORMModel):
    id: UUID
    service_id: UUID
    staff_id: UUID
    customer_id: UUID
    starts_at: datetime
    ends_at: datetime
    status: BookingStatus
    price_paise: int
    notes: str | None
    version: int
    created_at: datetime
