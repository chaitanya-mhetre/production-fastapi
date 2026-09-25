"""M5 acceptance: outbox, relay, webhook delivery, retries/DLQ/breaker, emails, payments."""

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, text

from slotwise.config import Settings
from slotwise.db import Database
from slotwise.models import DeliveryStatus, OutboxEvent, WebhookDelivery, WebhookEndpoint
from slotwise.notifications.mailer import RecordingMailer
from slotwise.outbox.relay import relay_batch
from slotwise.webhooks.signing import sign, verify
from slotwise.worker import core
from tests.integration.conftest import CatalogCtx, MakeCatalog, MakeTenant, TenantCtx

HOOK_URL = "http://hooks.example.com/slotwise"
SLOT = "2030-01-07T10:00:00+05:30"


@pytest.fixture
async def worker_db(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings.worker_database_url, settings, pooled=False)
    yield db
    await db.dispose()


@pytest.fixture
async def deps(worker_db: Database, settings: Settings) -> AsyncIterator[core.WorkerDeps]:
    async with httpx.AsyncClient() as http:
        yield core.WorkerDeps(
            db=worker_db,
            http=http,
            settings=settings,
            mailer=RecordingMailer(),
            rng=random.Random(42),
        )


async def _booking_world(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> tuple[TenantCtx, CatalogCtx, str]:
    t = await make_tenant("acme")
    c = await make_catalog(t)
    cust = await client.post(
        "/v1/customers",
        headers=t.headers,
        json={"name": "Asha", "phone": "9876543210", "email": "asha@example.com"},
    )
    return t, c, cust.json()["id"]


async def _book(
    client: httpx.AsyncClient, t: TenantCtx, c: CatalogCtx, cust: str, start: str = SLOT
) -> dict[str, Any]:
    resp = await client.post(
        "/v1/bookings",
        headers=t.headers | {"Idempotency-Key": f"evt-{uuid.uuid4()}"},
        json={
            "service_id": c.service_id,
            "staff_id": c.staff_ids[0],
            "customer_id": cust,
            "start": start,
        },
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def _endpoint(
    client: httpx.AsyncClient, t: TenantCtx, events: list[str] | None = None
) -> dict[str, Any]:
    resp = await client.post(
        "/v1/webhook-endpoints",
        headers=t.headers,
        json={"url": HOOK_URL, "events": events or ["booking.created", "booking.cancelled"]},
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def _events(db: Database) -> list[OutboxEvent]:
    async with db.session() as s:
        return list((await s.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars())


# --- outbox + relay -------------------------------------------------------------------------


async def test_booking_and_event_commit_together(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    worker_db: Database,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    b = await _book(client, t, c, cust)
    lost = await client.post(  # same slot → 409: its transaction rolls back, so no event either
        "/v1/bookings",
        headers=t.headers | {"Idempotency-Key": "evt-lost-race"},
        json={
            "service_id": c.service_id,
            "staff_id": c.staff_ids[0],
            "customer_id": cust,
            "start": SLOT,
        },
    )
    assert lost.status_code == 409
    events = await _events(worker_db)
    assert [(e.event_type, e.aggregate_id) for e in events] == [("booking.created", b["id"])]
    assert events[0].published_at is None


async def test_relay_publishes_and_marks(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    worker_db: Database,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    await _book(client, t, c, cust)
    await _book(client, t, c, cust, "2030-01-07T12:00:00+05:30")
    published: list[int] = []
    assert await relay_batch(worker_db, lambda e: published.append(e.id)) == 2
    assert await relay_batch(worker_db, lambda e: published.append(e.id)) == 0
    assert len(published) == 2 and all(e.published_at for e in await _events(worker_db))


async def test_relay_crash_mid_batch_loses_nothing(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    worker_db: Database,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    for hour in (10, 11, 12):
        await _book(client, t, c, cust, f"2030-01-07T{hour}:00:00+05:30")
    sent: list[int] = []

    def flaky(event: OutboxEvent) -> None:
        if len(sent) == 1:
            raise ConnectionError("broker went away")
        sent.append(event.id)

    with pytest.raises(ConnectionError):
        await relay_batch(worker_db, flaky)
    assert all(e.published_at is None for e in await _events(worker_db))  # rolled back

    retried: list[int] = []
    assert await relay_batch(worker_db, lambda e: retried.append(e.id)) == 3
    # The first event went out twice (at-least-once); consumers dedupe on the event id.
    assert set(retried) == {e.id for e in await _events(worker_db)}


async def test_concurrent_relays_never_double_publish(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    worker_db: Database,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    for hour in range(9, 16):
        await _book(client, t, c, cust, f"2030-01-07T{hour:02d}:00:00+05:30")
    published: list[int] = []

    def slow(event: OutboxEvent) -> None:
        published.append(event.id)
        time.sleep(0.01)  # widen the window where both relays are mid-batch

    await asyncio.gather(*[relay_batch(worker_db, slow, batch_size=2) for _ in range(6)])
    assert len(published) == len(set(published)) == 7


# --- webhook delivery -------------------------------------------------------------------------


async def _dispatch_all(deps: core.WorkerDeps) -> None:
    for event in await _events(deps.db):
        await core.dispatch_event(deps, event.id)


@respx.mock
async def test_signed_webhook_is_delivered_once(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
    settings: Settings,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    endpoint = await _endpoint(client, t)
    route = respx.post(HOOK_URL).mock(return_value=httpx.Response(200))
    b = await _book(client, t, c, cust)

    await _dispatch_all(deps)
    await _dispatch_all(deps)  # duplicate dispatch (relay re-sent the event)
    assert route.call_count == 1

    request = route.calls[0].request
    verify(
        endpoint["secret"],
        request.headers["Slotwise-Signature"],
        request.content,
        tolerance_seconds=300,
    )
    payload = json.loads(request.content)
    assert payload["type"] == "booking.created" and payload["data"]["id"] == b["id"]

    deliveries = (await client.get("/v1/webhook-deliveries", headers=t.headers)).json()
    assert [d["status"] for d in deliveries] == ["delivered"]


@respx.mock
async def test_unsubscribed_events_are_not_sent(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    await _endpoint(client, t, events=["booking.cancelled"])
    route = respx.post(HOOK_URL).mock(return_value=httpx.Response(200))
    await _book(client, t, c, cust)
    await _dispatch_all(deps)
    assert route.call_count == 0


@respx.mock
async def test_failures_back_off_then_dead_letter(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    await _endpoint(client, t)
    respx.post(HOOK_URL).mock(return_value=httpx.Response(503))
    await _book(client, t, c, cust)
    await _dispatch_all(deps)

    async with deps.db.session() as s:
        d = (await s.execute(select(WebhookDelivery))).scalar_one()
    assert d.status is DeliveryStatus.PENDING and d.attempt == 1 and d.last_error == "HTTP 503"
    assert d.next_retry_at is not None and d.next_retry_at > datetime.now(UTC) + timedelta(
        seconds=10
    )

    for _ in range(core.MAX_ATTEMPTS - 1):  # fast-forward: make it due, run the sweeper
        async with deps.db.session() as s:
            await s.execute(
                text("UPDATE webhook_deliveries SET next_retry_at = now() - interval '1s'")
            )
            await s.commit()
        await core.retry_due_deliveries(deps)

    dead = (
        await client.get("/v1/webhook-deliveries", headers=t.headers, params={"status": "dead"})
    ).json()
    assert len(dead) == 1 and dead[0]["attempt"] == core.MAX_ATTEMPTS

    retried = await client.post(f"/v1/webhook-deliveries/{dead[0]['id']}:retry", headers=t.headers)
    assert retried.json()["status"] == "pending" and retried.json()["attempt"] == 0


@respx.mock
async def test_circuit_breaker_disables_failing_endpoint(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    endpoint = await _endpoint(client, t)
    respx.post(HOOK_URL).mock(side_effect=httpx.ConnectError("refused"))
    for i in range(core.BREAKER_THRESHOLD):
        await _book(client, t, c, cust, f"2030-01-{8 + i // 7:02d}T{9 + i % 7:02d}:00:00+05:30")
    await _dispatch_all(deps)

    listed = (await client.get("/v1/webhook-endpoints", headers=t.headers)).json()
    assert listed[0]["active"] is False and "consecutive failures" in listed[0]["disabled_reason"]

    enabled = await client.post(f"/v1/webhook-endpoints/{endpoint['id']}:enable", headers=t.headers)
    assert enabled.json()["active"] is True and enabled.json()["consecutive_failures"] == 0


async def test_ssrf_targets_rejected(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "webhook_allow_private_targets", False)
    t = await make_tenant("acme")
    for url in (
        "http://127.0.0.1:5432/",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/",
        "http://user:pw@example.com/",
        "http://10.0.0.5/",
    ):
        resp = await client.post(
            "/v1/webhook-endpoints",
            headers=t.headers,
            json={"url": url, "events": ["booking.created"]},
        )
        assert resp.status_code == 422, url


async def test_webhook_secret_is_encrypted_at_rest(
    client: httpx.AsyncClient, make_tenant: MakeTenant, worker_db: Database
) -> None:
    t = await make_tenant("acme")
    endpoint = await _endpoint(client, t)
    async with worker_db.session() as s:
        stored = (await s.execute(select(WebhookEndpoint.secret_enc))).scalar_one()
    assert endpoint["secret"] not in stored and endpoint["secret"].startswith("whsec_")


# --- emails, reminders, expiry ------------------------------------------------------------------


async def test_customer_email_sent_once_per_event(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    await _book(client, t, c, cust)
    await _dispatch_all(deps)
    await _dispatch_all(deps)
    mailer = deps.mailer
    assert isinstance(mailer, RecordingMailer)
    assert len(mailer.sent) == 1
    email = mailer.sent[0]
    assert email.to == "asha@example.com" and "Mon 07 Jan 2030, 10:00" in email.body


async def test_reminders_sent_once_for_confirmed_bookings(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    b = await _book(client, t, c, cust)
    async with deps.db.session() as s:
        await s.execute(text("UPDATE bookings SET status = 'confirmed'"))
        await s.commit()
    starts = datetime.fromisoformat(b["starts_at"])
    assert await core.send_due_reminders(deps, now=starts - timedelta(hours=30)) == 0  # too early
    assert await core.send_due_reminders(deps, now=starts - timedelta(hours=2)) == 1
    assert await core.send_due_reminders(deps, now=starts - timedelta(hours=1)) == 0  # already sent


async def test_unpaid_bookings_expire_and_free_the_slot(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    deps: core.WorkerDeps,
    worker_db: Database,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    b = await _book(client, t, c, cust)
    assert await core.expire_unpaid_bookings(deps) == 0
    assert (
        await core.expire_unpaid_bookings(deps, now=datetime.now(UTC) + timedelta(minutes=16)) == 1
    )
    got = (await client.get(f"/v1/bookings/{b['id']}", headers=t.headers)).json()
    assert got["status"] == "cancelled"
    assert [e.event_type for e in await _events(worker_db)] == [
        "booking.created",
        "booking.cancelled",
    ]
    await _book(client, t, c, cust)  # the slot is free again


# --- inbound payments ------------------------------------------------------------------------


def _mockpay(
    settings: Settings, payload: dict[str, Any], ts: int | None = None
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    return raw, {
        "Mockpay-Signature": sign(settings.mockpay_webhook_secret, raw, ts),
        "Content-Type": "application/json",
    }


async def test_payment_webhook_confirms_booking_once(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    settings: Settings,
    worker_db: Database,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    b = await _book(client, t, c, cust)
    payload = {
        "provider_ref": "pay_123",
        "tenant_id": t.id,
        "booking_id": b["id"],
        "amount_paise": b["price_paise"],
        "status": "succeeded",
    }
    raw, headers = _mockpay(settings, payload)
    first = await client.post("/v1/webhooks/payments/mockpay", content=raw, headers=headers)
    dup = await client.post("/v1/webhooks/payments/mockpay", content=raw, headers=headers)
    assert first.json() == {"status": "succeeded", "booking_status": "confirmed"}
    assert dup.json() == {"status": "duplicate"}
    assert [e.event_type for e in await _events(worker_db)] == [
        "booking.created",
        "booking.confirmed",
    ]


async def test_payment_webhook_rejects_bad_signatures(
    client: httpx.AsyncClient, make_tenant: MakeTenant, settings: Settings
) -> None:
    t = await make_tenant("acme")
    payload = {
        "provider_ref": "pay_x",
        "tenant_id": t.id,
        "booking_id": str(uuid.uuid4()),
        "amount_paise": 1,
        "status": "succeeded",
    }
    raw, headers = _mockpay(settings, payload)
    tampered = raw.replace(b'"amount_paise": 1', b'"amount_paise": 0')
    stale_raw, stale = _mockpay(settings, payload, ts=int(time.time()) - 3600)
    for body, hdrs in (
        (tampered, headers),
        (stale_raw, stale),
        (raw, {"Content-Type": "application/json"}),
    ):
        assert (
            await client.post("/v1/webhooks/payments/mockpay", content=body, headers=hdrs)
        ).status_code == 401


async def test_payment_amount_mismatch_does_not_confirm(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    settings: Settings,
) -> None:
    t, c, cust = await _booking_world(client, make_tenant, make_catalog)
    b = await _book(client, t, c, cust)
    raw, headers = _mockpay(
        settings,
        {
            "provider_ref": "pay_cheap",
            "tenant_id": t.id,
            "booking_id": b["id"],
            "amount_paise": 1,
            "status": "succeeded",
        },
    )
    resp = await client.post("/v1/webhooks/payments/mockpay", content=raw, headers=headers)
    assert resp.json()["status"] == "amount_mismatch"
    assert (await client.get(f"/v1/bookings/{b['id']}", headers=t.headers)).json()[
        "status"
    ] == "pending_payment"


# --- uploads -------------------------------------------------------------------------------------


async def test_presigned_upload_is_scoped_to_tenant(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    resp = await client.post(
        "/v1/uploads:presign",
        headers=t.headers,
        json={"kind": "tenant_logo", "content_type": "image/png"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"].startswith(f"{t.id}/tenant_logo/") and body["key"].endswith(".png")
    assert body["fields"]["Content-Type"] == "image/png" and "policy" in body["fields"]
    bad = await client.post(
        "/v1/uploads:presign",
        headers=t.headers,
        json={"kind": "tenant_logo", "content_type": "application/x-sh"},
    )
    assert bad.status_code == 422
