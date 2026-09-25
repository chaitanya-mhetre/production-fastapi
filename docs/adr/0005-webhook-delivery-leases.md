# ADR 0005: Leases, not row locks, for webhook delivery

**Status:** accepted

**Context.** A delivery attempt makes an HTTP call that can take up to 10 s. Two workers (the dispatcher and
the retry sweeper) must not send the same delivery at the same time.

**Decision.** Claim a delivery by atomically pushing `next_retry_at` forward 60 s (a lease) and committing.
Then make the HTTP call with no transaction open, then record the result in a new transaction.

**Consequences.** No DB connection or row lock is held during network I/O. If a worker dies mid-call, the lease
expires and the sweeper retries: at-least-once, and the `Slotwise-Event-Id` header lets receivers dedupe.
