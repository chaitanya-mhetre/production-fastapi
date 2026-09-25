"""Placeholder seam; M5 makes this publish domain events through the transactional outbox."""

from slotwise.services.bookings import BookingService


class EventedBookingService(BookingService):
    pass
