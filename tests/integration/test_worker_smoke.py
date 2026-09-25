"""Smoke test through the real Celery task wrapper and real SMTP (Mailpit), no fakes."""

import asyncio
import socket
import uuid

import httpx
import pytest
from sqlalchemy import select

from slotwise.db import Database
from slotwise.models import OutboxEvent
from tests.integration.conftest import MakeCatalog, MakeTenant

MAILPIT_API = "http://localhost:58025/api/v1"


def _mailpit_up() -> bool:
    try:
        with socket.create_connection(("localhost", 58025), 1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _mailpit_up(), reason="Mailpit not running on 58025 (docker compose up -d mailpit)"
)
async def test_dispatch_task_sends_real_email(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog, db: Database
) -> None:
    from slotwise.worker.tasks import dispatch_event

    async with httpx.AsyncClient() as mailpit:
        await mailpit.delete(f"{MAILPIT_API}/messages")

        t = await make_tenant("acme")
        c = await make_catalog(t)
        address = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
        cust = (
            await client.post(
                "/v1/customers",
                headers=t.headers,
                json={"name": "Smoke", "phone": "9000000099", "email": address},
            )
        ).json()
        await client.post(
            "/v1/bookings",
            headers=t.headers | {"Idempotency-Key": "smoke-test-key"},
            json={
                "service_id": c.service_id,
                "staff_id": c.staff_ids[0],
                "customer_id": cust["id"],
                "start": "2030-01-07T10:00:00+05:30",
            },
        )
        async with db.session(tenant_id=uuid.UUID(t.id)) as s:
            event_id = (await s.execute(select(OutboxEvent.id))).scalar_one()

        # .apply() runs the task body synchronously (no broker), exactly as a worker would.
        # to_thread gives the wrapper's asyncio.run() a thread without a running loop.
        result = await asyncio.to_thread(dispatch_event.apply, args=(event_id,))
        assert result.successful(), result.traceback

        messages = (await mailpit.get(f"{MAILPIT_API}/messages")).json()["messages"]
        assert [m["To"][0]["Address"] for m in messages] == [address]
        assert "received" in messages[0]["Subject"]
