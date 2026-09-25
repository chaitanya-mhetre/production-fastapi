"""Plan quotas: a hard monthly cap on bookings per tenant, by plan.

Counted from the database (bookings created this calendar month, UTC), backed by the
(tenant_id, created_at) index. It isn't atomic with the insert, so two racing requests at the
boundary can exceed the cap by a booking or two. That's acceptable for a billing soft-wall;
a strict cap would need a counter row updated with SELECT ... FOR UPDATE in the same transaction.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.errors import QuotaExceeded
from slotwise.models import Booking, TenantPlan


def monthly_booking_limit(plan: TenantPlan | None, settings: Settings) -> int:
    return (
        settings.monthly_bookings_free if plan is TenantPlan.FREE else settings.monthly_bookings_pro
    )


async def enforce_monthly_bookings(
    session: AsyncSession,
    tenant_id: UUID,
    plan: TenantPlan | None,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    limit = monthly_booking_limit(plan, settings)
    used = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.tenant_id == tenant_id, Booking.created_at >= month_start)
        )
    ).scalar_one()
    if used >= limit:
        next_month = month_start.replace(
            month=month_start.month % 12 + 1, year=month_start.year + month_start.month // 12
        )
        raise QuotaExceeded(
            f"monthly booking quota of {limit} reached for the {plan or 'current'} plan",
            retry_after=int((next_month - now).total_seconds()),
            limit=limit,
        )
