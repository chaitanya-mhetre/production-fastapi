# ADR 0004: Reminders by periodic scan, not Celery ETA tasks

**Status:** accepted

**Context.** Customers get a reminder 24 h before a confirmed booking.

**Options.** (1) On create, schedule a Celery task with `eta = start − 24h`. (2) Every minute, scan for
bookings starting within 24 h with `reminder_sent_at IS NULL`.

**Decision.** Option 2. ETA tasks live in the broker for days. With Redis they're re-delivered once past the
visibility timeout, they go stale when bookings are rescheduled or cancelled, and they vanish if Redis is lost.
The scan reads the current truth from Postgres, uses a partial index, and claims rows with `SKIP LOCKED`.

**Consequences.** Up to one minute of reminder latency; one cheap indexed query per minute.
