"""Bookings: create / reschedule / cancel with database-enforced double-booking prevention.

Defence in depth against double booking:
  1. The app checks working hours and time off (friendly 422s).
  2. It does NOT check for overlapping bookings itself. A read-then-insert check is racy: two
     requests can both read "free" and both insert. Instead the `no_double_booking` exclusion
     constraint makes Postgres reject the second insert atomically, and we map 23P01 → 409.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.availability import Interval, windows_for_day
from slotwise.db import EXCLUSION_VIOLATION, sqlstate
from slotwise.errors import AppError, NotFound, SlotTaken, ValidationFailed, VersionConflict
from slotwise.models import Booking, BookingStatus, Service
from slotwise.pagination import Cursor
from slotwise.repositories.audit import AuditRepository
from slotwise.repositories.bookings import BookingRepository
from slotwise.repositories.catalog import ScheduleRepository, ServiceRepository, StaffRepository
from slotwise.repositories.customers import CustomerRepository
from slotwise.repositories.tenancy import TenantRepository
from slotwise.security.principal import Principal


class OutsideWorkingHours(AppError):
    status_code = 422
    code = "outside_working_hours"


class StaffUnavailable(AppError):
    status_code = 422
    code = "staff_unavailable"


class InvalidBookingState(AppError):
    status_code = 409
    code = "invalid_booking_state"


class BookingService:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.bookings = BookingRepository(session, tenant_id)
        self.services = ServiceRepository(session, tenant_id)
        self.staff = StaffRepository(session, tenant_id)
        self.schedule = ScheduleRepository(session, tenant_id)
        self.customers = CustomerRepository(session, tenant_id)
        self.audit = AuditRepository(session, tenant_id)

    async def create(
        self,
        principal: Principal | None,
        *,
        service_id: UUID,
        staff_id: UUID,
        customer_id: UUID,
        start: datetime,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> Booking:
        service = await self.services.get(service_id)
        if service is None or not service.active:
            raise NotFound("service not found")
        if staff_id not in {s.id for s in await self.staff.for_service(service_id, staff_id)}:
            raise ValidationFailed("staff member does not offer this service")
        if await self.customers.get(customer_id) is None:
            raise NotFound("customer not found")
        start = _as_utc(start)
        period, ends_at = await self._validated_period(service, staff_id, start, now)

        booking = self.bookings.add(
            Booking(
                staff_id=staff_id,
                service_id=service_id,
                customer_id=customer_id,
                period=period,
                starts_at=start,
                ends_at=ends_at,
                status=(
                    BookingStatus.PENDING_PAYMENT
                    if service.price_paise > 0
                    else BookingStatus.CONFIRMED
                ),
                price_paise=service.price_paise,
                notes=notes,
            )
        )
        async with self._overlap_is_409():
            await self.session.flush()
        self.audit.record(
            principal,
            action="booking.created",
            entity="booking",
            entity_id=booking.id,
            diff={"start": start.isoformat()},
        )
        await self.on_created(booking)
        return booking

    async def reschedule(
        self,
        principal: Principal | None,
        booking_id: UUID,
        *,
        start: datetime,
        version: int,
        now: datetime | None = None,
    ) -> Booking:
        booking = await self.get(booking_id)
        if not booking.status.is_active:
            raise InvalidBookingState(f"cannot reschedule a {booking.status.value} booking")
        service = await self.services.get(booking.service_id)
        assert service is not None  # FK guarantees it
        start = _as_utc(start)
        period, ends_at = await self._validated_period(service, booking.staff_id, start, now)
        old_start = booking.starts_at
        async with self._overlap_is_409():
            updated = await self.bookings.update_versioned(
                booking_id, version, period=period, starts_at=start, ends_at=ends_at
            )
        if not updated:
            raise VersionConflict("booking was modified by someone else; reload and retry")
        await self.session.refresh(booking)
        self.audit.record(
            principal,
            action="booking.rescheduled",
            entity="booking",
            entity_id=booking_id,
            diff={"from": old_start.isoformat(), "to": start.isoformat()},
        )
        await self.on_rescheduled(booking, old_start)
        return booking

    async def cancel(
        self,
        principal: Principal | None,
        booking_id: UUID,
        *,
        version: int | None = None,
        reason: str | None = None,
    ) -> Booking:
        booking = await self.get(booking_id)
        if booking.status is BookingStatus.CANCELLED:
            return booking  # cancelling twice is a no-op, not an error
        if not booking.status.is_active:
            raise InvalidBookingState(f"cannot cancel a {booking.status.value} booking")
        expected = booking.version if version is None else version
        if not await self.bookings.update_versioned(
            booking_id, expected, status=BookingStatus.CANCELLED
        ):
            raise VersionConflict("booking was modified by someone else; reload and retry")
        await self.session.refresh(booking)
        self.audit.record(
            principal,
            action="booking.cancelled",
            entity="booking",
            entity_id=booking_id,
            diff={"reason": reason},
        )
        await self.on_cancelled(booking, reason)
        return booking

    async def confirm_paid(self, booking_id: UUID) -> Booking:
        booking = await self.get(booking_id, for_update=True)
        if booking.status is BookingStatus.PENDING_PAYMENT:
            await self.bookings.update_versioned(
                booking_id, booking.version, status=BookingStatus.CONFIRMED
            )
            await self.session.refresh(booking)
            self.audit.record(
                None, action="booking.confirmed", entity="booking", entity_id=booking_id
            )
            await self.on_confirmed(booking)
        return booking

    async def get(self, booking_id: UUID, *, for_update: bool = False) -> Booking:
        booking = await self.bookings.get(booking_id, for_update=for_update)
        if booking is None:
            raise NotFound("booking not found")
        return booking

    async def page(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        staff_id: UUID | None,
        status: BookingStatus | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[Booking], str | None]:
        after = None
        if cursor:
            c = Cursor.decode(cursor)
            after = (c.created_at, c.id)  # here the cursor's timestamp is starts_at
        rows = await self.bookings.find(
            start=start, end=end, staff_id=staff_id, status=status, limit=limit + 1, after=after
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = Cursor(last.starts_at, last.id).encode()
        return rows[:limit], next_cursor

    # --- hooks: overridden by the outbox-aware subclass in M5 ---------------------------------

    async def on_created(self, booking: Booking) -> None: ...
    async def on_rescheduled(self, booking: Booking, old_start: datetime) -> None: ...
    async def on_cancelled(self, booking: Booking, reason: str | None) -> None: ...
    async def on_confirmed(self, booking: Booking) -> None: ...

    # --- internals ---------------------------------------------------------------------------

    async def _validated_period(
        self, service: Service, staff_id: UUID, start: datetime, now: datetime | None
    ) -> tuple[Range[datetime], datetime]:
        if start <= (now or datetime.now(UTC)):
            raise ValidationFailed("start must be in the future")
        ends_at = start + timedelta(minutes=service.duration_min)
        block_end = ends_at + timedelta(minutes=service.buffer_min)

        tenant = await TenantRepository(self.session).get(self.tenant_id)
        assert tenant is not None
        tz = ZoneInfo(tenant.timezone)
        local_day = start.astimezone(tz).date()
        hours = await self.schedule.working_hours([staff_id], weekday=local_day.weekday())
        windows = windows_for_day(local_day, tz, [(h.start_time, h.end_time) for h in hours])
        if not any(w.start <= start and ends_at <= w.end for w in windows):
            raise OutsideWorkingHours("requested time is outside the staff member's hours")
        block = Interval(start, block_end)
        for off in await self.schedule.time_off([staff_id], start, block_end):
            if (
                off.period.lower
                and off.period.upper
                and block.overlaps(Interval(off.period.lower, off.period.upper))
            ):
                raise StaffUnavailable("staff member is on time off")
        return Range(start, block_end, bounds="[)"), ends_at

    @asynccontextmanager
    async def _overlap_is_409(self) -> AsyncIterator[None]:
        """Translate the exclusion-constraint violation into a clean 409 slot_taken."""
        try:
            yield
        except IntegrityError as exc:
            if sqlstate(exc) == EXCLUSION_VIOLATION:
                raise SlotTaken("that slot was just taken; pick another") from exc
            raise


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValidationFailed("start must include a timezone offset")
    return value.astimezone(UTC)
