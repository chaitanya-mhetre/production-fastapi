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

## API under load
| Scenario | Script | Result |
|---|---|---|
| Booking rush (browse 50 rps + contended booking ramp to 50 rps) | `loadtest/k6/booking_rush.js` | **TBD, not yet run** |
| Booking-create p95 at N rps | same | **TBD** |
| Outbox lag p95 under load | Prometheus `outbox_lag_seconds` during the rush | **TBD** |
| Webhook success rate with 10% failing endpoints | (not written yet) | **TBD** |
