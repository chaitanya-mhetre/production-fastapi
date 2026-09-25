"""Transactional outbox, write side.

The dual-write problem: "commit the booking, then publish the event" loses the event if the
process dies between the two steps, and "publish, then commit" announces bookings that never
existed if the commit fails. The fix: write the event as a row in the SAME transaction as the
business change. Either both commit or neither does. A separate relay publishes the rows later.
"""

from typing import Any
from uuid import UUID

from opentelemetry import propagate
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.models import OutboxEvent


def emit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: object,
    event_type: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    # Save the current trace context (W3C traceparent) with the event, so the worker that handles
    # it later can continue the SAME trace: API request → outbox → webhook delivery, end to end.
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    event = OutboxEvent(
        trace_context=carrier,
        tenant_id=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    return event
