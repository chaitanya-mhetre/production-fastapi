"""Inbound payment webhooks (a mock provider standing in for Razorpay/Stripe).

Providers retry webhooks aggressively, so processing must be idempotent: the payment row is
inserted with ON CONFLICT (provider, provider_ref) DO NOTHING. A duplicate is acknowledged
with 200 and changes nothing.
"""

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.errors import NotFound
from slotwise.models import Payment
from slotwise.schemas.webhooks import MockpayEventIn
from slotwise.services.booking_events import EventedBookingService

PROVIDER = "mockpay"


class PaymentService:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def handle_mockpay(self, event: MockpayEventIn, raw: dict[str, Any]) -> dict[str, Any]:
        bookings = EventedBookingService(self.session, self.tenant_id)
        try:
            booking = await bookings.get(event.booking_id, for_update=True)
        except NotFound:
            return {"status": "ignored", "reason": "unknown booking"}

        amount_ok = event.amount_paise == booking.price_paise
        status = event.status if amount_ok else "amount_mismatch"
        inserted = await self.session.execute(
            insert(Payment)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                booking_id=booking.id,
                provider=PROVIDER,
                provider_ref=event.provider_ref,
                amount_paise=event.amount_paise,
                status=status,
                raw=raw,
            )
            .on_conflict_do_nothing(index_elements=["provider", "provider_ref"])
            .returning(Payment.id)
        )
        if inserted.scalar_one_or_none() is None:
            return {"status": "duplicate"}
        if status == "succeeded":
            await bookings.confirm_paid(booking.id)
        return {"status": status, "booking_status": booking.status.value}
