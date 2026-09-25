from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import Range

from slotwise.models import Service, Staff, StaffService, TimeOff, WorkingHours
from slotwise.repositories.base import TenantScopedRepository


class ServiceRepository(TenantScopedRepository):
    async def get(self, service_id: UUID) -> Service | None:
        result = await self.session.execute(
            select(Service).where(Service.id == service_id, Service.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def find_all(self, *, include_inactive: bool = False) -> list[Service]:
        stmt = select(Service).where(Service.tenant_id == self.tenant_id)
        if not include_inactive:
            stmt = stmt.where(Service.active)
        return list((await self.session.execute(stmt.order_by(Service.name))).scalars())

    def add(self, service: Service) -> Service:
        service.tenant_id = self.tenant_id
        self.session.add(service)
        return service


class StaffRepository(TenantScopedRepository):
    async def get(self, staff_id: UUID) -> Staff | None:
        result = await self.session.execute(
            select(Staff).where(Staff.id == staff_id, Staff.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def find_all(self) -> list[Staff]:
        stmt = select(Staff).where(Staff.tenant_id == self.tenant_id).order_by(Staff.display_name)
        return list((await self.session.execute(stmt)).scalars())

    def add(self, staff: Staff) -> Staff:
        staff.tenant_id = self.tenant_id
        self.session.add(staff)
        return staff

    async def service_ids(self, staff_id: UUID) -> list[UUID]:
        result = await self.session.execute(
            select(StaffService.service_id).where(
                StaffService.staff_id == staff_id, StaffService.tenant_id == self.tenant_id
            )
        )
        return list(result.scalars())

    async def set_services(self, staff_id: UUID, service_ids: list[UUID]) -> None:
        await self.session.execute(
            delete(StaffService).where(
                StaffService.staff_id == staff_id, StaffService.tenant_id == self.tenant_id
            )
        )
        self.session.add_all(
            StaffService(staff_id=staff_id, service_id=sid, tenant_id=self.tenant_id)
            for sid in dict.fromkeys(service_ids)  # dedupe, keep order
        )

    async def for_service(self, service_id: UUID, staff_id: UUID | None = None) -> list[Staff]:
        stmt = (
            select(Staff)
            .join(StaffService, StaffService.staff_id == Staff.id)
            .where(
                StaffService.service_id == service_id,
                Staff.tenant_id == self.tenant_id,
                Staff.active,
            )
        )
        if staff_id is not None:
            stmt = stmt.where(Staff.id == staff_id)
        return list((await self.session.execute(stmt.order_by(Staff.display_name))).scalars())


class ScheduleRepository(TenantScopedRepository):
    async def working_hours(
        self, staff_ids: list[UUID], weekday: int | None = None
    ) -> list[WorkingHours]:
        stmt = select(WorkingHours).where(
            WorkingHours.tenant_id == self.tenant_id, WorkingHours.staff_id.in_(staff_ids)
        )
        if weekday is not None:
            stmt = stmt.where(WorkingHours.weekday == weekday)
        stmt = stmt.order_by(WorkingHours.weekday, WorkingHours.start_time)
        return list((await self.session.execute(stmt)).scalars())

    async def replace_working_hours(self, staff_id: UUID, rows: list[WorkingHours]) -> None:
        await self.session.execute(
            delete(WorkingHours).where(
                WorkingHours.staff_id == staff_id, WorkingHours.tenant_id == self.tenant_id
            )
        )
        for row in rows:
            row.tenant_id, row.staff_id = self.tenant_id, staff_id
        self.session.add_all(rows)

    async def time_off(
        self, staff_ids: list[UUID], start: datetime, end: datetime
    ) -> list[TimeOff]:
        stmt = select(TimeOff).where(
            TimeOff.tenant_id == self.tenant_id,
            TimeOff.staff_id.in_(staff_ids),
            TimeOff.period.overlaps(Range(start, end)),
        )
        return list((await self.session.execute(stmt.order_by(TimeOff.period))).scalars())

    async def get_time_off(self, time_off_id: UUID) -> TimeOff | None:
        result = await self.session.execute(
            select(TimeOff).where(TimeOff.id == time_off_id, TimeOff.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    def add_time_off(self, row: TimeOff) -> TimeOff:
        row.tenant_id = self.tenant_id
        self.session.add(row)
        return row
