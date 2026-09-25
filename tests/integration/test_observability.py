"""M6 acceptance: metrics exposed, one request = one trace across API and DB, ids in audit."""

import asyncio
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
    # Drive startup through the real ASGI lifespan protocol, exactly as uvicorn does. (Entering
    # app.router.lifespan_context directly hid a bug where instrumentation added at startup
    # never ran under uvicorn.)
    startup_done, shutdown = asyncio.Event(), asyncio.Event()
    messages = iter([{"type": "lifespan.startup"}])

    async def receive() -> dict[str, str]:
        try:
            return next(messages)
        except StopIteration:
            await shutdown.wait()
            return {"type": "lifespan.shutdown"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "lifespan.startup.complete":
            startup_done.set()

    lifespan = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    )  # type: ignore[arg-type]
    await startup_done.wait()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://traced") as c:
        yield c, exporter
    shutdown.set()
    await lifespan


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


async def test_outbox_carries_trace_context_to_the_worker(
    traced: tuple[httpx.AsyncClient, InMemorySpanExporter],
    make_tenant: MakeTenant,
    make_catalog: object,
    settings: Settings,
) -> None:
    """The worker's dispatch span continues the trace of the API request that booked."""
    import random

    from opentelemetry import trace as otel_trace

    from slotwise.db import Database
    from slotwise.models import OutboxEvent
    from slotwise.notifications.mailer import RecordingMailer
    from slotwise.worker import core

    client, exporter = traced
    t = await make_tenant("acme")
    c = await make_catalog(t)  # type: ignore[operator]
    cust = (
        await client.post(
            "/v1/customers", headers=t.headers, json={"name": "T", "phone": "9000000077"}
        )
    ).json()
    exporter.clear()
    await client.post(
        "/v1/bookings",
        headers=t.headers | {"Idempotency-Key": "trace-ctx-key"},
        json={
            "service_id": c.service_id,
            "staff_id": c.staff_ids[0],
            "customer_id": cust["id"],
            "start": "2030-01-07T10:00:00+05:30",
        },
    )
    [api_span] = [s for s in exporter.get_finished_spans() if s.kind.name == "SERVER"]

    worker_db = Database(settings.worker_database_url, settings, pooled=False)
    from sqlalchemy import select

    async with worker_db.session() as s:
        event_id = (await s.execute(select(OutboxEvent.id))).scalar_one()
    provider = TracerProvider()
    worker_spans = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(worker_spans))
    original = core.tracer
    core.tracer = provider.get_tracer("test")
    try:
        async with httpx.AsyncClient() as http:
            deps = core.WorkerDeps(
                db=worker_db,
                http=http,
                settings=settings,
                mailer=RecordingMailer(),
                rng=random.Random(1),
            )
            await core.dispatch_event(deps, event_id)
    finally:
        core.tracer = original
        await worker_db.dispose()
    [dispatch] = worker_spans.get_finished_spans()
    assert dispatch.name == "outbox.dispatch"
    assert dispatch.context.trace_id == api_span.context.trace_id
    assert otel_trace.format_trace_id(dispatch.context.trace_id)
