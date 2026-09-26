# Load tests

| Scenario | File | Status |
|---|---|---|
| Booking rush (availability browse + contended booking) | `k6/booking_rush.js` | run 2026-09-25, results in `../docs/benchmarks.md` |
| Availability algorithm micro-benchmark | `../scripts/bench_availability.py` | run once, see `docs/benchmarks.md` |

What the booking rush should show: `bookings_created` never exceeds the number of distinct slots
(the exclusion constraint holds under load), zero 5xx, and p95 latency for the rush scenario.

## How to run
```bash
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml --profile full \
    up -d postgres redis mailpit migrate api worker relay nginx
docker compose exec api python -m slotwise.cli create-superadmin \
    --email sa@loadtest.example.com --password loadtest-password-1
LT_SUPERADMIN_EMAIL=sa@loadtest.example.com LT_SUPERADMIN_PASSWORD=loadtest-password-1 \
    uv run python loadtest/bootstrap.py > /tmp/k6.env
loadtest/run.sh my-run /tmp/k6.env          # refuses to start below 3 GB free RAM
```
Capacity runs (tenant limit out of the way): `LOADTEST_TENANT_RPM=1000000` on `up`, and `LT_BASE_URL=http://localhost:18081`.
Two replicas: add `-f loadtest/docker-compose.scale.yml --scale api=2`, `BASE=http://api:8000` in the env file, and
`K6_NETWORK=slotwise_default`.
