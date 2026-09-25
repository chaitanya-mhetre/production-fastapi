"""M3 acceptance: double-booking prevention under concurrency, optimistic locking, idempotency."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from slotwise.db import Database
from tests.integration.conftest import CatalogCtx, MakeCatalog, MakeTenant, TenantCtx

SLOT = "2030-01-07T10:00:00+05:30"  # a Monday, inside 09:00–17:00 IST


async def _customer(client: httpx.AsyncClient, t: TenantCtx, phone: str = "9876543210") -> str:
    resp = await client.post(
        "/v1/customers", headers=t.headers, json={"name": "Asha", "phone": phone}
    )
    assert resp.status_code == 201, resp.text
    cid: str = resp.json()["id"]
    return cid


def _body(c: CatalogCtx, customer: str, start: str = SLOT, staff: int = 0) -> dict[str, Any]:
    return {
        "service_id": c.service_id,
        "staff_id": c.staff_ids[staff],
        "customer_id": customer,
        "start": start,
    }


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}


@pytest.fixture
async def setup(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> tuple[TenantCtx, CatalogCtx, str]:
    t = await make_tenant("acme")
    c = await make_catalog(t)
    return t, c, await _customer(client, t)


async def test_create_booking(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    resp = await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    assert resp.status_code == 201, resp.text
    b = resp.json()
    assert b["status"] == "pending_payment" and b["version"] == 1
    assert b["starts_at"].startswith("2030-01-07T04:30:00") and b["ends_at"].startswith(
        "2030-01-07T05:00:00"
    )


async def test_idempotency_key_is_required(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    resp = await client.post("/v1/bookings", headers=t.headers, json=_body(c, cust))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "idempotency_key_required"


async def test_same_slot_twice_is_409(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    assert (
        await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    ).status_code == 201
    again = await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    assert again.status_code == 409 and again.json()["error"]["code"] == "slot_taken"


async def test_buffer_is_enforced_by_the_database(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup  # 30 min service + 10 min buffer → 10:00 booking reserves 10:00–10:40
    await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    inside_buffer = await client.post(
        "/v1/bookings", headers=t.headers | _key(), json=_body(c, cust, "2030-01-07T10:35:00+05:30")
    )
    after_buffer = await client.post(
        "/v1/bookings", headers=t.headers | _key(), json=_body(c, cust, "2030-01-07T10:40:00+05:30")
    )
    assert inside_buffer.status_code == 409
    assert after_buffer.status_code == 201


async def test_concurrent_requests_for_one_slot_exactly_one_wins(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    responses = await asyncio.gather(
        *[
            client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
            for _ in range(20)
        ]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 19, codes


async def test_different_staff_same_time_is_fine(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")
    c = await make_catalog(t, staff_count=2)
    cust = await _customer(client, t)
    for staff in (0, 1):
        r = await client.post(
            "/v1/bookings", headers=t.headers | _key(), json=_body(c, cust, staff=staff)
        )
        assert r.status_code == 201


@pytest.mark.parametrize(
    ("start", "code"),
    [
        ("2030-01-07T08:30:00+05:30", "outside_working_hours"),
        ("2030-01-07T16:45:00+05:30", "outside_working_hours"),  # would end 17:15
        ("2030-01-06T10:00:00+05:30", "outside_working_hours"),  # Sunday
        ("2020-01-06T10:00:00+05:30", "validation_failed"),  # past
    ],
)
async def test_rejected_times(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str], start: str, code: str
) -> None:
    t, c, cust = setup
    resp = await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust, start))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code


async def test_time_off_blocks_booking(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    await client.post(
        f"/v1/staff/{c.staff_ids[0]}/time-off",
        headers=t.headers,
        json={"start": "2030-01-07T09:45:00+05:30", "end": "2030-01-07T10:15:00+05:30"},
    )
    resp = await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "staff_unavailable"


async def test_booked_slot_disappears_from_availability(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    slots = (
        await client.get(
            "/v1/availability",
            headers=t.headers,
            params={"service_id": c.service_id, "date": "2030-01-07"},
        )
    ).json()
    starts = {s["start"][11:16] for s in slots}  # UTC HH:MM
    # booking reserves 04:30–05:10Z; candidates 04:00Z (ends+buffer 04:40) .. 05:00Z blocked
    assert {"04:00", "04:15", "04:30", "04:45", "05:00"}.isdisjoint(starts)
    assert "03:45" in starts and "05:15" in starts


async def test_reschedule_with_optimistic_locking(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    b = (await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))).json()
    moved = await client.post(
        f"/v1/bookings/{b['id']}:reschedule",
        headers=t.headers,
        json={"start": "2030-01-07T12:00:00+05:30", "version": 1},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["version"] == 2
    stale = await client.post(
        f"/v1/bookings/{b['id']}:reschedule",
        headers=t.headers,
        json={"start": "2030-01-07T13:00:00+05:30", "version": 1},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"


async def test_reschedule_into_taken_slot(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    b2 = (
        await client.post(
            "/v1/bookings",
            headers=t.headers | _key(),
            json=_body(c, cust, "2030-01-07T12:00:00+05:30"),
        )
    ).json()
    resp = await client.post(
        f"/v1/bookings/{b2['id']}:reschedule", headers=t.headers, json={"start": SLOT, "version": 1}
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "slot_taken"


async def test_cancel_frees_the_slot_and_is_repeatable(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    b = (await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))).json()
    first = await client.post(
        f"/v1/bookings/{b['id']}:cancel", headers=t.headers, json={"reason": "sick"}
    )
    second = await client.post(f"/v1/bookings/{b['id']}:cancel", headers=t.headers)
    assert first.json()["status"] == second.json()["status"] == "cancelled"
    rebook = await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    assert rebook.status_code == 201
    stuck = await client.post(
        f"/v1/bookings/{b['id']}:reschedule", headers=t.headers, json={"start": SLOT, "version": 2}
    )
    assert stuck.status_code == 409 and stuck.json()["error"]["code"] == "invalid_booking_state"


async def test_list_bookings_with_filters_and_cursor(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    for hour in (10, 11, 12, 13):
        await client.post(
            "/v1/bookings",
            headers=t.headers | _key(),
            json=_body(c, cust, f"2030-01-07T{hour}:00:00+05:30"),
        )
    page1 = (await client.get("/v1/bookings", headers=t.headers, params={"limit": 3})).json()
    page2 = (
        await client.get(
            "/v1/bookings", headers=t.headers, params={"limit": 3, "cursor": page1["next_cursor"]}
        )
    ).json()
    assert len(page1["items"]) == 3 and len(page2["items"]) == 1 and page2["next_cursor"] is None
    filtered = (
        await client.get("/v1/bookings", headers=t.headers, params={"from": "2030-01-07T06:00:00Z"})
    ).json()
    assert len(filtered["items"]) == 2  # 12:00 and 13:00 IST


# --- idempotency ---------------------------------------------------------------------------


async def test_replay_returns_same_response_without_second_booking(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str], db: Database
) -> None:
    t, c, cust = setup
    key = _key()
    first = await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    second = await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    async with db.session(tenant_id=uuid.UUID(t.id)) as s:
        assert (await s.execute(text("SELECT count(*) FROM bookings"))).scalar_one() == 1


async def test_replay_survives_redis_cache_loss(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str], app: Any
) -> None:
    t, c, cust = setup
    key = _key()
    first = await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    await app.state.redis.flushdb()  # Postgres is the source of truth
    second = await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    assert second.json() == first.json() and second.headers.get("Idempotent-Replayed") == "true"


async def test_same_key_different_body_is_422(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    key = _key()
    await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    other = await client.post(
        "/v1/bookings", headers=t.headers | key, json=_body(c, cust, "2030-01-07T12:00:00+05:30")
    )
    assert other.status_code == 422 and other.json()["error"]["code"] == "idempotency_key_reused"


async def test_in_flight_key_is_409(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str], db: Database
) -> None:
    from slotwise.services.idempotency import request_fingerprint

    t, c, cust = setup
    body = _body(c, cust)
    fp = request_fingerprint(
        "POST", "/v1/bookings", {**body, "start": "2030-01-07T10:00:00+05:30", "notes": None}
    )
    async with db.session(tenant_id=uuid.UUID(t.id)) as s:
        await s.execute(
            text(
                "INSERT INTO idempotency_keys (tenant_id, key, request_hash, locked_until) "
                "VALUES (:t, 'inflight-key-1', :h, now() + interval '30 seconds')"
            ),
            {"t": t.id, "h": fp},
        )
        await s.commit()
    resp = await client.post(
        "/v1/bookings", headers=t.headers | {"Idempotency-Key": "inflight-key-1"}, json=body
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "idempotency_in_progress"

    async with db.session(tenant_id=uuid.UUID(t.id)) as s:  # lock expires → retry proceeds
        await s.execute(
            text("UPDATE idempotency_keys SET locked_until = now() - interval '1 second'")
        )
        await s.commit()
    retry = await client.post(
        "/v1/bookings", headers=t.headers | {"Idempotency-Key": "inflight-key-1"}, json=body
    )
    assert retry.status_code == 201, retry.text


async def test_business_error_is_stored_and_replayed(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str]
) -> None:
    t, c, cust = setup
    await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))
    key = _key()
    lost = await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    replay = await client.post("/v1/bookings", headers=t.headers | key, json=_body(c, cust))
    assert lost.status_code == replay.status_code == 409
    assert replay.headers.get("Idempotent-Replayed") == "true"


async def test_other_tenant_cannot_see_booking(
    client: httpx.AsyncClient, setup: tuple[TenantCtx, CatalogCtx, str], make_tenant: MakeTenant
) -> None:
    t, c, cust = setup
    b = (await client.post("/v1/bookings", headers=t.headers | _key(), json=_body(c, cust))).json()
    other = await make_tenant("bravo")
    assert (await client.get(f"/v1/bookings/{b['id']}", headers=other.headers)).status_code == 404
    cancel = await client.post(f"/v1/bookings/{b['id']}:cancel", headers=other.headers)
    assert cancel.status_code == 404
