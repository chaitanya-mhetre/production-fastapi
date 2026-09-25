"""App factory. `create_app()` wires middleware, routers and error handlers."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis

from slotwise.api.routers import (
    api_keys,
    auth,
    bookings,
    catalog,
    customers,
    health,
    tenants,
    uploads,
    webhooks,
)
from slotwise.config import Settings, get_settings
from slotwise.context import request_id_var, tenant_id_var
from slotwise.db import Database
from slotwise.errors import AppError, RateLimited
from slotwise.logging_config import configure_logging
from slotwise.observability import metrics
from slotwise.observability.tracing import build_provider, instrument_clients, instrument_fastapi

log = logging.getLogger("slotwise.http")


def _error_body(code: str, message: str, details: object = None) -> dict[str, object]:
    body: dict[str, object] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


def create_app(
    settings: Settings | None = None, *, tracer_provider: TracerProvider | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    provider = tracer_provider or build_provider(settings, settings.service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.db = Database(settings.database_url, settings)
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        if provider is not None:
            instrument_clients(app.state.db.engine, provider)
        yield
        if provider is not None:
            provider.shutdown()  # flush buffered spans before exit
        await app.state.redis.aclose()
        await app.state.db.dispose()

    app = FastAPI(title="Slotwise", version="0.1.0", lifespan=lifespan)
    if provider is not None:
        instrument_fastapi(app, provider)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Accept an upstream id (from Nginx) so logs join up across hops; otherwise mint one.
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        tid_token = tenant_id_var.set(None)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            # Label by route *template* (/v1/bookings/{booking_id}), never the raw path:
            # raw paths put every UUID into its own time series and blow up Prometheus.
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")
            metrics.HTTP_REQUESTS.labels(request.method, template, str(status_code)).inc()
            metrics.HTTP_LATENCY.labels(request.method, template).observe(
                time.perf_counter() - started
            )
            response.headers["x-request-id"] = request_id
            rate = getattr(request.state, "rate_limit", None)
            if rate is not None:
                response.headers["X-RateLimit-Limit"] = str(rate.limit)
                response.headers["X-RateLimit-Remaining"] = str(max(rate.remaining, 0))
                response.headers["X-RateLimit-Reset"] = str(rate.reset_in)
            return response
        finally:
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(rid_token)
            tenant_id_var.reset(tid_token)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(exc.retry_after)
            headers["X-RateLimit-Limit"] = str(exc.limit)
            headers["X-RateLimit-Remaining"] = "0"
        return JSONResponse(
            _error_body(exc.code, exc.message, exc.details or None),
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            _error_body(
                "validation_failed",
                "request validation failed",
                [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()],
            ),
            status_code=422,
        )

    for router in (
        health.router,
        auth.router,
        tenants.router,
        customers.router,
        catalog.router,
        bookings.router,
        api_keys.router,
        webhooks.router,
        uploads.router,
    ):
        app.include_router(router)
    return app


app = create_app()
