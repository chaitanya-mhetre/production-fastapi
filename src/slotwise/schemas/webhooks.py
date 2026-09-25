from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl

from slotwise.models import DeliveryStatus
from slotwise.schemas.common import ORMModel


class WebhookEndpointIn(BaseModel):
    url: HttpUrl
    events: list[str] = Field(min_length=1)


class WebhookEndpointOut(ORMModel):
    id: UUID
    url: str
    events: list[str]
    active: bool
    consecutive_failures: int
    disabled_reason: str | None
    created_at: datetime


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    secret: str = Field(
        description="Signing secret; shown once. Verify Slotwise-Signature with it."
    )


class WebhookDeliveryOut(ORMModel):
    id: UUID
    endpoint_id: UUID
    outbox_event_id: int
    event_type: str
    status: DeliveryStatus
    attempt: int
    status_code: int | None
    response_ms: int | None
    last_error: str | None
    next_retry_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime


class MockpayEventIn(BaseModel):
    """What the (mock) payment provider POSTs to us after a checkout."""

    provider_ref: str = Field(min_length=4, max_length=100)
    tenant_id: UUID
    booking_id: UUID
    amount_paise: int = Field(ge=0)
    status: str = Field(pattern="^(succeeded|failed)$")
