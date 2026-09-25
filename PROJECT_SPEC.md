# Production FastAPI — "Slotwise"
> A multi-tenant appointment-booking SaaS backend, hardened the way a real team would run it: tenant isolation, idempotency, outbox, webhooks, refresh tokens, API keys, workers, OpenTelemetry, Nginx, AWS.

## 1. Problem & why it exists
Clinics, salons, and coaching centres need online booking: service catalogue, staff schedules, slot availability,
bookings, payment status, reminders, and integrations over webhooks. Each business is a **tenant**, and one tenant must
never see another's data.

**Why this project:** `python-backend-lab` teaches Python and backend basics. This project is about **production
hardening**, the things that separate "a working API" from "a backend a team can operate":
tenant isolation, double-booking prevention, idempotent writes, reliable event delivery, token lifecycle, observability, deployment.
I chose a booking domain on purpose, because it's full of concurrency problems (two people booking the last slot at once).

## 2. What this proves to an employer
| Skill | Target requirement |
|---|---|
| Multi-tenant SaaS design | EaseOps (SaaS backend), Razorpay/Atlassian benchmarks |
| Idempotency + transactional outbox | Razorpay-style payment/platform thinking; distributed-systems interviews |
| Celery workers, retries, dead letters | EaseOps "async processing" |
| JWT access + rotating refresh tokens, API keys | EaseOps "authentication/security" |
| OpenTelemetry traces, Prometheus metrics | Razorpay "observability" |
| Nginx, Docker, AWS deploy | EaseOps "AWS"; all backend roles |
| Concurrency control in PostgreSQL | Zeko "databases, schema design" |

## 3. Scope
### In scope (v1)
- Tenants (businesses), users with tenant-scoped roles (`tenant_admin`, `staff`, `receptionist`), plus a platform `superadmin`
- Services (catalogue), staff, working hours, time-off, **slot availability computation**
- Bookings: create / reschedule / cancel, with **double-booking prevention**
- Customers (the tenant's end customers)
- Payment status tracking via a mock payment provider webhook (inbound webhook verification)
- Outgoing webhooks (`booking.created`, `booking.cancelled`, ...) through a **transactional outbox**
- Reminders (email via SES/Mailpit locally) scheduled by workers
- File uploads (staff avatars, tenant logos) to S3 with presigned URLs
- Refresh-token rotation with reuse detection; API keys per tenant; per-tenant rate limits and quotas
- **Idempotency-Key** header on all POSTs
- Audit log; health checks; structured logs; metrics; traces
- Docker, Nginx reverse proxy, CI/CD, AWS deploy (infra lives in `cloud-infra-lab`)

### Out of scope (explicitly)
- Real payment processing (a mock provider only)
- Frontend
- Calendar sync (Google/Outlook), a v2 idea
- Kubernetes (covered in `cloud-infra-lab`)

## 4. Architecture
```
Internet ─▶ Nginx (TLS, gzip, body limits, upstream timeouts)
              │
        ┌─────▼──────────────────────────────┐
        │ FastAPI (uvicorn workers)           │
        │ middleware: request-id → OTel →     │
        │   tenant resolution → auth → rate   │
        │ routers → services → repositories   │
        └───┬───────────────┬─────────────┬──┘
            │               │             │
      ┌─────▼─────┐   ┌─────▼────┐   ┌────▼───┐
      │PostgreSQL │   │  Redis   │   │   S3   │
      │ + outbox  │   │ limits,  │   │uploads │
      │ table     │   │ idem.,   │   └────────┘
      └─────┬─────┘   │ broker   │
            │         └────┬─────┘
   outbox relay (beat)     │
            └──────▶ Celery workers: webhooks, emails, reminders, payment reconciliation
                                   │
                        OTel collector → Jaeger/Tempo ; Prometheus → Grafana
```
- **Nginx**: TLS termination, request size and time limits, a buffer against slow clients. It keeps uvicorn off the raw internet.
- **Tenant resolution middleware**: finds the tenant from the JWT claim or the API key and puts it in a `contextvar`.
  Repositories *require* a tenant id, so there's no code path that queries without one.
- **Postgres Row-Level Security (RLS)**: a second layer. `SET LOCAL app.tenant_id` per transaction plus RLS policies,
  so a forgotten `WHERE tenant_id` still can't leak data.
- **Outbox table + relay**: domain events are written in the same transaction as the business change. A relay publishes them to Celery.
  This guarantees at-least-once delivery without dual-write bugs.
- **Celery workers**: keep slow work (emails, webhooks, reminders) off the request path. They retry with backoff and send poison messages to a dead-letter queue.
- **Redis**: the Celery broker, idempotency-key store (with a TTL), rate limiting.
- **OTel collector**: traces span API → DB → Redis → Celery task, so one booking can be followed end to end.

## 5. Tech stack & justification
| Choice | Why | Alternatives |
|---|---|---|
| FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic | same core as the lab, so the focus is hardening | — |
| Celery 5 + Redis broker, celery-beat | the industry default. Worth knowing because many JDs expect it (the lab used arq) | arq, Dramatiq |
| PostgreSQL 16 with RLS + `btree_gist` exclusion constraints | the database enforces "no overlapping bookings" instead of the app | app-level locks only |
| OpenTelemetry SDK + collector, Prometheus, Grafana | vendor-neutral observability | Datadog (paid) |
| Nginx | standard reverse proxy; I've already used it (the Ponticare deploy) | Traefik, Caddy |
| boto3 / S3 presigned URLs | no file bytes go through the API | streaming uploads through the API |
| Mailpit (local) / SES (AWS) | test emails locally without spam | — |

## 6. Data model
```
tenants(id uuid pk, slug unique, name, plan enum(free,pro), timezone, status, created_at)
users(id uuid pk, email citext unique, password_hash, is_superadmin bool)
tenant_memberships(user_id, tenant_id, role enum(tenant_admin,staff,receptionist), pk(user_id,tenant_id))
refresh_tokens(id uuid pk, user_id, family_id uuid, token_hash, expires_at, revoked_at, replaced_by uuid null)
    index (family_id)                          -- reuse detection revokes the whole family
api_keys(id, tenant_id, prefix unique, key_hash, scopes text[], rate_limit_per_min, revoked_at)
services(id, tenant_id, name, duration_min, price_paise, buffer_min, active)
staff(id, tenant_id, user_id null, display_name, avatar_key)
staff_services(staff_id, service_id, tenant_id)
working_hours(id, tenant_id, staff_id, weekday smallint, start_time, end_time)
time_off(id, tenant_id, staff_id, period tstzrange)
customers(id, tenant_id, name, phone, email, unique(tenant_id, phone))
bookings(id uuid pk, tenant_id, staff_id, service_id, customer_id, period tstzrange,
         status enum(pending_payment,confirmed,cancelled,no_show,completed), version int, created_at,
         EXCLUDE USING gist (staff_id WITH =, period WITH &&) WHERE (status IN ('pending_payment','confirmed')))
payments(id, tenant_id, booking_id, provider_ref unique, amount_paise, status, raw jsonb)
idempotency_keys(tenant_id, key, request_hash, response_status, response_body jsonb, locked_until, created_at,
         pk(tenant_id, key))          -- Redis is the fast path; the Postgres table is the durable record
outbox_events(id bigserial pk, tenant_id, aggregate_type, aggregate_id, event_type, payload jsonb,
         created_at, published_at null, attempts int)
    index (created_at) where published_at is null
webhook_endpoints(id, tenant_id, url, secret_enc, events text[], active)
webhook_deliveries(id, endpoint_id, outbox_event_id, attempt, status_code, response_ms, next_retry_at, dead_lettered bool)
audit_logs(id bigserial, tenant_id, actor, action, entity, entity_id, diff jsonb, request_id, trace_id, created_at)
```
Every tenant table has `tenant_id NOT NULL` + an RLS policy `USING (tenant_id = current_setting('app.tenant_id')::uuid)`.

## 7. API / interface design
```
POST /v1/auth/login            → {access_token (15m), refresh_token (30d, rotating)}
POST /v1/auth/refresh          → rotates; reuse of an old token → 401 + family revoked
POST /v1/auth/logout
POST /v1/tenants  (superadmin)   GET /v1/tenant   PATCH /v1/tenant
POST /v1/api-keys  DELETE /v1/api-keys/{id}
CRUD /v1/services  /v1/staff  /v1/staff/{id}/working-hours  /v1/staff/{id}/time-off  /v1/customers
GET  /v1/availability?service_id=&staff_id=&date=2026-10-01     → [{start, end, staff_id}]
POST /v1/bookings              (Idempotency-Key required)   → 201 | 409 slot_taken
POST /v1/bookings/{id}:reschedule   POST /v1/bookings/{id}:cancel
GET  /v1/bookings?from=&to=&staff_id=&status=&cursor=
POST /v1/uploads:presign       → {url, fields, key}
POST /v1/webhooks/payments/mockpay   (inbound; HMAC verified; idempotent on provider_ref)
CRUD /v1/webhook-endpoints     GET /v1/webhook-deliveries?status=failed   POST /v1/webhook-deliveries/{id}:retry
GET  /v1/audit-logs
GET  /healthz  /readyz  /metrics (internal only via Nginx allow-list)
```
Idempotency semantics: the same key plus the same body replays the stored response. The same key with a different body gives 422 `idempotency_key_reused`.
A request that's still in flight gives 409 `idempotency_in_progress`.

## 8. Key engineering problems
1. **Double booking under concurrency.** Two requests for the same slot at once. The primary defence is a Postgres
   exclusion constraint. The app catches `ExclusionViolation` and returns 409 `slot_taken`. A test fires 20 concurrent requests and asserts exactly one wins.
2. **Dual-write problem.** "Commit the booking, then publish the event" loses events on a crash between the two steps.
   Fix: the outbox row goes in the same transaction. The relay uses `SELECT ... FOR UPDATE SKIP LOCKED` batches, publishes, then marks `published_at`.
   Delivery is at-least-once, so consumers dedupe on the event id.
3. **Idempotent POSTs.** Mobile clients retry on bad networks. A lock in Redis (`SET NX PX`) plus the durable response in Postgres.
   Edge case: the process dies after taking the lock. The lock expires through `locked_until`.
4. **Refresh-token theft.** Rotation plus reuse detection: if a token that was already used shows up again, revoke the whole family.
5. **Tenant isolation.** An app-layer contextvar plus Postgres RLS. A test tries a cross-tenant read through every repository and expects zero rows.
6. **Availability computation.** Working hours − time-off − existing bookings − buffers, all timezone-aware. Property-based tests (hypothesis) cover DST edges.
7. **Webhook reliability.** Retries with jitter, a circuit breaker per endpoint (disabled after N consecutive failures, tenant notified), a dead-letter view.
8. **Graceful shutdown.** Uvicorn/Celery SIGTERM handling, so deploys don't drop in-flight requests or tasks (`acks_late`, `worker_prefetch_multiplier=1`).

## 9. Milestones
**M1: Tenancy skeleton (2 wks).** Tenants, users, memberships, login, tenant middleware, repositories that require a tenant, RLS policies + migration.
*Accept:* the cross-tenant leakage test suite passes; RLS proves that a missing WHERE still returns 0 rows.

**M2: Catalogue + availability (2 wks).** Services, staff, hours, time-off, the availability algorithm.
*Accept:* hypothesis tests for timezone/DST; availability for a 30-staff tenant across 1 day computed in `TBD ms`.

**M3: Bookings + concurrency (1–2 wks).** The exclusion constraint, reschedule/cancel with versioning, the idempotency layer.
*Accept:* 20-concurrent-request test gives exactly one success; idempotency replay/mismatch/in-flight tests.

**M4: Auth hardening (1 wk).** Refresh rotation + reuse detection, API keys with scopes, per-tenant rate limits and plan quotas.
*Accept:* a replay-attack test revokes the family; a quota exceeded gives 429 with headers.

**M5: Outbox + Celery + webhooks + emails (2 wks).** The relay, workers, signed webhooks, retry/backoff/circuit breaker, DLQ, reminder scheduling, the mock payment inbound webhook.
*Accept:* kill the relay or a worker mid-batch and no event is lost (verified by a test); a duplicate inbound payment webhook is processed once.

**M6: Observability (1 wk).** OTel traces across API → DB → Celery, Prometheus metrics (RED + business: bookings_created_total, outbox_lag_seconds), Grafana dashboards as JSON in the repo, an alert rule examples file.
*Accept:* screenshot of one booking trace spanning API and worker; the outbox lag metric visible.

**M7: Nginx + Docker + CI/CD + AWS (1–2 wks).** Compose prod profile, Nginx config, a GitHub Actions pipeline (lint, type, test, build, push to ECR, deploy), deployed through `cloud-infra-lab` Terraform.
*Accept:* a public HTTPS endpoint (temporary, torn down after the demo); a zero-downtime deploy documented; rollback tested.

**M8: Load test + docs (1 wk).** k6 scenario "booking rush" and failure-injection notes; complete README, ADRs, runbook.
*Accept:* measured numbers in `docs/benchmarks.md` with the script and hardware noted.

## 10. Testing strategy
- Unit: availability algorithm (hypothesis), token rotation logic, idempotency state machine, webhook signature.
- Integration (testcontainers Postgres + Redis): RLS, exclusion constraint, outbox relay, Celery tasks run eagerly **and** with a real worker in one smoke test.
- Concurrency tests: `asyncio.gather` of N clients against a live app.
- Security tests: cross-tenant access for every endpoint (parametrised over the route table), JWT tampering, expired tokens.
- E2E smoke test in CI after deploy: `/readyz`, a booking create + cancel.

## 11. Observability
- Logs: JSON with `trace_id`, `span_id`, `tenant_id`, `request_id`.
- Metrics: HTTP RED metrics, `bookings_created_total{tenant_plan}`, `outbox_unpublished_count`, `outbox_lag_seconds`,
  `webhook_delivery_total{result}`, `celery_task_duration_seconds`, DB pool usage.
- Traces: FastAPI, SQLAlchemy, Redis, Celery, httpx auto-instrumentation.
- Dashboards + alert rules committed as code (`observability/`).

## 12. Security
- RLS + app-layer tenant scoping; least-privilege DB role for the app (no `BYPASSRLS`); a separate migration role.
- Argon2; rotating refresh tokens in httpOnly cookies for browsers or the body for mobile (documented); API keys hashed.
- Inbound webhook HMAC + timestamp window; outbound SSRF guard.
- Secrets from AWS SSM/Secrets Manager in the cloud and `.env` locally. Never committed; gitleaks runs in CI.
- Dependency scanning (pip-audit), container scanning (trivy) in CI.
- OWASP API Top 10 checklist in `docs/security.md`.

## 13. Deployment
- Local: `docker compose --profile full up` (api, worker, beat, relay, postgres, redis, nginx, mailpit, otel-collector, jaeger, prometheus, grafana).
- AWS (via `cloud-infra-lab`): ECS Fargate or a single EC2 in low-cost mode, RDS Postgres, ElastiCache or self-hosted Redis, S3, SES, ALB/Nginx.
- Migrations run as a one-off task before rolling out the new version; expand/contract pattern documented.

## 14. Evaluation / measurements to collect
All `TBD` until measured, with scripts in `loadtest/`:
- Booking-create p95 latency at [N] RPS; the max sustained RPS before p95 > 300 ms
- Outbox lag p95 under load
- Webhook delivery success rate with 10% of endpoints failing (simulated)
- Cold availability query time for [N] staff

## 15. Prerequisite learning
`learning/backend/auth` (JWT, refresh rotation), `learning/databases/transactions` (isolation levels, locking),
`learning/databases/postgresql` (RLS, exclusion constraints), `learning/distributed-systems/idempotency`, `.../outbox`,
`learning/backend/celery`, `learning/cloud/observability`, `learning/cloud/nginx`.

## 16. Interview talking points
- "How do you guarantee no double booking?" (the DB constraint vs locks vs serializable isolation: trade-offs)
- "Explain the dual-write problem and the outbox pattern."
- "How do you make a POST idempotent? What if the client sends the same key with a different body?"
- "How do you isolate tenants? What if a developer forgets the WHERE clause?"
- "Refresh-token rotation: what's reuse detection?"
- "How would you trace a slow booking request?"

## 17. Resume bullet templates
- Built a multi-tenant booking SaaS backend (FastAPI, PostgreSQL, Celery, Redis) with Postgres row-level security, exclusion-constraint double-booking prevention, idempotent APIs, and a transactional outbox for at-least-once webhook delivery.
- Instrumented the API and workers with OpenTelemetry and Prometheus; sustained [MEASURED_VALUE] RPS at p95 [MEASURED_VALUE] ms in k6 load tests.
- Deployed via GitHub Actions → ECR → AWS with Terraform-managed infrastructure and tested rollback.

## 18. Open questions / uncertainties
- **Relation to Ponticare:** I've already built outbox, idempotency and a Redis Streams event bus in Go for Ponticare,
  largely with AI assistance. This project re-implements those patterns **in Python, by hand**, so I understand them
  deeply. `docs/comparison-with-go-implementation.md` should compare the two (the Go outbox relay vs Celery + outbox,
  Redis Streams vs the Celery broker). That's only possible if the Ponticare IP/employment question is resolved; see `career/resume-analysis/OPEN_QUESTIONS.md` #14.
- ECS Fargate vs a single EC2 for the demo: decide by cost in `cloud-infra-lab`.
- Celery vs arq for this project: Celery is chosen for JD coverage. Revisit if its complexity blocks progress.
- Timeline assumes ~10–12 h/week; unverified.
