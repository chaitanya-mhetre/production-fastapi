import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import ValidationError

from slotwise.api.deps import DbDep, SettingsDep, TenantSessionDep, require, tenant_of
from slotwise.errors import ValidationFailed
from slotwise.models import DeliveryStatus
from slotwise.schemas.webhooks import (
    MockpayEventIn,
    WebhookDeliveryOut,
    WebhookEndpointCreatedOut,
    WebhookEndpointIn,
    WebhookEndpointOut,
)
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal
from slotwise.services.payments import PaymentService
from slotwise.services.webhooks import WebhookService
from slotwise.webhooks.signing import verify

router = APIRouter(prefix="/v1", tags=["webhooks"])

Admin = Annotated[Principal, Depends(require(P.WEBHOOKS_MANAGE))]


@router.post("/webhook-endpoints", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: WebhookEndpointIn, principal: Admin, session: TenantSessionDep, settings: SettingsDep
) -> WebhookEndpointCreatedOut:
    endpoint, secret = await WebhookService(
        session, tenant_of(principal), settings
    ).create_endpoint(principal, url=str(body.url), events=body.events)
    await session.commit()
    return WebhookEndpointCreatedOut.model_validate(
        {**WebhookEndpointOut.model_validate(endpoint).model_dump(), "secret": secret}
    )


@router.get("/webhook-endpoints")
async def list_endpoints(
    principal: Admin, session: TenantSessionDep, settings: SettingsDep
) -> list[WebhookEndpointOut]:
    service = WebhookService(session, tenant_of(principal), settings)
    return [WebhookEndpointOut.model_validate(e) for e in await service.endpoints()]


@router.delete("/webhook-endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: UUID, principal: Admin, session: TenantSessionDep, settings: SettingsDep
) -> Response:
    await WebhookService(session, tenant_of(principal), settings).delete_endpoint(
        principal, endpoint_id
    )
    await session.commit()
    return Response(status_code=204)


@router.post("/webhook-endpoints/{endpoint_id:uuid}:enable")
async def enable_endpoint(
    endpoint_id: UUID, principal: Admin, session: TenantSessionDep, settings: SettingsDep
) -> WebhookEndpointOut:
    endpoint = await WebhookService(session, tenant_of(principal), settings).enable_endpoint(
        principal, endpoint_id
    )
    await session.commit()
    return WebhookEndpointOut.model_validate(endpoint)


@router.get("/webhook-deliveries")
async def list_deliveries(
    principal: Admin,
    session: TenantSessionDep,
    settings: SettingsDep,
    status: DeliveryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WebhookDeliveryOut]:
    rows = await WebhookService(session, tenant_of(principal), settings).deliveries(status, limit)
    return [WebhookDeliveryOut.model_validate(r) for r in rows]


@router.post("/webhook-deliveries/{delivery_id:uuid}:retry")
async def retry_delivery(
    delivery_id: UUID, principal: Admin, session: TenantSessionDep, settings: SettingsDep
) -> WebhookDeliveryOut:
    delivery = await WebhookService(session, tenant_of(principal), settings).retry_delivery(
        principal, delivery_id
    )
    await session.commit()
    return WebhookDeliveryOut.model_validate(delivery)


@router.post("/webhooks/payments/mockpay", tags=["inbound"])
async def mockpay_webhook(request: Request, db: DbDep, settings: SettingsDep) -> dict[str, Any]:
    """Inbound provider webhook. Authenticated by HMAC signature, not by a user token.

    The signature is verified over the RAW bytes before anything is parsed: re-serialising the
    JSON could change whitespace or key order and break (or worse, bypass) verification.
    """
    raw = await request.body()
    verify(
        settings.mockpay_webhook_secret,
        request.headers.get("mockpay-signature"),
        raw,
        tolerance_seconds=settings.webhook_timestamp_tolerance_seconds,
    )
    try:
        payload: dict[str, Any] = json.loads(raw)
        event = MockpayEventIn.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise ValidationFailed("invalid payment event payload") from exc
    async with db.session(tenant_id=event.tenant_id) as session:
        result = await PaymentService(session, event.tenant_id).handle_mockpay(event, payload)
        await session.commit()
    return result
