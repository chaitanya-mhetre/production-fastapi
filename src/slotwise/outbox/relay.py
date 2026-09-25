"""Outbox relay: moves committed outbox rows onto the Celery queue.

    SELECT ... WHERE published_at IS NULL ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED
    → publish each → mark published_at → COMMIT

- FOR UPDATE SKIP LOCKED lets several relay replicas run at once: each grabs a different batch
  instead of blocking on (or double-publishing) rows another replica holds.
- If the relay dies after publishing but before COMMIT, the rows stay unpublished and are sent
  again. Delivery is therefore AT-LEAST-ONCE, and consumers must dedupe on the event id (see
  consumer_inbox and the UNIQUE(endpoint_id, outbox_event_id) on webhook deliveries).
- If publishing fails midway (broker down), the transaction rolls back: nothing is marked
  published, nothing is lost, and the next loop retries.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select

from slotwise.config import get_settings
from slotwise.db import Database
from slotwise.models import OutboxEvent
from slotwise.observability import metrics

log = logging.getLogger("slotwise.relay")

Publisher = Callable[[OutboxEvent], None]


async def relay_batch(db: Database, publish: Publisher, batch_size: int = 100) -> int:
    """Publish one batch. Returns how many events were published."""
    async with db.session() as session, session.begin():
        events = list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        now = datetime.now(UTC)
        for event in events:
            publish(event)  # raising here rolls back the whole batch: nothing is lost
            event.published_at = now
            event.attempts += 1
            metrics.OUTBOX_LAG.observe((now - event.created_at).total_seconds())
        if events:
            log.info("relayed outbox batch", extra={"count": len(events), "last_id": events[-1].id})
    return len(events)


async def unpublished_count(db: Database) -> int:
    async with db.session() as session:
        count = await session.execute(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        )
        return int(count.scalar_one())


def celery_publisher(event: OutboxEvent) -> None:
    from slotwise.worker.tasks import dispatch_event

    dispatch_event.delay(event.id)


async def run_forever(poll_seconds: float = 0.5) -> None:
    settings = get_settings()
    db = Database(settings.worker_database_url, settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # graceful shutdown on deploys
        loop.add_signal_handler(sig, stop.set)
    log.info("outbox relay started")
    while not stop.is_set():
        try:
            published = await relay_batch(db, celery_publisher)
            metrics.OUTBOX_UNPUBLISHED.set(await unpublished_count(db))
        except Exception:
            log.exception("relay batch failed; will retry")
            published = 0
        if not published:  # only sleep when idle, so a backlog drains at full speed
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    await db.dispose()
    log.info("outbox relay stopped")


if __name__ == "__main__":
    from slotwise.logging_config import configure_logging

    configure_logging(get_settings().log_level)
    asyncio.run(run_forever())
