"""Prometheus metrics. RED metrics (Rate, Errors, Duration) for HTTP plus business metrics."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
BOOKINGS_CREATED = Counter("bookings_created_total", "Bookings created", ["tenant_plan"])
OUTBOX_UNPUBLISHED = Gauge("outbox_unpublished_count", "Outbox rows waiting to be relayed")
OUTBOX_LAG = Histogram(
    "outbox_lag_seconds",
    "Time from outbox write to relay publish",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
WEBHOOK_DELIVERIES = Counter("webhook_delivery_total", "Webhook delivery attempts", ["result"])
CELERY_TASK_DURATION = Histogram("celery_task_duration_seconds", "Celery task run time", ["task"])
