# Slotwise

A multi-tenant appointment-booking SaaS backend, built the way a small team would run it in production:
tenant isolation enforced by the database, double-booking made impossible under concurrency, idempotent
APIs, a transactional outbox with signed webhooks, rotating refresh tokens, and traces that follow one
booking from the HTTP request through to the background worker.

**Stack:** Python 3.12 · FastAPI · PostgreSQL 16 (RLS, exclusion constraints) · SQLAlchemy 2 (async) ·
Alembic · Redis · Celery · OpenTelemetry · Prometheus · Grafana · Nginx · Docker · GitHub Actions

---

## Problem
Clinics, salons and coaching centres need online booking: a service catalogue, staff schedules, live
availability, bookings, payments, reminders and integrations. Each business is a **tenant**, and a tenant must
never see another tenant's data. Booking is full of concurrency traps: two people grab the last slot at
the same moment, a mobile client retries a request whose response was lost, a webhook receiver is down for an hour.

## Why it exists
It's a portfolio project about **production hardening**, the part that separates "a working CRUD API" from
"a backend a team can operate". Every feature exists to demonstrate a specific failure mode and its fix,
and each fix is covered by a test that reproduces the failure.

## Architecture

```
Client ─▶ Nginx ─▶ FastAPI ──────────────▶ PostgreSQL  (RLS per tenant, exclusion constraint,
          (runtime   │  request-id → tenant   │           outbox table in the same transaction)
          DNS, rate  │  → auth → rate limit   │
          limit)     ▼                        │ SELECT … FOR UPDATE SKIP LOCKED
                   Redis ◀── rate limits,      ▼
                   idempotency cache      Outbox relay ──▶ Celery (Redis broker)
                                                            ├─ signed webhooks (lease, backoff, DLQ, breaker)
                                                            ├─ customer emails (inbox dedupe) ─▶ SMTP/SES
                                                            └─ beat: retries, reminders, unpaid expiry

OpenTelemetry: API ─▶ DB ─▶ Redis ─▶ (traceparent stored in the outbox row) ─▶ worker, all in ONE trace
Prometheus: RED metrics per route template + outbox lag, webhook results, booking rate
```

Details and the reasoning behind each component: [`docs/architecture.md`](docs/architecture.md).

## Features
| Area | What's implemented |
|---|---|
| Tenancy | tenant-scoped repositories **and** Postgres row-level security (`SET LOCAL app.tenant_id`); separate DB roles for migrations, API (RLS enforced) and workers |
| Booking correctness | `EXCLUDE USING gist (staff_id WITH =, period WITH &&)`: overlapping bookings are rejected by the database; buffers included; optimistic locking (`version`) on reschedule/cancel |
| Availability | pure, timezone-aware slot algorithm (working hours − time off − bookings), property-tested against a brute-force oracle across DST changes |
| Idempotency | `Idempotency-Key` on POSTs: replay, mismatch (422), in-flight (409), crash recovery via lock expiry; response stored in the same transaction as the change |
| Auth | Argon2id passwords; 15-min JWTs (pinned algorithm); rotating refresh tokens with family-wide reuse detection; scoped API keys (hashed, prefix lookup) |
| Limits | Redis fixed-window rate limits per tenant / per key, login brute-force throttle, monthly booking quota per plan |
| Events | transactional outbox → relay (`SKIP LOCKED`, at-least-once) → Celery; consumer inbox for dedupe |
| Webhooks | HMAC-SHA256 signed (`t=…,v1=…`), leased delivery, exponential backoff with jitter, dead-letter queue + manual retry, per-endpoint circuit breaker, SSRF guard, secrets encrypted at rest (Fernet) |
| Payments | signed, idempotent inbound webhook from a mock provider (`ON CONFLICT (provider, provider_ref)`), amount check |
| Background jobs | reminders (indexed scan, not broker ETAs), expiry of unpaid holds, idempotency-key purge |
| Uploads | S3 presigned POST with content-type + size conditions, tenant-prefixed keys |
| Observability | JSON logs with request/tenant/trace ids, OpenTelemetry traces (FastAPI, SQLAlchemy, Redis, httpx, Celery), Prometheus metrics, Grafana dashboard + alert rules as code |
| Delivery | multi-stage non-root Docker image, full compose stack, Nginx, GitHub Actions (lint, types, tests, pip-audit, gitleaks, trivy), OIDC deploy workflow |

## Tech stack
| Choice | Why | Alternatives considered |
|---|---|---|
| FastAPI + Pydantic v2 | async, typed, OpenAPI for free | Django REST |
| SQLAlchemy 2 async + asyncpg | explicit sessions/transactions | SQLModel, raw asyncpg |
| PostgreSQL 16 | RLS, exclusion constraints, `SKIP LOCKED`, JSONB | — |
| Celery + Redis | the industry default for Python background work | arq, Dramatiq |
| OpenTelemetry | vendor-neutral traces | vendor SDKs |
| Nginx | TLS, slow-client protection, coarse rate limit | Caddy, Traefik |

## Quick start
Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
make up          # postgres (55436), redis (56383), mailpit (58025) — creates DB roles on first start
uv sync
make migrate
make check       # ruff + mypy --strict + 88 tests (unit + integration against real Postgres/Redis)
make run         # API on http://localhost:18081  (docs at /docs)
```

The whole system (API, worker, beat, relay, Nginx, Jaeger, Prometheus, Grafana):

```bash
make full-up     # Nginx :58088 · Jaeger :56686 · Prometheus :59091 · Grafana :53001 · Mailpit :58025
make smoke       # end-to-end: tenant → booking → signed payment → cancel → emails arrive
make full-down
```

## API usage
A full curl walkthrough is in [`examples/walkthrough.sh`](examples/walkthrough.sh). The core calls:

```bash
# book a slot (safe to retry with the same key)
curl -X POST localhost:18081/v1/bookings \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: 7b1c3f0e-booking-1" \
  -H "Content-Type: application/json" \
  -d '{"service_id":"…","staff_id":"…","customer_id":"…","start":"2030-01-07T10:00:00+05:30"}'
# → 201 booking | 409 slot_taken | 409 idempotency_in_progress | 422 outside_working_hours

curl -X POST "localhost:18081/v1/bookings/$ID:reschedule" -H "Authorization: Bearer $TOKEN" \
  -d '{"start":"2030-01-07T12:00:00+05:30","version":1}'     # → 409 version_conflict if stale
```

Endpoint list: `/docs` (OpenAPI). Verifying webhook signatures on the receiving side:
[`examples/webhook_receiver.py`](examples/webhook_receiver.py).

## Testing
88 tests, run against **real** Postgres and Redis (no DB mocks):
- **Concurrency:** 20 simultaneous requests for one slot → exactly one 201, nineteen 409s.
- **Tenant isolation:** raw SQL *without* a `WHERE tenant_id` clause, run as the API role, still sees one tenant only; cross-tenant writes fail with an RLS violation.
- **Availability:** hypothesis property tests (300 examples each) against a brute-force oracle, across 5 timezones including DST and half-hour/30-minute-DST zones.
- **Outbox:** a relay crash mid-batch loses nothing; six concurrent relays never double-publish.
- **Auth:** refresh-token replay revokes the family; forged, expired and `alg:none` JWTs are rejected.
- **Webhooks:** signature verifies; failure → backoff → dead letter after 8 attempts; breaker trips after 10.
- **Tracing:** one request produces one trace, API → DB → worker (the test drives the real ASGI lifespan).
- **Smoke:** `scripts/smoke.py` runs end to end against the full stack, through Nginx.

## Deployment
Locally: Docker Compose (above). AWS: [`deploy/aws/README.md`](deploy/aws/README.md) has a low-cost
single-EC2 layout with RDS, SSM secrets, OIDC-authenticated GitHub deploys, expand/contract migrations and
rollback by image tag. **Status: scripted but not yet executed against AWS.** No public endpoint exists.

## Security
Summary: RLS + app-layer scoping, least-privilege DB roles, Argon2id, pinned JWT algorithm, refresh
rotation with reuse detection, hashed API keys with non-grantable admin scopes, HMAC + timestamp on every
webhook (both directions), SSRF guard on outbound URLs, no redirects followed, encrypted webhook secrets,
constant-time comparisons, per-tenant and per-IP rate limits, dependency/secret/container scanning in CI.
Full checklist against the OWASP API Security Top 10, plus known gaps: [`docs/security.md`](docs/security.md).

## Performance
Measured numbers only, with how they were produced: [`docs/benchmarks.md`](docs/benchmarks.md).
- Availability algorithm, 30 staff × 12 bookings, one day: **median 3.59 ms** (in-memory only, one run on a
  laptop with other workloads active; see the benchmarks doc for the exact command and caveats).
- k6 booking rush (single runs on a busy laptop): **one API process handles ~89 rps at p95 41 ms** (booking
  p95 60 ms, 0 errors) and saturates at ~106 rps, CPU-bound on the single uvicorn process. **Two replicas: 169 rps**
  at p95 1.27 s (still CPU-bound). The runs also found three bugs (details in the benchmarks doc).

## Engineering trade-offs
- **The database enforces correctness** (exclusion constraint, RLS, unique keys) instead of app-level locks,
  at the cost of mapping Postgres error codes (e.g. `23P01`) to API errors.
- **At-least-once over exactly-once:** the outbox can re-publish, so every consumer dedupes. Exactly-once
  delivery across a network isn't achievable; idempotent consumers are the practical answer.
- **Fixed-window rate limiting:** cheap, but allows up to 2× the limit across a window boundary.
- **Monthly quota is not atomic** with the insert: it can overshoot by a request or two at the boundary. Fine for a billing soft-wall.
- **Reminders by periodic scan**, not Celery ETA tasks: always consistent with the DB, survives reschedules and restarts.
- **Leases instead of row locks for webhook delivery:** no DB connection held across a 10-second HTTP call.
- **Global tables without RLS** (users, memberships, refresh tokens, API keys) because they're read before the tenant is known. Documented and explicitly filtered.

## Limitations
- Not deployed to AWS yet; zero-downtime deploy and rollback are designed but unverified.
- Load tests are single runs on a busy laptop; per-request CPU cost not yet profiled.
- Payment provider is a mock; no real gateway integration.
- Webhook SSRF guard is vulnerable to DNS rebinding (resolve-then-connect); see `docs/security.md`.
- The worker's Prometheus endpoint requires `--pool threads` (prefork needs multiprocess mode).
- Local file uploads need an S3-compatible store; MinIO images are no longer freely pullable, so none is bundled.

## Roadmap
- Profile per-request CPU (py-spy) and compare `uvicorn --workers N` with N containers.
- Deploy through `cloud-infra-lab` Terraform; verify rollback.
- Connect-to-vetted-IP egress for webhooks (close the DNS-rebinding gap).
- Calendar sync (Google/Outlook) and a real payment gateway.

## Contributing
See [`CONTRIBUTING.md`](CONTRIBUTING.md). Learning notes for this codebase: [`docs/LEARNING_GUIDE.md`](docs/LEARNING_GUIDE.md).

## License
MIT. See [`LICENSE`](LICENSE).
