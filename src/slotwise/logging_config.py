"""Structured JSON logs. Every line carries request_id, tenant_id and trace_id so one request can
be followed across API, worker and database logs."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from slotwise.context import request_id_var, tenant_id_var
from slotwise.observability.tracing import current_trace_id

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        tenant = tenant_id_var.get()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant_id": str(tenant) if tenant else None,
            "trace_id": current_trace_id(),
        }
        # Anything passed via `extra={...}` becomes a top-level field.
        payload |= {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access",):  # our middleware logs requests with more context
        logging.getLogger(noisy).handlers[:] = []
        logging.getLogger(noisy).propagate = False
