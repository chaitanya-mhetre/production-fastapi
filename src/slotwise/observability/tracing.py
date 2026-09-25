"""OpenTelemetry helpers. Full setup is added in M6; this module is safe to import without it."""

from opentelemetry import trace


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
