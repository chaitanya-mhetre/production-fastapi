# Load tests

| Scenario | File | Status |
|---|---|---|
| Booking rush (availability browse + contended booking) | `k6/booking_rush.js` | written, **not yet run** |
| Availability algorithm micro-benchmark | `../scripts/bench_availability.py` | run once, see `docs/benchmarks.md` |

What the booking rush should show: `bookings_created` never exceeds the number of distinct slots
(the exclusion constraint holds under load), zero 5xx, and p95 latency for the rush scenario.
