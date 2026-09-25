"""Celery application: broker = Redis, schedule = celery beat.

Reliability settings (and why):
- task_acks_late: ack only after the task finishes, so a worker crash re-delivers the task
  instead of losing it. Tasks must therefore be idempotent, and ours are.
- task_reject_on_worker_lost: with acks_late, requeue if the worker process is killed (OOM).
- worker_prefetch_multiplier=1: don't let one worker hoard tasks it hasn't started, so a slow
  task doesn't block a queue of fast ones behind it.
- visibility_timeout: how long Redis waits before re-delivering an unacked task. It must exceed
  the longest task, or long tasks run twice.
"""

from typing import Any

from celery import Celery
from celery.signals import worker_init
from prometheus_client import start_http_server

from slotwise.config import get_settings
from slotwise.logging_config import configure_logging
from slotwise.observability.tracing import build_provider, instrument_worker

settings = get_settings()

celery_app = Celery(
    "slotwise", broker=settings.celery_broker_url, include=["slotwise.worker.tasks"]
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_transport_options={"visibility_timeout": 3600},
    task_default_queue="slotwise",
    timezone="UTC",
    beat_schedule={
        "retry-due-webhooks": {"task": "slotwise.retry_due_deliveries", "schedule": 15.0},
        "send-reminders": {"task": "slotwise.send_due_reminders", "schedule": 60.0},
        "expire-unpaid-bookings": {"task": "slotwise.expire_unpaid_bookings", "schedule": 60.0},
        "purge-idempotency-keys": {"task": "slotwise.purge_idempotency_keys", "schedule": 3600.0},
    },
)


@worker_init.connect
def _setup_observability(**_: Any) -> None:
    """Runs once when the worker boots.

    Metrics note: run the worker with `--pool threads` (our tasks are I/O-bound). With the
    default prefork pool each child process has its own metric registry and this endpoint would
    only show the parent's. Prefork needs prometheus_client's multiprocess mode instead.
    """
    configure_logging(settings.log_level)
    provider = build_provider(settings, "slotwise-worker")
    if provider is not None:
        instrument_worker(provider)
    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port)
