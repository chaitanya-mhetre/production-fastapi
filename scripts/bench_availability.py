"""Micro-benchmark: the pure availability algorithm for N staff over one day (no DB, no HTTP).

    uv run python scripts/bench_availability.py --staff 30 --bookings-per-staff 12 --runs 50

This measures only the in-memory computation. The API endpoint adds DB queries and
serialisation on top; measure that with the k6 scenario in loadtest/.
"""

import argparse
import platform
import random
import statistics
import time
from datetime import date, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from slotwise.availability import Interval, compute_slots, day_bounds_utc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staff", type=int, default=30)
    parser.add_argument("--bookings-per-staff", type=int, default=12)
    parser.add_argument("--runs", type=int, default=50)
    args = parser.parse_args()

    rng = random.Random(7)  # noqa: S311 — deterministic test data, not crypto
    tz, day = ZoneInfo("Asia/Kolkata"), date(2030, 1, 7)
    bounds = day_bounds_utc(day, tz)
    staff_busy = []
    for _ in range(args.staff):
        busy = []
        for _ in range(args.bookings_per_staff):
            start = bounds.start + timedelta(minutes=rng.randrange(9 * 60, 17 * 60, 15) - 330)
            busy.append(Interval(start, start + timedelta(minutes=40)))
        staff_busy.append(busy)

    timings = []
    for _ in range(args.runs):
        started = time.perf_counter()
        for busy in staff_busy:
            compute_slots(
                day=day,
                tz=tz,
                working_hours=[(dtime(9), dtime(17))],
                busy=busy,
                duration=timedelta(minutes=30),
                buffer=timedelta(minutes=10),
            )
        timings.append((time.perf_counter() - started) * 1000)

    timings.sort()
    print(f"python {platform.python_version()} on {platform.machine()} / {platform.system()}")  # noqa: T201
    print(f"{args.staff} staff x {args.bookings_per_staff} bookings, {args.runs} runs")  # noqa: T201
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"median {statistics.median(timings):.2f} ms, p95 {p95:.2f} ms")  # noqa: T201


if __name__ == "__main__":
    main()
