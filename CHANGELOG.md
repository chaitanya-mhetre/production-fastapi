# Changelog

## [0.1.0] - 2026-09-25
Milestones, tagged `m1`…`m8`:
- **m1** tenancy: tenants, users, memberships, login, customers, Postgres RLS, three DB roles, audit log.
- **m2** catalogue: services, staff, weekly hours, time off; timezone/DST-aware availability with property tests.
- **m3** bookings: exclusion-constraint double-booking prevention, optimistic locking, Idempotency-Key.
- **m4** auth hardening: rotating refresh tokens with reuse detection, scoped API keys, rate limits, plan quotas.
- **m5** events: transactional outbox + relay, Celery, signed webhooks (backoff, DLQ, circuit breaker, SSRF
  guard), inbox-deduped emails, reminders, unpaid-hold expiry, inbound payment webhook, presigned uploads.
- **m6** observability: OpenTelemetry traces, Prometheus metrics, Grafana dashboard, alert rules.
- **m7** delivery: Docker image, full compose stack with Nginx, CI (tests, pip-audit, gitleaks, trivy), AWS
  deploy workflow and scripts (not yet executed); trace context carried through the outbox.
- **m8** load-test scenario (not yet run), availability micro-benchmark, documentation and learning guide.
