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

from celery import Celery

from slotwise.config import get_settings

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
