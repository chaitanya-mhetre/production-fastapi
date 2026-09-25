"""OpenTelemetry tracing.

Off unless SLOTWISE_OTEL_EXPORTER_OTLP_ENDPOINT is set (or a test passes its own provider).
When on, one booking request produces a single trace: FastAPI server span → SQLAlchemy query
spans → Redis spans, and the Celery task spawned later links back to it via propagated context.
The trace id is also stamped on every log line and audit row, so logs ↔ traces ↔ audit join up.
"""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from sqlalchemy.ext.asyncio import AsyncEngine

from slotwise.config import Settings

UNTRACED_URLS = "healthz,readyz,metrics"  # probes would drown real traffic in the trace store


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def build_provider(
    settings: Settings, service_name: str, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    if exporter is None:
        if not settings.otel_exporter_otlp_endpoint:
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": settings.env}
        )
    )
    # Batch: spans are exported off the request path, in the background.
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def instrument_fastapi(app: object, provider: TracerProvider) -> None:
    """Must run when the app is created, NOT in the lifespan hook: under uvicorn, Starlette
    builds its middleware stack on the very first ASGI call (the lifespan startup event), so
    middleware added during startup is silently never used."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,  # type: ignore[arg-type]
        tracer_provider=provider,
        excluded_urls=UNTRACED_URLS,
    )


def instrument_clients(engine: AsyncEngine, provider: TracerProvider) -> None:
    """DB/Redis/httpx client instrumentation: runs in the lifespan, once the engine exists."""
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    # Async engines are instrumented through their underlying sync engine. skip_dep_check:
    # the instrumentation's declared range stops at SQLAlchemy < 2.1, but the event hooks it
    # uses are unchanged in 2.1. The trace test in tests/integration guards this assumption.
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine, tracer_provider=provider, skip_dep_check=True
    )
    RedisInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)


def instrument_worker(provider: TracerProvider) -> None:
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    trace.set_tracer_provider(provider)
    CeleryInstrumentor().instrument(tracer_provider=provider)  # type: ignore[no-untyped-call]
    SQLAlchemyInstrumentor().instrument(tracer_provider=provider, skip_dep_check=True)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
