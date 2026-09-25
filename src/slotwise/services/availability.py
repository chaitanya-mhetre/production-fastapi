"""Loads schedules from the DB and runs the pure availability algorithm per staff member."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.availability import Interval, compute_slots, day_bounds_utc
from slotwise.errors import NotFound
from slotwise.repositories.catalog import ScheduleRepository, ServiceRepository, StaffRepository
from slotwise.repositories.tenancy import TenantRepository


class AvailabilityService:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.services = ServiceRepository(session, tenant_id)
        self.staff = StaffRepository(session, tenant_id)
        self.schedule = ScheduleRepository(session, tenant_id)

    async def slots(
        self,
        *,
        service_id: UUID,
        day: date,
        staff_id: UUID | None = None,
        now: datetime | None = None,
    ) -> list[tuple[UUID, Interval]]:
        tenant = await TenantRepository(self.session).get(self.tenant_id)
        service = await self.services.get(service_id)
        if tenant is None or service is None or not service.active:
            raise NotFound("service not found")
        tz = ZoneInfo(tenant.timezone)
        staff = await self.staff.for_service(service_id, staff_id)
        if not staff:
            return []
        staff_ids = [s.id for s in staff]
        bounds = day_bounds_utc(day, tz)
        # Query a little wider than the day: a booking that starts late the previous evening can
        # still block this morning, and trailing buffers can cross midnight.
        margin = timedelta(hours=12)
        hours = await self.schedule.working_hours(staff_ids, weekday=day.weekday())
        busy = await self.busy_intervals(staff_ids, bounds.start - margin, bounds.end + margin)

        result: list[tuple[UUID, Interval]] = []
        for member in staff:
            member_hours = [(h.start_time, h.end_time) for h in hours if h.staff_id == member.id]
            for slot in compute_slots(
                day=day,
                tz=tz,
                working_hours=member_hours,
                busy=busy.get(member.id, []),
                duration=timedelta(minutes=service.duration_min),
                buffer=timedelta(minutes=service.buffer_min),
                not_before=now or datetime.now(UTC),
            ):
                result.append((member.id, slot))
        result.sort(key=lambda item: (item[1].start, str(item[0])))
        return result

    async def busy_intervals(
        self, staff_ids: list[UUID], start: datetime, end: datetime
    ) -> dict[UUID, list[Interval]]:
        busy: dict[UUID, list[Interval]] = {}
        for row in await self.schedule.time_off(staff_ids, start, end):
            if row.period.lower and row.period.upper:
                busy.setdefault(row.staff_id, []).append(
                    Interval(row.period.lower, row.period.upper)
                )
        return busy
