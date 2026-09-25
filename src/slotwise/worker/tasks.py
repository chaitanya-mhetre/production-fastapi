"""Celery tasks: thin sync wrappers that run the async core with fresh dependencies.

Each task builds its own engine (NullPool) and HTTP client inside `asyncio.run`, because an
asyncio connection pool can't be shared across the separate event loops each task call creates.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from slotwise.config import get_settings
from slotwise.db import Database
from slotwise.notifications.mailer import SmtpMailer
from slotwise.observability import metrics
from slotwise.worker import core
from slotwise.worker.celery_app import celery_app


def _run[T](name: str, fn: Callable[[core.WorkerDeps], Awaitable[T]]) -> T:
    async def main() -> T:
        settings = get_settings()
        db = Database(settings.worker_database_url, settings, pooled=False)
        async with httpx.AsyncClient() as http:
            deps = core.WorkerDeps(
                db=db,
                http=http,
                settings=settings,
                mailer=SmtpMailer(settings.smtp_host, settings.smtp_port, settings.mail_from),
            )
            try:
                return await fn(deps)
            finally:
                await db.dispose()

    started = time.perf_counter()
    try:
        return asyncio.run(main())
    finally:
        metrics.CELERY_TASK_DURATION.labels(name).observe(time.perf_counter() - started)


@celery_app.task(
    name="slotwise.dispatch_event",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=10,
)
def dispatch_event(event_id: int) -> int:
    return len(_run("dispatch_event", lambda deps: core.dispatch_event(deps, event_id)))


@celery_app.task(name="slotwise.retry_due_deliveries")
def retry_due_deliveries() -> int:
    return _run("retry_due_deliveries", core.retry_due_deliveries)


@celery_app.task(name="slotwise.send_due_reminders")
def send_due_reminders() -> int:
    return _run("send_due_reminders", core.send_due_reminders)


@celery_app.task(name="slotwise.expire_unpaid_bookings")
def expire_unpaid_bookings() -> int:
    return _run("expire_unpaid_bookings", core.expire_unpaid_bookings)


@celery_app.task(name="slotwise.purge_idempotency_keys")
def purge_idempotency_keys() -> int:
    return _run("purge_idempotency_keys", core.purge_idempotency_keys)
