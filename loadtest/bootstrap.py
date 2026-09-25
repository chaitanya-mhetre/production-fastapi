"""Create a throwaway pro-plan tenant + catalogue for the k6 booking rush, and print the k6 env.

    docker compose exec api python -m slotwise.cli create-superadmin \
        --email sa@loadtest.example.com --password loadtest-password-1
    LT_SUPERADMIN_EMAIL=sa@loadtest.example.com LT_SUPERADMIN_PASSWORD=loadtest-password-1 \
        uv run python loadtest/bootstrap.py > /tmp/k6.env

Prints KEY=value lines (BASE, TOKEN, SERVICE, STAFF, CUSTOMER, DAY) for `k6 run -e ...`.
"""

import os
import sys
import uuid
from datetime import date, timedelta

import httpx

BASE = os.environ.get("LT_BASE_URL", "http://localhost:58088")


def ok(resp: httpx.Response, expected: int) -> dict:  # type: ignore[type-arg]
    if resp.status_code != expected:
        sys.exit(f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text}")
    return resp.json() if resp.content else {}


def main() -> None:
    run = uuid.uuid4().hex[:8]
    slug = f"lt-{run}"
    with httpx.Client(base_url=BASE, timeout=15) as c:
        sa = ok(
            c.post(
                "/v1/auth/login",
                json={
                    "email": os.environ["LT_SUPERADMIN_EMAIL"],
                    "password": os.environ["LT_SUPERADMIN_PASSWORD"],
                },
            ),
            200,
        )
        ok(
            c.post(
                "/v1/tenants",
                headers={"Authorization": f"Bearer {sa['access_token']}"},
                json={
                    "slug": slug,
                    "name": "Load Test Clinic",
                    "plan": "pro",
                    "admin_email": f"admin@{slug}.example.com",
                    "admin_password": "loadtest-password-1",
                    "admin_name": "LT",
                },
            ),
            201,
        )
        tok = ok(
            c.post(
                "/v1/auth/login",
                json={
                    "email": f"admin@{slug}.example.com",
                    "password": "loadtest-password-1",
                    "tenant_slug": slug,
                },
            ),
            200,
        )
        h = {"Authorization": f"Bearer {tok['access_token']}"}
        svc = ok(
            c.post(
                "/v1/services",
                headers=h,
                json={"name": "Checkup", "duration_min": 30, "price_paise": 50000},
            ),
            201,
        )
        staff = ok(
            c.post(
                "/v1/staff", headers=h, json={"display_name": "Dr LT", "service_ids": [svc["id"]]}
            ),
            201,
        )
        ok(
            c.put(
                f"/v1/staff/{staff['id']}/working-hours",
                headers=h,
                json={
                    "items": [
                        {"weekday": d, "start_time": "09:00", "end_time": "17:00"} for d in range(7)
                    ]
                },
            ),
            200,
        )
        cust = ok(
            c.post(
                "/v1/customers",
                headers=h,
                json={
                    "name": "LT Customer",
                    "phone": "9000000002",
                    "email": f"c-{run}@example.com",
                },
            ),
            201,
        )
    day = (date.today() + timedelta(days=30)).isoformat()
    for key, value in {
        "BASE": BASE,
        "TOKEN": tok["access_token"],
        "SERVICE": svc["id"],
        "STAFF": staff["id"],
        "CUSTOMER": cust["id"],
        "DAY": day,
    }.items():
        print(f"{key}={value}")  # noqa: T201


if __name__ == "__main__":
    main()
