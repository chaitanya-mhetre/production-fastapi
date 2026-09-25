"""M6 acceptance: metrics exposed, one request = one trace across API and DB, ids in audit."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text

from slotwise.config import Settings
from slotwise.main import create_app
from tests.integration.conftest import MakeTenant


async def test_metrics_endpoint_exposes_red_and_business_metrics(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    await client.get("/v1/tenant", headers=t.headers)
    body = (await client.get("/metrics")).text
    assert 'http_requests_total{method="GET",route="/v1/tenant",status="200"}' in body
    assert "http_request_duration_seconds_bucket" in body
    for name in ("bookings_created_total", "outbox_unpublished_count", "webhook_delivery_total"):
        assert name in body


async def test_route_label_is_the_template_not_the_raw_path(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    await client.get(f"/v1/customers/{uuid.uuid4()}", headers=t.headers)
    body = (await client.get("/metrics")).text
    assert 'route="/v1/customers/{customer_id}"' in body


@pytest.fixture
async def traced() -> AsyncIterator[tuple[httpx.AsyncClient, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = Settings()
    app = create_app(settings, tracer_provider=provider)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://traced") as c:
            yield c, exporter


async def test_one_request_is_one_trace_across_api_and_database(
    traced: tuple[httpx.AsyncClient, InMemorySpanExporter],
    make_tenant: MakeTenant,
    owner_engine: object,
) -> None:
    client, exporter = traced
    t = await make_tenant("acme")
    exporter.clear()
    resp = await client.post(
        "/v1/customers", headers=t.headers, json={"name": "Traced", "phone": "9000000042"}
    )
    assert resp.status_code == 201

    spans = exporter.get_finished_spans()
    server = [s for s in spans if s.kind.name == "SERVER"]
    assert len(server) == 1
    trace_id = server[0].context.trace_id
    db_spans = [s for s in spans if s.attributes and s.attributes.get("db.system") == "postgresql"]
    assert db_spans, [s.name for s in spans]
    assert all(s.context.trace_id == trace_id for s in db_spans)

    # The audit row written in that request carries the same trace id.
    from sqlalchemy.ext.asyncio import AsyncEngine

    assert isinstance(owner_engine, AsyncEngine)
    async with owner_engine.connect() as conn:
        stored: str = (
            await conn.execute(
                text("SELECT trace_id FROM audit_logs WHERE action = 'customer.created'")
            )
        ).scalar_one()
    assert stored == format(trace_id, "032x")
