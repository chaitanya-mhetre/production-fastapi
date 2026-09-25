"""Worker logic as plain async functions.

Celery tasks (worker/tasks.py) are thin wrappers around these, which keeps the logic testable
without a broker: tests call these directly with a fake mailer and respx-mocked HTTP.

Everything here runs as the `slotwise_worker` DB role (BYPASSRLS), because queues span tenants.
Every query still filters by tenant explicitly where it matters.
"""

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from opentelemetry import propagate, trace
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.db import Database
from slotwise.models import (
    Booking,
    BookingStatus,
    ConsumerInbox,
    Customer,
    DeliveryStatus,
    IdempotencyKey,
    OutboxEvent,
    Service,
    Tenant,
    WebhookDelivery,
    WebhookEndpoint,
)
from slotwise.notifications.mailer import Mailer
from slotwise.notifications.templates import booking_email
from slotwise.observability import metrics
from slotwise.services.booking_events import EventedBookingService
from slotwise.webhooks import secrets as webhook_secrets
from slotwise.webhooks.signing import sign
from slotwise.webhooks.ssrf import UnsafeWebhookTarget, validate_target

log = logging.getLogger("slotwise.worker")
tracer = trace.get_tracer("slotwise.worker")

MAX_ATTEMPTS = 8  # ~30s, 1m, 2m, 4m, 8m, 16m, 32m, 64m (with jitter) → then dead-letter
BREAKER_THRESHOLD = 10  # consecutive failures before an endpoint is disabled
DELIVERY_LEASE = timedelta(seconds=60)
DELIVERY_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
EMAIL_EVENTS = ("booking.created", "booking.confirmed", "booking.rescheduled", "booking.cancelled")


@dataclass
class WorkerDeps:
    db: Database
    http: httpx.AsyncClient
    settings: Settings
    mailer: Mailer
    rng: random.Random = field(default_factory=random.Random)


def backoff(attempt: int, rng: random.Random) -> timedelta:
    """Exponential backoff with jitter. Jitter spreads retries out, so a recovering endpoint isn't
    hit by every queued delivery at the same instant (the thundering herd)."""
    base = min(30 * 2 ** (attempt - 1), 6 * 3600)
    return timedelta(seconds=base * rng.uniform(0.5, 1.0))


# --- fan-out -----------------------------------------------------------------------------------


async def dispatch_event(deps: WorkerDeps, event_id: int) -> list[UUID]:
    """Fan an outbox event out to subscribed webhook endpoints and internal consumers.

    Safe to run more than once for the same event (the relay is at-least-once):
    UNIQUE(endpoint_id, outbox_event_id) stops duplicate deliveries, and the inbox stops
    duplicate emails.
    """
    async with deps.db.session() as session:
        carrier = (
            await session.execute(
                select(OutboxEvent.trace_context).where(OutboxEvent.id == event_id)
            )
        ).scalar_one_or_none()
    parent = propagate.extract(carrier or {})
    with tracer.start_as_current_span(
        "outbox.dispatch", context=parent, attributes={"outbox.event_id": event_id}
    ):
        return await _dispatch(deps, event_id)


async def _dispatch(deps: WorkerDeps, event_id: int) -> list[UUID]:
    async with deps.db.session() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        if event is None:
            return []
        endpoints = (
            await session.execute(
                select(WebhookEndpoint.id).where(
                    WebhookEndpoint.tenant_id == event.tenant_id,
                    WebhookEndpoint.active,
                    WebhookEndpoint.events.contains([event.event_type]),
                )
            )
        ).scalars()
        created: list[UUID] = []
        for endpoint_id in endpoints:
            row = await session.execute(
                insert(WebhookDelivery)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=event.tenant_id,
                    endpoint_id=endpoint_id,
                    outbox_event_id=event.id,
                    event_type=event.event_type,
                    next_retry_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["endpoint_id", "outbox_event_id"])
                .returning(WebhookDelivery.id)
            )
            if (delivery_id := row.scalar_one_or_none()) is not None:
                created.append(delivery_id)
        event_type = event.event_type

    for delivery_id in created:
        await deliver(deps, delivery_id)
    if event_type in EMAIL_EVENTS:
        await notify_customer(deps, event_id)
    return created


# --- webhook delivery ---------------------------------------------------------------------------


async def deliver(deps: WorkerDeps, delivery_id: UUID) -> DeliveryStatus | None:
    """One delivery attempt. Returns the resulting status, or None if another worker holds it.

    Lease, not lock: we claim the row by pushing next_retry_at forward and COMMIT, then make the
    HTTP call *outside* any transaction. Holding a row lock across a 10-second network call would
    pin a DB connection and a transaction per in-flight webhook. If this worker dies mid-call,
    the lease simply expires and the retry sweeper picks the delivery up again.
    """
    now = datetime.now(UTC)
    async with deps.db.session() as session, session.begin():
        claimed = (
            await session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.status == DeliveryStatus.PENDING,
                    WebhookDelivery.next_retry_at <= now,
                )
                .values(next_retry_at=now + DELIVERY_LEASE)
                .returning(WebhookDelivery.endpoint_id, WebhookDelivery.outbox_event_id)
            )
        ).one_or_none()
        if claimed is None:
            return None
        endpoint = await session.get(WebhookEndpoint, claimed[0])
        event = await session.get(OutboxEvent, claimed[1])
        assert endpoint is not None and event is not None
        if not endpoint.active:
            await _finish(session, delivery_id, DeliveryStatus.DEAD, error="endpoint disabled")
            return DeliveryStatus.DEAD
        url, secret = endpoint.url, webhook_secrets.decrypt(deps.settings, endpoint.secret_enc)
        body = json.dumps(
            {
                "id": f"evt_{event.id}",
                "type": event.event_type,
                "created_at": event.created_at.isoformat(),
                "tenant_id": str(event.tenant_id),
                "data": event.payload,
            },
            separators=(",", ":"),
        ).encode()
        event_ref = f"evt_{event.id}"

    status_code: int | None = None
    error: str | None = None
    started = time.perf_counter()
    try:
        await validate_target(url, allow_private=deps.settings.webhook_allow_private_targets)
        response = await deps.http.post(
            url,
            content=body,
            timeout=DELIVERY_TIMEOUT,
            follow_redirects=False,  # a redirect could bounce us to an internal address
            headers={
                "Content-Type": "application/json",
                "Slotwise-Signature": sign(secret, body),
                "Slotwise-Event-Id": event_ref,
                "User-Agent": "Slotwise-Webhooks/1.0",
            },
        )
        status_code = response.status_code
        if not 200 <= status_code < 300:
            error = f"HTTP {status_code}"
    except UnsafeWebhookTarget as exc:
        error = exc.message
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    async with deps.db.session() as session, session.begin():
        delivery = await session.get(WebhookDelivery, delivery_id, with_for_update=True)
        endpoint = await session.get(WebhookEndpoint, claimed[0], with_for_update=True)
        assert delivery is not None and endpoint is not None
        delivery.attempt += 1
        delivery.status_code, delivery.response_ms = status_code, elapsed_ms
        if error is None:
            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = datetime.now(UTC)
            delivery.next_retry_at = None
            delivery.last_error = None
            endpoint.consecutive_failures = 0
            metrics.WEBHOOK_DELIVERIES.labels("delivered").inc()
            return DeliveryStatus.DELIVERED

        delivery.last_error = error
        endpoint.consecutive_failures += 1
        if endpoint.consecutive_failures >= BREAKER_THRESHOLD and endpoint.active:
            # Circuit breaker: stop hammering a dead endpoint (and wasting worker time).
            endpoint.active = False
            endpoint.disabled_reason = f"disabled after {BREAKER_THRESHOLD} consecutive failures"
            log.warning("webhook endpoint disabled", extra={"endpoint_id": str(endpoint.id)})
        if delivery.attempt >= MAX_ATTEMPTS:
            delivery.status = DeliveryStatus.DEAD  # dead-letter: visible via API, retry by hand
            delivery.next_retry_at = None
            metrics.WEBHOOK_DELIVERIES.labels("dead").inc()
            return DeliveryStatus.DEAD
        delivery.next_retry_at = datetime.now(UTC) + backoff(delivery.attempt, deps.rng)
        metrics.WEBHOOK_DELIVERIES.labels("retry").inc()
        return DeliveryStatus.PENDING


async def _finish(
    session: AsyncSession, delivery_id: UUID, status: DeliveryStatus, *, error: str
) -> None:
    await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id)
        .values(status=status, last_error=error, next_retry_at=None)
    )


async def retry_due_deliveries(deps: WorkerDeps, limit: int = 100) -> int:
    """Beat task: attempt every pending delivery whose retry time (or expired lease) has come."""
    async with deps.db.session() as session:
        due = list(
            (
                await session.execute(
                    select(WebhookDelivery.id)
                    .where(
                        WebhookDelivery.status == DeliveryStatus.PENDING,
                        WebhookDelivery.next_retry_at <= datetime.now(UTC),
                    )
                    .order_by(WebhookDelivery.next_retry_at)
                    .limit(limit)
                )
            ).scalars()
        )
    attempted = 0
    for delivery_id in due:
        if await deliver(deps, delivery_id) is not None:
            attempted += 1
    return attempted


# --- internal consumers ----------------------------------------------------------------------


async def _claim_inbox(session: AsyncSession, consumer: str, event_id: int) -> bool:
    row = await session.execute(
        insert(ConsumerInbox)
        .values(consumer=consumer, event_id=event_id)
        .on_conflict_do_nothing()
        .returning(ConsumerInbox.event_id)
    )
    return row.scalar_one_or_none() is not None


async def notify_customer(deps: WorkerDeps, event_id: int) -> bool:
    """Email the customer about a booking change, at most once per event.

    The inbox row and the send share a transaction: if SMTP fails, the transaction rolls back and
    the task retries. A crash after a successful send but before COMMIT can still send twice.
    That's the honest at-least-once trade-off; exactly-once email delivery isn't achievable.
    """
    async with deps.db.session() as session, session.begin():
        if not await _claim_inbox(session, "email.customer", event_id):
            return False
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        data: dict[str, Any] = event.payload
        customer = await session.get(Customer, UUID(data["customer_id"]))
        if customer is None or not customer.email:
            return False
        tenant = await session.get(Tenant, event.tenant_id)
        service = await session.get(Service, UUID(data["service_id"]))
        assert tenant is not None and service is not None
        await deps.mailer.send(
            booking_email(
                event.event_type,
                to=customer.email,
                customer=customer.name,
                tenant=tenant.name,
                tz=tenant.timezone,
                service=service.name,
                starts_at=datetime.fromisoformat(data["starts_at"]),
            )
        )
        return True


async def send_due_reminders(
    deps: WorkerDeps, now: datetime | None = None, lead: timedelta = timedelta(hours=24)
) -> int:
    """Beat task: remind customers of bookings starting within `lead`.

    Why a periodic scan instead of a Celery task with eta=start-24h: ETA tasks sit in the broker
    for days (with Redis they're re-delivered once past the visibility timeout), they go stale
    when a booking is rescheduled or cancelled, and they're invisible to the database. A scan
    over an indexed `reminder_sent_at IS NULL` column is simple, restart-safe and always current.
    """
    now = now or datetime.now(UTC)
    sent = 0
    async with deps.db.session() as session:
        candidates = list(
            (
                await session.execute(
                    select(Booking.id)
                    .where(
                        Booking.reminder_sent_at.is_(None),
                        Booking.status.in_([BookingStatus.CONFIRMED]),
                        Booking.starts_at > now,
                        Booking.starts_at <= now + lead,
                    )
                    .limit(500)
                )
            ).scalars()
        )
    for booking_id in candidates:
        async with deps.db.session() as session, session.begin():
            booking = (
                await session.execute(
                    select(Booking)
                    .where(Booking.id == booking_id, Booking.reminder_sent_at.is_(None))
                    .with_for_update(skip_locked=True)  # another worker already has it
                )
            ).scalar_one_or_none()
            if booking is None:
                continue
            customer = await session.get(Customer, booking.customer_id)
            tenant = await session.get(Tenant, booking.tenant_id)
            service = await session.get(Service, booking.service_id)
            assert tenant is not None and service is not None
            if customer is not None and customer.email:
                await deps.mailer.send(
                    booking_email(
                        "reminder",
                        to=customer.email,
                        customer=customer.name,
                        tenant=tenant.name,
                        tz=tenant.timezone,
                        service=service.name,
                        starts_at=booking.starts_at,
                    )
                )
                sent += 1
            booking.reminder_sent_at = now
    return sent


async def expire_unpaid_bookings(
    deps: WorkerDeps, now: datetime | None = None, ttl: timedelta = timedelta(minutes=15)
) -> int:
    """Beat task: release slots held by bookings nobody paid for."""
    now = now or datetime.now(UTC)
    async with deps.db.session() as session:
        stale = (
            await session.execute(
                select(Booking.id, Booking.tenant_id).where(
                    Booking.status == BookingStatus.PENDING_PAYMENT,
                    Booking.created_at < now - ttl,
                )
            )
        ).all()
    for booking_id, tenant_id in stale:
        async with deps.db.session() as session, session.begin():
            await EventedBookingService(session, tenant_id).cancel(
                None, booking_id, reason="payment_timeout"
            )
    return len(stale)


async def purge_idempotency_keys(deps: WorkerDeps, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=deps.settings.idempotency_ttl_seconds)
    async with deps.db.session() as session, session.begin():
        result = await session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
        )
    return int(result.rowcount)  # type: ignore[attr-defined]
