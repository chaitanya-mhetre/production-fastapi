from datetime import UTC, date, datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from slotwise.api.deps import TenantSessionDep, require, tenant_of
from slotwise.models import Staff, TimeOff
from slotwise.schemas.catalog import (
    ServiceIn,
    ServiceOut,
    ServiceUpdateIn,
    SlotOut,
    StaffIn,
    StaffOut,
    StaffUpdateIn,
    TimeOffIn,
    TimeOffOut,
    WorkingHoursIn,
    WorkingHoursItem,
)
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal
from slotwise.services.availability import AvailabilityService
from slotwise.services.catalog import CatalogService

router = APIRouter(prefix="/v1", tags=["catalog"])

Reader = Annotated[Principal, Depends(require(P.CATALOG_READ))]
Writer = Annotated[Principal, Depends(require(P.CATALOG_WRITE))]


def _staff_out(staff: Staff, service_ids: list[UUID]) -> StaffOut:
    return StaffOut(
        id=staff.id,
        display_name=staff.display_name,
        user_id=staff.user_id,
        avatar_key=staff.avatar_key,
        active=staff.active,
        service_ids=service_ids,
    )


def _time_off_out(row: TimeOff) -> TimeOffOut:
    assert row.period.lower is not None and row.period.upper is not None
    return TimeOffOut(
        id=row.id,
        staff_id=row.staff_id,
        start=row.period.lower,
        end=row.period.upper,
        reason=row.reason,
    )


# --- services -----------------------------------------------------------------


@router.post("/services", status_code=status.HTTP_201_CREATED)
async def create_service(
    body: ServiceIn, principal: Writer, session: TenantSessionDep
) -> ServiceOut:
    service = await CatalogService(session, tenant_of(principal)).create_service(principal, body)
    await session.commit()
    return ServiceOut.model_validate(service)


@router.get("/services")
async def list_services(
    principal: Reader, session: TenantSessionDep, include_inactive: bool = False
) -> list[ServiceOut]:
    rows = await CatalogService(session, tenant_of(principal)).services.find_all(
        include_inactive=include_inactive
    )
    return [ServiceOut.model_validate(r) for r in rows]


@router.get("/services/{service_id}")
async def get_service(service_id: UUID, principal: Reader, session: TenantSessionDep) -> ServiceOut:
    return ServiceOut.model_validate(
        await CatalogService(session, tenant_of(principal)).get_service(service_id)
    )


@router.patch("/services/{service_id}")
async def update_service(
    service_id: UUID, body: ServiceUpdateIn, principal: Writer, session: TenantSessionDep
) -> ServiceOut:
    service = await CatalogService(session, tenant_of(principal)).update_service(
        principal, service_id, body
    )
    await session.commit()
    return ServiceOut.model_validate(service)


# --- staff --------------------------------------------------------------------


@router.post("/staff", status_code=status.HTTP_201_CREATED)
async def create_staff(body: StaffIn, principal: Writer, session: TenantSessionDep) -> StaffOut:
    staff, service_ids = await CatalogService(session, tenant_of(principal)).create_staff(
        principal, body
    )
    await session.commit()
    return _staff_out(staff, service_ids)


@router.get("/staff")
async def list_staff(principal: Reader, session: TenantSessionDep) -> list[StaffOut]:
    catalog = CatalogService(session, tenant_of(principal))
    return [
        _staff_out(s, await catalog.staff.service_ids(s.id)) for s in await catalog.staff.find_all()
    ]


@router.get("/staff/{staff_id}")
async def get_staff(staff_id: UUID, principal: Reader, session: TenantSessionDep) -> StaffOut:
    catalog = CatalogService(session, tenant_of(principal))
    staff = await catalog.get_staff(staff_id)
    return _staff_out(staff, await catalog.staff.service_ids(staff_id))


@router.patch("/staff/{staff_id}")
async def update_staff(
    staff_id: UUID, body: StaffUpdateIn, principal: Writer, session: TenantSessionDep
) -> StaffOut:
    catalog = CatalogService(session, tenant_of(principal))
    staff = await catalog.update_staff(principal, staff_id, body)
    service_ids = await catalog.staff.service_ids(staff_id)
    await session.commit()
    return _staff_out(staff, service_ids)


@router.put("/staff/{staff_id}/working-hours")
async def set_working_hours(
    staff_id: UUID, body: WorkingHoursIn, principal: Writer, session: TenantSessionDep
) -> list[WorkingHoursItem]:
    """Replaces the whole weekly schedule atomically (simpler and safer than per-row edits)."""
    rows = await CatalogService(session, tenant_of(principal)).set_working_hours(
        principal, staff_id, body.items
    )
    await session.commit()
    return [
        WorkingHoursItem(weekday=r.weekday, start_time=r.start_time, end_time=r.end_time)
        for r in rows
    ]


@router.get("/staff/{staff_id}/working-hours")
async def get_working_hours(
    staff_id: UUID, principal: Reader, session: TenantSessionDep
) -> list[WorkingHoursItem]:
    catalog = CatalogService(session, tenant_of(principal))
    await catalog.get_staff(staff_id)
    rows = await catalog.schedule.working_hours([staff_id])
    return [
        WorkingHoursItem(weekday=r.weekday, start_time=r.start_time, end_time=r.end_time)
        for r in rows
    ]


@router.post("/staff/{staff_id}/time-off", status_code=status.HTTP_201_CREATED)
async def add_time_off(
    staff_id: UUID, body: TimeOffIn, principal: Writer, session: TenantSessionDep
) -> TimeOffOut:
    row = await CatalogService(session, tenant_of(principal)).add_time_off(
        principal, staff_id, body.start, body.end, body.reason
    )
    await session.commit()
    return _time_off_out(row)


@router.get("/staff/{staff_id}/time-off")
async def list_time_off(
    staff_id: UUID,
    principal: Reader,
    session: TenantSessionDep,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
) -> list[TimeOffOut]:
    catalog = CatalogService(session, tenant_of(principal))
    await catalog.get_staff(staff_id)
    rows = await catalog.schedule.time_off(
        [staff_id], datetime.combine(start, time(0), UTC), datetime.combine(end, time(0), UTC)
    )
    return [_time_off_out(r) for r in rows]


@router.delete("/staff/{staff_id}/time-off/{time_off_id}", status_code=204)
async def delete_time_off(
    staff_id: UUID, time_off_id: UUID, principal: Writer, session: TenantSessionDep
) -> Response:
    await CatalogService(session, tenant_of(principal)).delete_time_off(
        principal, staff_id, time_off_id
    )
    await session.commit()
    return Response(status_code=204)


# --- availability -------------------------------------------------------------


@router.get("/availability")
async def availability(
    principal: Reader,
    session: TenantSessionDep,
    service_id: UUID,
    date: Annotated[date, Query(description="Local date in the tenant's timezone")],
    staff_id: UUID | None = None,
) -> list[SlotOut]:
    slots = await AvailabilityService(session, tenant_of(principal)).slots(
        service_id=service_id, day=date, staff_id=staff_id
    )
    return [SlotOut(staff_id=sid, start=s.start, end=s.end) for sid, s in slots]
