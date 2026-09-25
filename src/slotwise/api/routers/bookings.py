from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.api.deps import (
    DbDep,
    RedisDep,
    SettingsDep,
    TenantSessionDep,
    idempotency_context,
    require,
    tenant_of,
)
from slotwise.models import BookingStatus
from slotwise.schemas.bookings import (
    BookingCancelIn,
    BookingCreateIn,
    BookingOut,
    BookingRescheduleIn,
)
from slotwise.schemas.common import Page
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal
from slotwise.services.booking_events import EventedBookingService
from slotwise.services.bookings import BookingService
from slotwise.services.idempotency import run_idempotent
from slotwise.services.quotas import enforce_monthly_bookings

router = APIRouter(prefix="/v1/bookings", tags=["bookings"])

Reader = Annotated[Principal, Depends(require(P.BOOKINGS_READ))]
Writer = Annotated[Principal, Depends(require(P.BOOKINGS_WRITE))]


def service_for(session: AsyncSession, principal: Principal) -> BookingService:
    """The outbox-publishing variant, so every booking change emits a domain event."""
    return EventedBookingService(session, tenant_of(principal))


def _dump(booking: Any) -> dict[str, Any]:
    return BookingOut.model_validate(booking).model_dump(mode="json")


@router.post(
    "",
    status_code=201,
    response_model=BookingOut,
    responses={409: {"description": "slot_taken / idempotency_in_progress"}},
)
async def create_booking(
    body: BookingCreateIn,
    request: Request,
    principal: Writer,
    session: TenantSessionDep,
    db: DbDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> JSONResponse:
    """Create a booking. Requires an `Idempotency-Key` header (safe to retry)."""
    ctx = idempotency_context(
        request,
        db=db,
        redis=redis,
        settings=settings,
        principal=principal,
        body=body,
        required=True,
    )

    async def handler() -> tuple[int, dict[str, Any]]:
        await enforce_monthly_bookings(session, tenant_of(principal), principal.plan, settings)
        booking = await service_for(session, principal).create(
            principal,
            service_id=body.service_id,
            staff_id=body.staff_id,
            customer_id=body.customer_id,
            start=body.start,
            notes=body.notes,
        )
        return 201, _dump(booking)

    return await run_idempotent(ctx, session, handler)


@router.post("/{booking_id:uuid}:reschedule", response_model=BookingOut)
async def reschedule_booking(
    booking_id: UUID,
    body: BookingRescheduleIn,
    request: Request,
    principal: Writer,
    session: TenantSessionDep,
    db: DbDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> JSONResponse:
    ctx = idempotency_context(
        request,
        db=db,
        redis=redis,
        settings=settings,
        principal=principal,
        body=body,
        required=False,
    )

    async def handler() -> tuple[int, dict[str, Any]]:
        booking = await service_for(session, principal).reschedule(
            principal, booking_id, start=body.start, version=body.version
        )
        return 200, _dump(booking)

    return await run_idempotent(ctx, session, handler)


@router.post("/{booking_id:uuid}:cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: UUID,
    request: Request,
    principal: Writer,
    session: TenantSessionDep,
    db: DbDep,
    redis: RedisDep,
    settings: SettingsDep,
    body: BookingCancelIn | None = None,
) -> JSONResponse:
    body = body or BookingCancelIn()
    ctx = idempotency_context(
        request,
        db=db,
        redis=redis,
        settings=settings,
        principal=principal,
        body=body,
        required=False,
    )

    async def handler() -> tuple[int, dict[str, Any]]:
        booking = await service_for(session, principal).cancel(
            principal, booking_id, version=body.version, reason=body.reason
        )
        return 200, _dump(booking)

    return await run_idempotent(ctx, session, handler)


@router.get("/{booking_id:uuid}")
async def get_booking(booking_id: UUID, principal: Reader, session: TenantSessionDep) -> BookingOut:
    return BookingOut.model_validate(await service_for(session, principal).get(booking_id))


@router.get("")
async def list_bookings(
    principal: Reader,
    session: TenantSessionDep,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    staff_id: UUID | None = None,
    status: BookingStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[BookingOut]:
    rows, next_cursor = await service_for(session, principal).page(
        start=start, end=end, staff_id=staff_id, status=status, limit=limit, cursor=cursor
    )
    return Page(items=[BookingOut.model_validate(r) for r in rows], next_cursor=next_cursor)
