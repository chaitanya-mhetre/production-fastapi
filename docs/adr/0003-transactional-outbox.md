# ADR 0003: Transactional outbox for domain events

**Status:** accepted

**Context.** Booking changes must trigger webhooks and emails. Publishing to the broker directly from the
request has the dual-write problem: commit-then-publish loses events on a crash; publish-then-commit
announces bookings that were rolled back.

**Decision.** Write the event to `outbox_events` in the same transaction as the change. A relay publishes
unpublished rows with `FOR UPDATE SKIP LOCKED` and marks them published.

**Consequences.** Delivery is at-least-once. Consumers dedupe via `UNIQUE(endpoint_id, outbox_event_id)` and
the `consumer_inbox` table. There's a small publish delay (poll interval 0.5 s when idle). The relay is one more
process to run and monitor (`outbox_unpublished_count`, `outbox_lag_seconds`).
