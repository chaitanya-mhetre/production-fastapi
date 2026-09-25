from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import Range

from slotwise.models import Booking, BookingStatus
from slotwise.repositories.base import TenantScopedRepository

ACTIVE = (BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED)


class BookingRepository(TenantScopedRepository):
    async def get(self, booking_id: UUID, *, for_update: bool = False) -> Booking | None:
        stmt = select(Booking).where(Booking.id == booking_id, Booking.tenant_id == self.tenant_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_staff_schedule(self, staff_id: UUID) -> None:
        """Serialise writes to one staff member's calendar until this transaction ends.

        Two concurrent INSERTs with overlapping periods can each wait on the other's uncommitted
        row while Postgres checks the exclusion constraint, and Postgres then aborts one with
        40P01 deadlock_detected (after deadlock_timeout, 1 s by default). Taking a per-staff
        transaction-level advisory lock first turns that into a short, ordered wait. The
        exclusion constraint is still what guarantees correctness; this only removes the deadlock.
        """
        key = f"slotwise:booking:{self.tenant_id}:{staff_id}"
        # no_autoflush: the lock must be held *before* a pending INSERT is sent, not after.
        with self.session.no_autoflush:
            await self.session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
            )

    def add(self, booking: Booking) -> Booking:
        booking.tenant_id = self.tenant_id
        self.session.add(booking)
        return booking

    async def reserved_blocks(
        self, staff_ids: list[UUID], start: datetime, end: datetime
    ) -> list[tuple[UUID, Range[datetime]]]:
        result = await self.session.execute(
            select(Booking.staff_id, Booking.period).where(
                Booking.tenant_id == self.tenant_id,
                Booking.staff_id.in_(staff_ids),
                Booking.status.in_(ACTIVE),
                Booking.period.overlaps(Range(start, end)),
            )
        )
        return [(row[0], row[1]) for row in result]

    async def update_versioned(
        self, booking_id: UUID, expected_version: int, **values: object
    ) -> bool:
        """Optimistic locking: the UPDATE only matches if nobody changed the row since it was read.

        Returns False on a lost race (the caller maps that to 409 version_conflict).
        """
        result = await self.session.execute(
            update(Booking)
            .where(
                Booking.id == booking_id,
                Booking.tenant_id == self.tenant_id,
                Booking.version == expected_version,
            )
            .values(**values, version=Booking.version + 1)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def find(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        staff_id: UUID | None,
        status: BookingStatus | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[Booking]:
        stmt = select(Booking).where(Booking.tenant_id == self.tenant_id)
        if start:
            stmt = stmt.where(Booking.starts_at >= start)
        if end:
            stmt = stmt.where(Booking.starts_at < end)
        if staff_id:
            stmt = stmt.where(Booking.staff_id == staff_id)
        if status:
            stmt = stmt.where(Booking.status == status)
        if after:
            stmt = stmt.where(tuple_(Booking.starts_at, Booking.id) > after)
        stmt = stmt.order_by(Booking.starts_at, Booking.id).limit(limit)
        return list((await self.session.execute(stmt)).scalars())
