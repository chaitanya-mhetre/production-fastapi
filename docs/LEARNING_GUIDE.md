# Learning guide: Slotwise

This guide is for studying the codebase until you can explain every design decision out loud, without notes.
Work through it in order. After each section, close the file and explain the idea to an imaginary interviewer.

---

## 0. The one-paragraph pitch (memorise this)
"Slotwise is a multi-tenant booking backend in FastAPI and Postgres. Tenant isolation is enforced twice:
repositories always filter by tenant, and Postgres row-level security blocks anything they miss. Double
booking is impossible because an exclusion constraint makes Postgres reject overlapping bookings atomically.
POSTs are idempotent through an Idempotency-Key whose response is stored in the same transaction as the
change. Domain events go through a transactional outbox to Celery, which sends HMAC-signed webhooks with
retries, a dead-letter queue and a circuit breaker. OpenTelemetry carries one trace from the HTTP request
through the outbox into the worker."

---

## 1. Reading order (≈ 2–3 evenings)

| # | File | What to understand |
|---|---|---|
| 1 | `src/slotwise/config.py` | 12-factor settings; why there are three DB URLs |
| 2 | `docker/postgres/10-roles.sql` | the three roles and why the worker has BYPASSRLS |
| 3 | `migrations/versions/0001_tenancy.py` + `src/slotwise/migration_helpers.py` | how an RLS policy is written; `NULLIF(current_setting(...), '')` |
| 4 | `src/slotwise/db.py` | the `after_begin` hook: `set_config('app.tenant_id', …, true)` on every transaction |
| 5 | `src/slotwise/security/*` | Argon2, JWT decode with a pinned algorithm, the RBAC table, `Principal` |
| 6 | `src/slotwise/api/deps.py` | the dependency chain: principal → rate limit → permission → tenant session |
| 7 | `src/slotwise/main.py` | app factory, middleware (request id, metrics), error handlers |
| 8 | `src/slotwise/repositories/base.py`, `customers.py` | layer-1 tenant scoping, keyset pagination (`pagination.py`) |
| 9 | `tests/integration/test_tenancy.py` | **read the tests as the spec**: especially the two RLS tests |
| 10 | `src/slotwise/availability.py` + `tests/unit/test_availability.py` | pure functions, DST, property tests with an oracle |
| 11 | `migrations/versions/0003_bookings.py` | the exclusion constraint |
| 12 | `src/slotwise/services/bookings.py` | `_overlap_is_409`, `update_versioned` (optimistic locking) |
| 13 | `src/slotwise/services/idempotency.py` | `claim()`, `run_idempotent()`, and the three outcomes |
| 14 | `tests/integration/test_bookings.py` | the 20-concurrent-requests test, the idempotency tests |
| 15 | `src/slotwise/services/refresh_tokens.py` | rotation + family revocation |
| 16 | `src/slotwise/services/api_keys.py`, `security/rate_limit.py`, `services/quotas.py` | key format, fixed window |
| 17 | `src/slotwise/outbox/writer.py`, `outbox/relay.py` | dual-write problem, `SKIP LOCKED` |
| 18 | `src/slotwise/worker/core.py` | fan-out, leased delivery, backoff, breaker, inbox, reminders |
| 19 | `src/slotwise/worker/celery_app.py`, `worker/tasks.py` | acks_late and friends; why `asyncio.run` per task |
| 20 | `src/slotwise/webhooks/*` | signing, SSRF guard, Fernet |
| 21 | `tests/integration/test_events.py` | crash-mid-batch, concurrent relays, DLQ, breaker |
| 22 | `src/slotwise/observability/*` + `tests/integration/test_observability.py` | traces, metrics, the lifespan bug |
| 23 | `Dockerfile`, `docker-compose.yml`, `nginx/nginx.conf`, `.github/workflows/*` | delivery |
| 24 | `docs/architecture.md`, `docs/adr/*`, `docs/security.md` | the why, in prose |

Hands-on after reading: `make up && make migrate && make check`, then `make full-up && make smoke`, then open
Jaeger (:56686), find an `outbox.dispatch` span, and confirm it sits in the same trace as the booking request.

---

## 2. Concepts, each tied to the code

### 2.1 Multi-tenancy and row-level security
- **Shared schema, `tenant_id` column** on every tenant table: cheapest to run, but isolation is only as good as your filters.
- **Layer 1:** `TenantScopedRepository.__init__` (repositories/base.py) refuses to exist without a tenant id,
  and every query adds `Model.tenant_id == self.tenant_id`.
- **Layer 2:** `enable_tenant_rls()` creates `POLICY tenant_isolation USING (...) WITH CHECK (...)`.
  `USING` filters reads/updates/deletes; `WITH CHECK` validates inserted/updated rows.
- **Why transaction-local:** `set_config(name, value, is_local => true)` resets at COMMIT/ROLLBACK. A
  session-level setting would stay on the pooled connection and the *next request* (maybe another tenant)
  would inherit it. `db.py::_set_tenant_on_begin` runs on **every** transaction start.
- **Why `NULLIF(…, '')`:** once a custom setting has been used on a connection, `current_setting(x, true)`
  returns `''` instead of NULL, and `''::uuid` raises an error. NULLIF turns it into NULL, which matches nothing: fail closed.
- **Proof:** `test_rls_hides_rows_even_without_where_clause` runs `SELECT name FROM customers` with no
  WHERE, as the API role, and sees one tenant's rows.
- **Global tables** (users, memberships, API keys, refresh tokens) are read *before* the tenant is known, so there's no RLS there. Know this limitation.

### 2.2 Double booking: exclusion constraints
- `EXCLUDE USING gist (staff_id WITH =, period WITH &&) WHERE (status IN (...))`: "no two rows where
  staff_id is equal AND periods overlap". `&&` is the range-overlap operator; `btree_gist` lets `=` on a
  uuid be used in a GiST index.
- `period` is the **reserved block** = duration + buffer, so buffers are protected too.
- Half-open ranges `[start, end)`: a 10:00–10:40 booking and a 10:40 booking don't conflict.
- The partial `WHERE` means cancelling a booking frees the slot automatically.
- Error code `23P01` → `SlotTaken` in `BookingService._overlap_is_409`.
- **Why not check first?** Check-then-insert is a race (time-of-check vs time-of-use). The constraint
  is checked atomically inside the insert.

### 2.3 Optimistic locking
`UPDATE bookings SET …, version = version + 1 WHERE id = :id AND version = :expected` (`repositories/bookings.py::update_versioned`).
0 rows updated means someone else changed it first → `409 version_conflict`, and the client reloads. No locks are held
while a human stares at a form. Compare with pessimistic locking (`SELECT … FOR UPDATE`), used in
`confirm_paid` because that path is short and fully server-side.

### 2.4 Idempotency keys
Read `services/idempotency.py` top to bottom. The state machine:
```
no row           → INSERT (PK tenant_id,key) wins → run handler
row, same hash, response stored   → replay (header Idempotent-Replayed: true)
row, different hash               → 422 idempotency_key_reused
row, no response, lock valid      → 409 idempotency_in_progress
row, no response, lock expired    → take over (the first attempt crashed)
```
- The **claim** is its own tiny committed transaction, so a concurrent duplicate sees it immediately.
- The **response** is written in the *same* transaction as the booking (`ctx.complete(session, …)` then
  `session.commit()`), so there's never a booking without a stored response, or the reverse.
- **4xx business errors are stored** (a retry of a lost race gets the same 409); unexpected errors
  **release** the claim so the client can retry.
- Redis is only a cache of finished responses: `test_replay_survives_redis_cache_loss` flushes Redis and still replays from Postgres.

### 2.5 Tokens
- Access JWT: 15 minutes, stateless, `algorithms=["HS256"]` **pinned** (defeats `alg: none` and HS/RS confusion).
- Refresh token: opaque random string, stored as a SHA-256 hash, **rotated on every use**.
- **Reuse detection** (`refresh_tokens.py::rotate`): presenting an already-rotated token means it was copied.
  Revoke the whole `family_id` and **commit before raising 401**, or the revocation would be rolled back.
- Refresh re-checks membership and tenant status, so removing a user takes effect within 15 minutes.
- Login: `verify_password(None, …)` still hashes against a dummy, so timing doesn't reveal whether an email exists.

### 2.6 API keys, rate limits, quotas
- Key `sw_<prefix8>_<secret>`: lookup by indexed prefix, compare the SHA-256 hash with `hmac.compare_digest`.
- Admin scopes (`api_keys:manage`, `webhooks:manage`, …) can't be granted to keys, so a leaked key can't escalate.
- Fixed window: `INCR rl:<subject>:<minute>` + `EXPIRE`. Weakness: bursts across the boundary (up to 2× the limit).
  Alternatives: sliding-window log (exact, O(n) memory) or token bucket (smooth, a Lua script).
- Quota: a count query on the `(tenant_id, created_at)` index. Not atomic, and documented as such.

### 2.7 Transactional outbox and at-least-once delivery
- **Dual-write problem:** you can't atomically commit to Postgres AND publish to Redis. Either order breaks on a crash.
- **Outbox:** insert the event row in the business transaction (`outbox/writer.py::emit`).
- **Relay** (`outbox/relay.py::relay_batch`): `FOR UPDATE SKIP LOCKED` lets multiple relays share the work
  without blocking or double-sending. A publish failure rolls back the batch, so nothing is lost.
- A crash after publish but before commit → re-publish → **consumers must be idempotent**:
  `UNIQUE(endpoint_id, outbox_event_id)` for webhooks, the `consumer_inbox` table for emails.
- **Exactly-once delivery doesn't exist across a network.** What you build is at-least-once delivery + idempotent processing ("effectively once").

### 2.8 Webhooks done properly
- **Signature:** `HMAC_SHA256(secret, f"{t}.{raw_body}")`, header `t=…,v1=…`. The timestamp is inside the MAC, and
  old timestamps are rejected, which stops replays. Verify the **raw bytes** before parsing JSON.
- **Lease** (`worker/core.py::deliver`): claim by moving `next_retry_at` forward, commit, do HTTP outside any
  transaction, record the result. No DB connection held during a slow call.
- **Backoff with jitter:** `30s·2^(n−1)` capped at 6 h, multiplied by a random factor in [0.5, 1). Jitter avoids the thundering herd.
- **Dead-letter queue:** after 8 attempts the status is `dead`, visible at `GET /v1/webhook-deliveries?status=dead`, with manual retry.
- **Circuit breaker:** 10 consecutive failures disable the endpoint (`disabled_reason`); `:enable` resets it.
- **SSRF guard:** resolve the host and reject private/loopback/link-local/reserved IPs. `follow_redirects=False`.
  Known gap: DNS rebinding.
- **Secrets encrypted, not hashed:** we need the plaintext to sign, so Fernet with a key from config.

### 2.9 Celery semantics you must be able to explain
- `task_acks_late=True`: ack after success, so a crash causes redelivery, which requires idempotent tasks.
- `task_reject_on_worker_lost=True`: requeue if the process is OOM-killed.
- `worker_prefetch_multiplier=1`: no hoarding; fair dispatch.
- `visibility_timeout`: Redis re-delivers unacked tasks after this long; it must exceed your longest task.
- Why `asyncio.run` per task (`worker/tasks.py::_run`): Celery tasks are sync; each call gets a fresh event
  loop, so the engine uses `NullPool` (asyncio connections can't cross event loops).
- Why reminders use a scan, not ETA tasks: ADR 0004.

### 2.10 Observability
- **Logs:** JSON, one line per event, with `request_id`, `tenant_id` and `trace_id` from contextvars (`logging_config.py`).
- **Metrics (RED):** `http_requests_total`, `http_request_duration_seconds` labelled by **route template**.
  A raw path would create a time series per UUID (cardinality explosion). Business metrics: bookings,
  outbox backlog/lag, webhook results.
- **Traces:** FastAPI/SQLAlchemy/Redis/httpx spans. The outbox row stores the W3C `traceparent`
  (`propagate.inject`), and the worker continues it (`propagate.extract` in `dispatch_event`), so one
  trace crosses the async boundary.
- **The lifespan bug (a great interview story):** instrumentation was first added in the startup hook.
  Tests passed because they entered the lifespan directly, but under uvicorn Starlette builds the middleware
  stack on the first ASGI call, which *is* the lifespan startup, so the tracing middleware was never used and
  the outbox stored empty trace contexts. Found by checking Jaeger. Fixed by instrumenting at app creation,
  and the test now drives the real ASGI lifespan protocol, so it fails on the old code.
- **The Nginx DNS bug (another story):** after recreating the API container, Nginx returned 502 because it
  had resolved `api` once at startup. Fixed with `resolver 127.0.0.11` + `server api:8000 resolve` in a zoned upstream.

### 2.11 Delivery
- Multi-stage Dockerfile: deps layer cached by `uv.lock`; final image has only the venv, runs as UID 10001.
- `--timeout-graceful-shutdown 20` + `stop_grace_period`: in-flight requests finish during deploys.
- CI runs the tests against real service containers on the same ports as local compose.
- Deploy workflow: GitHub OIDC → IAM role (no stored AWS keys), ECR, SSM `send-command`, post-deploy smoke.
  **Not executed yet.** Say so if asked.

---

## 3. Interview questions with answers

1. **How do you guarantee no double booking?**
   A Postgres exclusion constraint on `(staff_id =, period &&)` for active bookings. The DB rejects the
   overlapping insert atomically with error 23P01, which we map to 409. App-level check-then-insert is a race.
   I tested it with 20 concurrent requests: exactly one wins.

2. **Why not a `SERIALIZABLE` transaction instead?**
   It also works, but then any transaction can fail with a serialization error and must be retried, even
   ones that didn't truly conflict. The constraint is targeted: it only fails on a real overlap, and it
   protects every write path, including ones added later.

3. **What if a developer forgets `WHERE tenant_id = …`?**
   RLS. The API connects as a role without BYPASSRLS, and every tenant table has a policy comparing
   `tenant_id` to a transaction-local setting. A test runs unfiltered SQL and still sees one tenant.

4. **Why must the tenant setting be transaction-local?**
   Connections are pooled. A session-level `SET` survives on the connection and the next request, possibly
   another tenant's, would run with it. `set_config(..., true)` resets at transaction end.

5. **Explain the dual-write problem and the outbox pattern.**
   You can't atomically commit to the DB and publish to a broker. Writing the event into an outbox table in
   the same transaction makes both-or-neither true. A relay publishes afterwards. That gives at-least-once
   delivery, so consumers dedupe.

6. **Why `FOR UPDATE SKIP LOCKED` in the relay?**
   So several relays can run: each locks a different batch and skips rows others hold, instead of blocking
   or publishing the same rows twice. The same pattern works for any Postgres-backed job queue.

7. **Can you get exactly-once delivery?**
   Not across a network, because an ack can always be lost. You get at-least-once delivery plus idempotent
   consumers (unique constraints, an inbox table, event ids for receivers).

8. **How do you make a POST idempotent? Same key, different body?**
   The client sends an Idempotency-Key. We claim it with an INSERT on (tenant, key) and store the response
   in the same transaction as the change. Same key + same body replays; a different body gives 422; still in flight gives 409;
   a crashed first attempt's lock expires and a retry can take over.

9. **Why store 4xx responses but not 5xx?**
   A 4xx like `slot_taken` is a deterministic answer to that request, so a retry should get the same answer.
   A 5xx is our failure; we release the key so the retry can actually succeed.

10. **What is refresh-token reuse detection?**
    Every refresh issues a new token and retires the old one, and tokens from one login share a family. If a
    retired token comes back, someone copied it. We can't tell thief from victim, so we revoke the family and
    both have to log in again.

11. **Why short-lived JWTs plus refresh tokens instead of long-lived JWTs?**
    JWTs can't be revoked without a server lookup. Short access tokens bound the damage window; refresh
    tokens are stateful and revocable, and refresh is where membership is re-checked.

12. **How are API keys stored? Why SHA-256 and not Argon2 like passwords?**
    Only a hash is stored, with a plain-text prefix for indexed lookup. Keys have 256 bits of randomness,
    so brute force is impossible even with a fast hash. Argon2 exists because human passwords are guessable.

13. **How do you verify a webhook signature correctly?**
    HMAC-SHA256 over `timestamp.raw_body` with the shared secret, compared in constant time. Reject old
    timestamps (replay protection), and verify the raw bytes before parsing, because re-serialised JSON may differ.

14. **What's a circuit breaker, and where is yours?**
    After N consecutive failures, stop calling a dependency for a while instead of hammering it. Mine disables
    a webhook endpoint after 10 consecutive failures (`worker/core.py::deliver`). The tenant re-enables it
    after fixing their receiver. A fuller breaker has a half-open state that probes automatically.

15. **Why exponential backoff with jitter?**
    Backoff gives a failing receiver room to recover. Jitter spreads retries out so thousands of queued
    deliveries don't all hit it in the same second when it comes back (the thundering herd).

16. **What's SSRF and how did you defend against it?**
    Making our server request internal URLs chosen by an attacker, like cloud metadata at 169.254.169.254.
    We resolve the webhook host and reject non-public IPs, block credentials in URLs, and don't follow
    redirects. Remaining gap: DNS rebinding. Fix: connect to the vetted IP, or use an egress proxy.

17. **What does `acks_late` change, and what does it require?**
    The task is acknowledged after it finishes, so a worker crash causes redelivery instead of loss.
    That means tasks can run twice, so they must be idempotent.

18. **Why did you avoid Celery ETA tasks for reminders?**
    They sit in the broker for days, get re-delivered after Redis's visibility timeout, go stale on
    reschedule/cancel, and vanish if Redis is lost. A per-minute indexed scan of Postgres is always consistent.

19. **How do you trace a request across an async queue?**
    Store the W3C traceparent with the message (in the outbox row), then extract it in the worker and start
    the span with that parent context. One trace then shows API → DB → worker → webhook.

20. **Why label metrics by route template?**
    Every label value is a new time series. Raw paths with UUIDs create unbounded cardinality and can take
    Prometheus down. Templates keep it to the number of routes.

21. **Keyset vs offset pagination?**
    OFFSET N makes the DB scan and discard N rows, and pages shift when rows are inserted. Keyset
    (`WHERE (created_at, id) > cursor ORDER BY … LIMIT n`) is index-backed and stable.

22. **Liveness vs readiness probes?**
    Liveness: is the process alive (never check dependencies, or a DB blip restarts every pod). Readiness:
    can it serve traffic right now (check the DB), and failing it removes the instance from the load balancer.

23. **How does a zero-downtime deploy work here, and what about migrations?**
    Two API containers behind Nginx, restarted one at a time with a readiness wait; uvicorn drains in-flight
    requests on SIGTERM. Migrations are expand/contract, so old and new code both work mid-rollout.
    Honest caveat: designed and scripted, not yet run on AWS.

24. **Tell me about a bug you found in your own system.**
    Use the lifespan-instrumentation story (2.10) or the Nginx stale-DNS 502 (2.10). Both were found by
    running the real stack, not by unit tests, and both led to a test or config that prevents a repeat.

25. **What would you change at 100× the traffic?**
    Move availability reads to a cache keyed by (staff, day) invalidated by booking events; partition
    bookings by time; move the relay to logical decoding (Debezium/CDC) instead of polling; ElastiCache and
    RDS read replicas; ECS/Kubernetes with autoscaling; a sliding-window or token-bucket limiter; and
    connect-to-IP webhook egress.

---

## 4. Things to be honest about in an interview
- Built with AI assistance. Be ready to explain every file anyway, and this guide is how.
- Not deployed to AWS; no load-test numbers yet. Only the availability micro-benchmark has been measured.
- The payment provider is a mock.
- Known security gaps are listed in `docs/security.md`. Bringing them up yourself signals maturity.
