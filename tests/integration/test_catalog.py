"""M2 acceptance: catalogue CRUD, schedules, availability over the API."""

import httpx

from tests.integration.conftest import MakeCatalog, MakeTenant

FUTURE_MONDAY = "2030-01-07"  # far enough ahead that "no slots in the past" never interferes


async def test_availability_for_one_staff_day(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")  # Asia/Kolkata
    c = await make_catalog(t)
    resp = await client.get(
        "/v1/availability",
        headers=t.headers,
        params={"service_id": c.service_id, "date": FUTURE_MONDAY},
    )
    assert resp.status_code == 200
    slots = resp.json()
    # 09:00 IST = 03:30Z; last 30-min slot starts 16:30 IST = 11:00Z; 15-minute step.
    assert slots[0]["start"].startswith("2030-01-07T03:30:00")
    assert slots[-1]["start"].startswith("2030-01-07T11:00:00")
    assert len(slots) == 31


async def test_weekend_has_no_slots(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")
    c = await make_catalog(t)
    resp = await client.get(
        "/v1/availability",
        headers=t.headers,
        params={"service_id": c.service_id, "date": "2030-01-06"},
    )
    assert resp.json() == []


async def test_time_off_removes_slots(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")
    c = await make_catalog(t)
    staff = c.staff_ids[0]
    off = await client.post(
        f"/v1/staff/{staff}/time-off",
        headers=t.headers,
        json={
            "start": "2030-01-07T09:00:00+05:30",
            "end": "2030-01-07T13:00:00+05:30",
            "reason": "conference",
        },
    )
    assert off.status_code == 201
    slots = (
        await client.get(
            "/v1/availability",
            headers=t.headers,
            params={"service_id": c.service_id, "date": FUTURE_MONDAY},
        )
    ).json()
    assert slots[0]["start"].startswith("2030-01-07T07:30:00")  # 13:00 IST

    removed = await client.delete(
        f"/v1/staff/{staff}/time-off/{off.json()['id']}", headers=t.headers
    )
    assert removed.status_code == 204


async def test_new_york_tenant_across_dst(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("nyc", timezone="America/New_York")
    c = await make_catalog(t)
    winter = (
        await client.get(
            "/v1/availability",
            headers=t.headers,
            params={"service_id": c.service_id, "date": "2030-03-08"},
        )
    ).json()
    summer = (
        await client.get(
            "/v1/availability",
            headers=t.headers,
            params={"service_id": c.service_id, "date": "2030-03-11"},
        )
    ).json()
    assert winter[0]["start"].startswith("2030-03-08T14:00:00")  # EST
    assert summer[0]["start"].startswith("2030-03-11T13:00:00")  # EDT


async def test_staff_filter_and_multiple_staff(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")
    c = await make_catalog(t, staff_count=3)
    params = {"service_id": c.service_id, "date": FUTURE_MONDAY}
    all_slots = (await client.get("/v1/availability", headers=t.headers, params=params)).json()
    assert len(all_slots) == 31 * 3
    one = (
        await client.get(
            "/v1/availability", headers=t.headers, params=params | {"staff_id": c.staff_ids[1]}
        )
    ).json()
    assert {s["staff_id"] for s in one} == {c.staff_ids[1]}


async def test_overlapping_working_hours_rejected(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")
    c = await make_catalog(t)
    resp = await client.put(
        f"/v1/staff/{c.staff_ids[0]}/working-hours",
        headers=t.headers,
        json={
            "items": [
                {"weekday": 0, "start_time": "09:00", "end_time": "13:00"},
                {"weekday": 0, "start_time": "12:00", "end_time": "17:00"},
            ]
        },
    )
    assert resp.status_code == 422


async def test_cannot_assign_another_tenants_service(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    a, b = await make_tenant("alpha"), await make_tenant("bravo")
    cb = await make_catalog(b)
    resp = await client.post(
        "/v1/staff", headers=a.headers, json={"display_name": "X", "service_ids": [cb.service_id]}
    )
    assert resp.status_code == 422
    assert (await client.get(f"/v1/services/{cb.service_id}", headers=a.headers)).status_code == 404


async def test_deactivated_service_has_no_availability(
    client: httpx.AsyncClient, make_tenant: MakeTenant, make_catalog: MakeCatalog
) -> None:
    t = await make_tenant("acme")
    c = await make_catalog(t)
    await client.patch(f"/v1/services/{c.service_id}", headers=t.headers, json={"active": False})
    resp = await client.get(
        "/v1/availability",
        headers=t.headers,
        params={"service_id": c.service_id, "date": FUTURE_MONDAY},
    )
    assert resp.status_code == 404
