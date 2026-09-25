# Comparison with a Go implementation of the same patterns

The author has implemented outbox, idempotency and a Redis-Streams event bus in Go in another codebase.
A side-by-side comparison (Go relay vs Celery + outbox, Redis Streams consumer groups vs a Celery broker,
idempotency design differences) is **pending**. It will be written only once ownership/disclosure of that
codebase is cleared. No code from it is used here.
