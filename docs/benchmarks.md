# Benchmarks

Only numbers produced by a committed script, with the command and environment. Anything else is TBD.

## Availability algorithm (in-memory only)
```
uv run python scripts/bench_availability.py        # defaults: 30 staff × 12 bookings, 50 runs
```
| Date | Machine | Result |
|---|---|---|
| 2026-09-25 | Intel i5-1240P laptop, Python 3.12.14, Linux; other Docker workloads running at the same time | median 3.59 ms, p95 4.26 ms |

Caveats: a single run on a busy laptop; excludes DB queries, serialisation and HTTP. It shows the algorithm
isn't the bottleneck for a 30-staff day. It says nothing about endpoint latency.

## API under load: k6 booking rush (2026-09-25)

**Environment for every run:** Intel i5-1240P laptop (16 threads), 14.8 GB RAM, Linux, Docker 29.6.
The laptop was busy the whole time (IDE, browser, other projects' test suites; load average 2–3.5).
Each row is a **single run**, so expect run-to-run noise of tens of percent; no error bars.
Minimal stack only (`loadtest/docker-compose.loadtest.yml`): postgres, redis, mailpit, api (uvicorn, **one
worker process**), celery worker, relay, nginx; tracing off. k6 in Docker (`loadtest/run.sh`), raw output in
`loadtest/results/`.

Scenario (`loadtest/k6/booking_rush.js`): `browse` = GET /v1/availability at a constant rate; `rush` =
POST /v1/bookings ramping to the target rate, all competing for **4 slots**, so nearly every booking is a
409 by design. `http_req_failed` in k6 counts those 409s, so the useful error signals are 5xx and 429.

| # | Setup | Target rps (browse + rush) | Achieved rps | p95 all | p95 booking | Dropped iters | 5xx | Result |
|---|---|---|---|---|---|---|---|---|
| 02 | via nginx, as-shipped limits | 50 + 50 | 88.7 | 17.7 ms | 20.8 ms | 0 | 0 | **7,750 × 429**: the pro plan's 1,200 req/min per tenant and nginx's 50 r/s per IP both cap a single client IP/tenant well below this load. Working as designed. |
| 03 | direct to API, tenant limit raised | 50 + 50 | 88.7 | 40.4 ms | 64.9 ms | 0 | 0 | all thresholds pass |
| 04 | direct, 1 replica | 100 + 100 | 105.7 | 2.35 s | 2.41 s | 4,103 | 0 | **saturated**: api container pinned at ~100% CPU (one core), Postgres ~30% |
| 05 | 2 replicas, before fix | 100 + 100 | 75.8 | 2.03 s | 4.35 s | 3,727 | **125** | Postgres `deadlock_detected` (40P01) → HTTP 500, max latency 46 s |
| 06 | 2 replicas, advisory-lock fix | 100 + 100 | 163.6 | 1.51 s | 1.62 s | 651 | 0 | deadlocks gone… |
| 07 | 1 replica, advisory-lock fix | 50 + 50 | 88.7 | 711 ms | **907 ms** | 0 | 0 | …but booking p95 14× worse than run 03, so the lock was reverted |
| 08 | 1 replica, final (40P01 → 409) | 50 + 50 | 88.7 | 41.2 ms | 59.7 ms | 0 | 0 | back to the run-03 baseline; 0 deadlocks, 0 500s in logs |
| 09 | 2 replicas, final | 100 + 100 | 169.1 | 1.27 s | 1.46 s | 342 | 0 | still CPU-bound: both api containers ~100%, Postgres 55–99% |

### Bottleneck
**The single uvicorn process is CPU-bound** at roughly 100 requests/s on this laptop (run 04: api at one full
core while Postgres sat at ~30%). A second replica nearly doubles throughput (105.7 → 169.1 rps, run 09), which
confirms the API CPU, not the database, is the first limit. At 2 replicas Postgres climbs to 55–99% of a core,
so it would be the next limit. Not yet profiled: where the CPU goes per request (JSON serialisation, Pydantic,
SQLAlchemy ORM overhead and RLS `SET` per transaction are the suspects). Next step: `py-spy record` during run 04.

### Bugs found by running it
1. **k6 script used Idempotency-Keys under 8 characters** (`k6-1-0`): 265 × 422 in the first run. The API was
   right; the script was wrong. Keys now include a run id.
2. **Nginx answered rate limiting with 503.** `limit_req` defaults to 503, which looks like an outage to clients,
   load balancers and alerting. Now `limit_req_status 429`, guarded by `tests/unit/test_nginx_config.py`.
3. **Deadlocks between overlapping bookings returned 500.** Two concurrent INSERTs with overlapping periods each
   wait on the other's uncommitted row inside the `no_double_booking` exclusion check; Postgres aborts one with
   40P01 after `deadlock_timeout` (1 s). The loser simply lost the race for that time, so it now maps to
   409 `slot_taken` (`tests/unit/test_booking_conflicts.py`). A per-staff advisory lock also fixed it but
   serialised every attempt, including the ones that fail fast (run 07), so it was measured and reverted.

### Still TBD
| What | How |
|---|---|
| Outbox lag p95 under load | needs the Prometheus container during a run (skipped to save RAM) |
| Webhook success rate with 10% failing endpoints | scenario not written |
| Throughput with `uvicorn --workers N` vs N containers | same scripts, change the CMD |
| Repeated runs with error bars on a quiet machine | `loadtest/run.sh`, 3× each |
