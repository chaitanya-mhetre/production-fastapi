"""Catalogue: services, staff, schedules (working hours + time off)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.errors import NotFound, ValidationFailed
from slotwise.models import Service, Staff, TimeOff, WorkingHours
from slotwise.repositories.audit import AuditRepository
from slotwise.repositories.catalog import ScheduleRepository, ServiceRepository, StaffRepository
from slotwise.repositories.tenancy import MembershipRepository
from slotwise.schemas.catalog import (
    ServiceIn,
    ServiceUpdateIn,
    StaffIn,
    StaffUpdateIn,
    WorkingHoursItem,
)
from slotwise.security.principal import Principal


class CatalogService:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.services = ServiceRepository(session, tenant_id)
        self.staff = StaffRepository(session, tenant_id)
        self.schedule = ScheduleRepository(session, tenant_id)
        self.audit = AuditRepository(session, tenant_id)

    # --- services -------------------------------------------------------------

    async def create_service(self, principal: Principal, data: ServiceIn) -> Service:
        service = self.services.add(Service(**data.model_dump()))
        await self.session.flush()
        self.audit.record(
            principal,
            action="service.created",
            entity="service",
            entity_id=service.id,
            diff=data.model_dump(),
        )
        return service

    async def get_service(self, service_id: UUID) -> Service:
        service = await self.services.get(service_id)
        if service is None:
            raise NotFound("service not found")
        return service

    async def update_service(
        self, principal: Principal, service_id: UUID, data: ServiceUpdateIn
    ) -> Service:
        service = await self.get_service(service_id)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(service, field, value)
        self.audit.record(
            principal,
            action="service.updated",
            entity="service",
            entity_id=service_id,
            diff=changes,
        )
        return service

    # --- staff ----------------------------------------------------------------

    async def create_staff(self, principal: Principal, data: StaffIn) -> tuple[Staff, list[UUID]]:
        if data.user_id is not None:
            await self._ensure_member(data.user_id)
        staff = self.staff.add(Staff(display_name=data.display_name, user_id=data.user_id))
        await self.session.flush()
        await self._assign_services(staff.id, data.service_ids)
        self.audit.record(principal, action="staff.created", entity="staff", entity_id=staff.id)
        return staff, list(dict.fromkeys(data.service_ids))

    async def get_staff(self, staff_id: UUID) -> Staff:
        staff = await self.staff.get(staff_id)
        if staff is None:
            raise NotFound("staff not found")
        return staff

    async def update_staff(
        self, principal: Principal, staff_id: UUID, data: StaffUpdateIn
    ) -> Staff:
        staff = await self.get_staff(staff_id)
        changes = data.model_dump(exclude_unset=True, exclude={"service_ids"})
        for field, value in changes.items():
            setattr(staff, field, value)
        if data.service_ids is not None:
            await self._assign_services(staff_id, data.service_ids)
        self.audit.record(
            principal,
            action="staff.updated",
            entity="staff",
            entity_id=staff_id,
            diff=data.model_dump(exclude_unset=True, mode="json"),
        )
        return staff

    async def _assign_services(self, staff_id: UUID, service_ids: list[UUID]) -> None:
        for sid in service_ids:
            if await self.services.get(sid) is None:  # also rejects other tenants' services
                raise ValidationFailed(f"unknown service {sid}")
        await self.staff.set_services(staff_id, service_ids)
        await self.session.flush()

    async def _ensure_member(self, user_id: UUID) -> None:
        if await MembershipRepository(self.session).get(user_id, self.tenant_id) is None:
            raise ValidationFailed("user_id is not a member of this tenant")

    # --- schedule -------------------------------------------------------------

    async def set_working_hours(
        self, principal: Principal, staff_id: UUID, items: list[WorkingHoursItem]
    ) -> list[WorkingHours]:
        await self.get_staff(staff_id)
        rows = [
            WorkingHours(weekday=i.weekday, start_time=i.start_time, end_time=i.end_time)
            for i in items
        ]
        await self.schedule.replace_working_hours(staff_id, rows)
        await self.session.flush()
        self.audit.record(
            principal,
            action="staff.working_hours_set",
            entity="staff",
            entity_id=staff_id,
            diff={"items": len(rows)},
        )
        return rows

    async def add_time_off(
        self,
        principal: Principal,
        staff_id: UUID,
        start: datetime,
        end: datetime,
        reason: str | None,
    ) -> TimeOff:
        await self.get_staff(staff_id)
        row = self.schedule.add_time_off(
            TimeOff(staff_id=staff_id, period=Range(start, end), reason=reason)
        )
        await self.session.flush()
        self.audit.record(
            principal, action="staff.time_off_added", entity="time_off", entity_id=row.id
        )
        return row

    async def delete_time_off(
        self, principal: Principal, staff_id: UUID, time_off_id: UUID
    ) -> None:
        row = await self.schedule.get_time_off(time_off_id)
        if row is None or row.staff_id != staff_id:
            raise NotFound("time off not found")
        await self.session.delete(row)
        self.audit.record(
            principal, action="staff.time_off_deleted", entity="time_off", entity_id=time_off_id
        )
