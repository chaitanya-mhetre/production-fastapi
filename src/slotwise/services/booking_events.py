"""BookingService that publishes domain events through the transactional outbox."""

from datetime import datetime
from typing import Any

from slotwise.models import Booking
from slotwise.outbox.writer import emit
from slotwise.services.bookings import BookingService

EVENT_TYPES = ("booking.created", "booking.rescheduled", "booking.cancelled", "booking.confirmed")


def booking_payload(booking: Booking) -> dict[str, Any]:
    return {
        "id": str(booking.id),
        "service_id": str(booking.service_id),
        "staff_id": str(booking.staff_id),
        "customer_id": str(booking.customer_id),
        "starts_at": booking.starts_at.isoformat(),
        "ends_at": booking.ends_at.isoformat(),
        "status": booking.status.value,
        "price_paise": booking.price_paise,
        "version": booking.version,
    }


class EventedBookingService(BookingService):
    def _emit(self, booking: Booking, event_type: str, **extra: Any) -> None:
        emit(
            self.session,
            tenant_id=self.tenant_id,
            aggregate_type="booking",
            aggregate_id=booking.id,
            event_type=event_type,
            payload=booking_payload(booking) | extra,
        )

    async def on_created(self, booking: Booking) -> None:
        self._emit(booking, "booking.created")

    async def on_rescheduled(self, booking: Booking, old_start: datetime) -> None:
        self._emit(booking, "booking.rescheduled", previous_starts_at=old_start.isoformat())

    async def on_cancelled(self, booking: Booking, reason: str | None) -> None:
        self._emit(booking, "booking.cancelled", reason=reason)

    async def on_confirmed(self, booking: Booking) -> None:
        self._emit(booking, "booking.confirmed")
