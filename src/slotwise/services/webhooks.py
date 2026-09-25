"""Tenant-facing webhook management: endpoints, delivery log, manual retry of dead letters."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.errors import NotFound, ValidationFailed
from slotwise.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from slotwise.repositories.audit import AuditRepository
from slotwise.security.principal import Principal
from slotwise.services.booking_events import EVENT_TYPES
from slotwise.webhooks import secrets
from slotwise.webhooks.ssrf import validate_target


class WebhookService:
    def __init__(self, session: AsyncSession, tenant_id: UUID, settings: Settings) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.settings = settings
        self.audit = AuditRepository(session, tenant_id)

    async def create_endpoint(
        self, principal: Principal, *, url: str, events: list[str]
    ) -> tuple[WebhookEndpoint, str]:
        unknown = set(events) - set(EVENT_TYPES)
        if unknown:
            raise ValidationFailed(f"unknown event types: {sorted(unknown)}")
        # Checked here for a fast, friendly error AND again at delivery time (DNS can change).
        await validate_target(url, allow_private=self.settings.webhook_allow_private_targets)
        secret = secrets.new_secret()
        endpoint = WebhookEndpoint(
            tenant_id=self.tenant_id,
            url=url,
            events=sorted(set(events)),
            secret_enc=secrets.encrypt(self.settings, secret),
        )
        self.session.add(endpoint)
        await self.session.flush()
        self.audit.record(
            principal,
            action="webhook.created",
            entity="webhook_endpoint",
            entity_id=endpoint.id,
            diff={"url": url, "events": endpoint.events},
        )
        return endpoint, secret

    async def get_endpoint(self, endpoint_id: UUID) -> WebhookEndpoint:
        endpoint = (
            await self.session.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.id == endpoint_id, WebhookEndpoint.tenant_id == self.tenant_id
                )
            )
        ).scalar_one_or_none()
        if endpoint is None:
            raise NotFound("webhook endpoint not found")
        return endpoint

    async def endpoints(self) -> list[WebhookEndpoint]:
        result = await self.session.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.tenant_id == self.tenant_id)
            .order_by(WebhookEndpoint.created_at)
        )
        return list(result.scalars())

    async def delete_endpoint(self, principal: Principal, endpoint_id: UUID) -> None:
        endpoint = await self.get_endpoint(endpoint_id)
        await self.session.delete(endpoint)
        self.audit.record(
            principal, action="webhook.deleted", entity="webhook_endpoint", entity_id=endpoint_id
        )

    async def enable_endpoint(self, principal: Principal, endpoint_id: UUID) -> WebhookEndpoint:
        """Close the circuit breaker by hand once the tenant has fixed their receiver."""
        endpoint = await self.get_endpoint(endpoint_id)
        endpoint.active, endpoint.consecutive_failures, endpoint.disabled_reason = True, 0, None
        self.audit.record(
            principal, action="webhook.enabled", entity="webhook_endpoint", entity_id=endpoint_id
        )
        return endpoint

    async def deliveries(self, status: DeliveryStatus | None, limit: int) -> list[WebhookDelivery]:
        stmt = select(WebhookDelivery).where(WebhookDelivery.tenant_id == self.tenant_id)
        if status:
            stmt = stmt.where(WebhookDelivery.status == status)
        stmt = stmt.order_by(WebhookDelivery.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def retry_delivery(self, principal: Principal, delivery_id: UUID) -> WebhookDelivery:
        delivery = (
            await self.session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.id == delivery_id, WebhookDelivery.tenant_id == self.tenant_id
                )
            )
        ).scalar_one_or_none()
        if delivery is None:
            raise NotFound("delivery not found")
        if delivery.status is DeliveryStatus.DELIVERED:
            raise ValidationFailed("delivery already succeeded")
        # Re-queue: the retry sweeper picks it up within one beat interval.
        delivery.status, delivery.attempt = DeliveryStatus.PENDING, 0
        delivery.next_retry_at = datetime.now(UTC)
        self.audit.record(
            principal,
            action="webhook.delivery_retried",
            entity="webhook_delivery",
            entity_id=delivery_id,
        )
        return delivery
