# Architecture

## Request path
```
Nginx → uvicorn → FastAPI middleware (request id, metrics) → dependency chain → router → service → repository → Postgres
```
1. **Nginx** terminates TLS (in prod), drops slow clients (`client_body_timeout`), caps bodies at 1 MB,
   applies a coarse per-IP rate limit, stamps `X-Request-ID`, and re-resolves the API containers' DNS every
   10 s so redeploys don't produce 502s.
2. **Request middleware** (`main.py`) binds `request_id` into a contextvar (every log line carries it) and
   records RED metrics labelled by **route template**.
3. **`get_principal`** (`api/deps.py`) authenticates either a bearer JWT or an `X-API-Key`, then applies the
   Redis rate limit for that subject (tenant, or key).
4. **`require(Permission.X)`** checks RBAC. Roles map to permission sets (`security/permissions.py`); API keys
   carry scopes directly.
5. **`get_tenant_session`** opens a session tagged with the tenant id. An `after_begin` hook runs
   `set_config('app.tenant_id', …, true)` at the start of **every** transaction in it.
6. **Services** own business rules and write the audit row and the outbox event in the same transaction.
   **Repositories** are the only place SQL is built, and they can't be constructed without a tenant id.
7. The route **commits explicitly**. Nothing is committed implicitly by a dependency.

## Why three database roles
| Role | Used by | RLS | Why |
|---|---|---|---|
| `slotwise` | Alembic | owner (bypasses) | DDL needs ownership |
| `slotwise_app` | API | **enforced** | a missing `WHERE tenant_id` in app code still can't leak |
| `slotwise_worker` | relay, Celery | BYPASSRLS | queues span tenants; queries still filter explicitly |

## Write path of a booking
```
BEGIN
  (after_begin) set_config('app.tenant_id', T, true)
  INSERT idempotency_keys … ON CONFLICT DO NOTHING          ← separate short tx: the claim
BEGIN
  validate hours / time off
  INSERT bookings … (exclusion constraint may raise 23P01 → 409 slot_taken)
  INSERT audit_logs …
  INSERT outbox_events (…, trace_context = current traceparent)
  UPDATE idempotency_keys SET response_status, response_body
COMMIT                                                        ← all of it, or none of it
```

## Event path
```
relay:   SELECT … FROM outbox_events WHERE published_at IS NULL ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED
         → celery dispatch_event.delay(id) → UPDATE published_at → COMMIT
worker:  dispatch_event(id)
           continue the trace from outbox_events.trace_context
           INSERT webhook_deliveries … ON CONFLICT (endpoint_id, outbox_event_id) DO NOTHING   ← dedupe
           deliver(): lease (next_retry_at += 60s, COMMIT) → HTTP POST (no tx held) → record result
           notify_customer(): INSERT consumer_inbox … ON CONFLICT DO NOTHING → send email → COMMIT
beat:    retry_due_deliveries (15s) · send_due_reminders (60s) · expire_unpaid_bookings (60s) · purge keys (1h)
```

## Failure modes and what happens
| Failure | Outcome |
|---|---|
| Two requests for the same slot | the exclusion constraint lets exactly one commit; the other gets 409 |
| Client retries after a lost response | same Idempotency-Key → stored response replayed |
| API crashes mid-request holding an idempotency claim | `locked_until` expires (30 s); the retry takes over |
| Relay dies after publishing, before COMMIT | events re-published; deliveries/inbox dedupe |
| Broker down | relay batch rolls back; outbox grows; `OutboxBacklog` alert fires |
| Worker killed mid-task | `acks_late` + `reject_on_worker_lost` → the task is redelivered |
| Worker dies mid-webhook | the lease expires; the sweeper retries |
| Receiver down for hours | backoff with jitter; after 10 consecutive failures the endpoint is disabled; after 8 attempts the delivery is dead-lettered |
| Redis lost | rate-limit counters reset; idempotency falls back to Postgres; queued tasks lost, **but** outbox rows are already marked published → those events need a re-drive (see runbook) |
| Stolen refresh token replayed | the whole token family is revoked |
| Deploy replaces the API container | Nginx re-resolves DNS; uvicorn drains for up to 20 s |
