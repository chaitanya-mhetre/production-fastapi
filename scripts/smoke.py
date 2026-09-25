"""End-to-end smoke test against a running deployment (local compose or AWS).

    SMOKE_BASE_URL=http://localhost:58088 SMOKE_SUPERADMIN_EMAIL=... SMOKE_SUPERADMIN_PASSWORD=... \
        uv run python scripts/smoke.py

Creates a throwaway tenant, books a slot, pays for it through the signed mock-payment webhook,
cancels it, and (optionally) waits for the confirmation email to show up in Mailpit, which
proves API → outbox → relay → Celery → SMTP works. Exit code != 0 on any failure, so CI can
use it as a post-deploy gate.
"""

import json
import os
import sys
import time
import uuid
from datetime import date, timedelta

import httpx

from slotwise.webhooks.signing import sign

BASE = os.environ.get("SMOKE_BASE_URL", "http://localhost:58088")
MAILPIT = os.environ.get("SMOKE_MAILPIT_API")  # e.g. http://localhost:58025/api/v1
SA_EMAIL = os.environ["SMOKE_SUPERADMIN_EMAIL"]
SA_PASSWORD = os.environ["SMOKE_SUPERADMIN_PASSWORD"]
MOCKPAY_SECRET = os.environ.get("SMOKE_MOCKPAY_SECRET", "dev-mockpay-secret")


def step(name: str) -> None:
    print(f"→ {name}", flush=True)  # noqa: T201


def check(resp: httpx.Response, expected: int) -> dict:  # type: ignore[type-arg]
    if resp.status_code != expected:
        sys.exit(f"FAILED {resp.request.method} {resp.request.url}: {resp.status_code} {resp.text}")
    return resp.json() if resp.content else {}


def main() -> None:
    run = uuid.uuid4().hex[:8]
    with httpx.Client(base_url=BASE, timeout=15) as c:
        step("readiness")
        check(c.get("/readyz"), 200)

        step("superadmin login + tenant")
        sa = check(c.post("/v1/auth/login", json={"email": SA_EMAIL, "password": SA_PASSWORD}), 200)
        slug = f"smoke-{run}"
        tenant = check(
            c.post(
                "/v1/tenants",
                headers={"Authorization": f"Bearer {sa['access_token']}"},
                json={
                    "slug": slug,
                    "name": "Smoke Clinic",
                    "plan": "pro",
                    "admin_email": f"admin@{slug}.example.com",
                    "admin_password": "smoke-password-1",
                    "admin_name": "Smoke",
                },
            ),
            201,
        )
        tok = check(
            c.post(
                "/v1/auth/login",
                json={
                    "email": f"admin@{slug}.example.com",
                    "password": "smoke-password-1",
                    "tenant_slug": slug,
                },
            ),
            200,
        )
        h = {"Authorization": f"Bearer {tok['access_token']}"}

        step("catalogue")
        svc = check(
            c.post(
                "/v1/services",
                headers=h,
                json={"name": "Checkup", "duration_min": 30, "price_paise": 50000},
            ),
            201,
        )
        staff = check(
            c.post(
                "/v1/staff",
                headers=h,
                json={"display_name": "Dr Smoke", "service_ids": [svc["id"]]},
            ),
            201,
        )
        check(
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
        email = f"customer-{run}@example.com"
        cust = check(
            c.post(
                "/v1/customers",
                headers=h,
                json={"name": "Smoke Customer", "phone": "9000000001", "email": email},
            ),
            201,
        )

        step("availability + booking")
        day = (date.today() + timedelta(days=7)).isoformat()
        slots = check(
            c.get("/v1/availability", headers=h, params={"service_id": svc["id"], "date": day}), 200
        )
        assert slots, "no availability"
        booking = check(
            c.post(
                "/v1/bookings",
                headers=h | {"Idempotency-Key": f"smoke-{run}"},
                json={
                    "service_id": svc["id"],
                    "staff_id": staff["id"],
                    "customer_id": cust["id"],
                    "start": slots[0]["start"],
                },
            ),
            201,
        )
        again = c.post(
            "/v1/bookings",
            headers=h | {"Idempotency-Key": f"smoke-{run}"},
            json={
                "service_id": svc["id"],
                "staff_id": staff["id"],
                "customer_id": cust["id"],
                "start": slots[0]["start"],
            },
        )
        assert again.headers.get("Idempotent-Replayed") == "true", "idempotent replay failed"

        step("signed payment webhook")
        raw = json.dumps(
            {
                "provider_ref": f"pay_{run}",
                "tenant_id": tenant["id"],
                "booking_id": booking["id"],
                "amount_paise": 50000,
                "status": "succeeded",
            }
        ).encode()
        paid = check(
            c.post(
                "/v1/webhooks/payments/mockpay",
                content=raw,
                headers={
                    "Mockpay-Signature": sign(MOCKPAY_SECRET, raw),
                    "Content-Type": "application/json",
                },
            ),
            200,
        )
        assert paid["booking_status"] == "confirmed", paid

        step("cancel")
        cancelled = check(
            c.post(
                f"/v1/bookings/{booking['id']}:cancel", headers=h, json={"reason": "smoke test"}
            ),
            200,
        )
        assert cancelled["status"] == "cancelled"

    if MAILPIT:
        step("waiting for email via outbox → relay → worker → SMTP")
        deadline = time.time() + 30
        while time.time() < deadline:
            msgs = httpx.get(f"{MAILPIT}/search", params={"query": f"to:{email}"}).json()
            if msgs.get("messages_count", 0) >= 3:  # created, confirmed, cancelled
                break
            time.sleep(1)
        else:
            sys.exit("FAILED: emails did not arrive within 30s")
    print("SMOKE OK")  # noqa: T201


if __name__ == "__main__":
    main()
