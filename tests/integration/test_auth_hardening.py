"""M4 acceptance: refresh rotation + reuse detection, API keys + scopes, rate limits, quotas."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest

from slotwise.config import Settings
from tests.integration.conftest import MakeCatalog, MakeTenant, TenantCtx


async def _login_full(client: httpx.AsyncClient, t: TenantCtx) -> dict[str, Any]:
    resp = await client.post(
        "/v1/auth/login",
        json={"email": t.admin_email, "password": "admin-password-1", "tenant_slug": t.slug},
    )
    assert resp.status_code == 200
    data: dict[str, Any] = resp.json()
    return data


# --- refresh tokens ---------------------------------------------------------------------------


async def test_refresh_rotates_tokens(client: httpx.AsyncClient, make_tenant: MakeTenant) -> None:
    t = await make_tenant("acme")
    first = await _login_full(client, t)
    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200
    new = rotated.json()
    assert new["refresh_token"] != first["refresh_token"]
    me = await client.get("/v1/tenant", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 200


async def test_reusing_a_rotated_token_revokes_the_whole_family(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    stolen = (await _login_full(client, t))["refresh_token"]
    legit = (await client.post("/v1/auth/refresh", json={"refresh_token": stolen})).json()

    replay = await client.post("/v1/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reused"

    # The descendant token issued to the legitimate client is now dead too.
    after = await client.post("/v1/auth/refresh", json={"refresh_token": legit["refresh_token"]})
    assert after.status_code == 401


async def test_other_login_sessions_survive_a_family_revocation(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    phone, laptop = await _login_full(client, t), await _login_full(client, t)
    await client.post("/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    await client.post("/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]})  # reuse
    ok = await client.post("/v1/auth/refresh", json={"refresh_token": laptop["refresh_token"]})
    assert ok.status_code == 200


async def test_logout_revokes(client: httpx.AsyncClient, make_tenant: MakeTenant) -> None:
    t = await make_tenant("acme")
    tokens = await _login_full(client, t)
    assert (
        await client.post("/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 204
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401


# --- JWT tampering ----------------------------------------------------------------------------


async def test_tampered_expired_and_alg_none_tokens_rejected(
    client: httpx.AsyncClient, make_tenant: MakeTenant, settings: Settings
) -> None:
    t = await make_tenant("acme")
    claims = jwt.decode(t.admin_token, options={"verify_signature": False})
    forged = jwt.encode(
        claims | {"role": "tenant_admin", "tid": str(uuid.uuid4())},
        "wrong-secret-wrong-secret-wrong-secret",
        algorithm="HS256",
    )
    expired = jwt.encode(
        claims | {"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
        settings.jwt_secret,
        algorithm="HS256",
    )
    header = jwt.utils.base64url_encode(b'{"alg":"none","typ":"JWT"}').decode()
    body = jwt.utils.base64url_encode(json.dumps(claims).encode()).decode()
    none_alg = f"{header}.{body}."
    for token in (forged, expired, none_alg, "garbage"):
        resp = await client.get("/v1/tenant", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401, token


# --- API keys -------------------------------------------------------------------------------


async def _key(
    client: httpx.AsyncClient, t: TenantCtx, scopes: list[str], rate: int = 600
) -> dict[str, Any]:
    resp = await client.post(
        "/v1/api-keys",
        headers=t.headers,
        json={"name": "integration", "scopes": scopes, "rate_limit_per_min": rate},
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def test_api_key_auth_and_scopes(client: httpx.AsyncClient, make_tenant: MakeTenant) -> None:
    t = await make_tenant("acme")
    key = await _key(client, t, ["customers:read"])
    assert key["key"].startswith(f"sw_{key['prefix']}_")
    headers = {"X-API-Key": key["key"]}
    assert (await client.get("/v1/customers", headers=headers)).status_code == 200
    denied = await client.post(
        "/v1/customers", headers=headers, json={"name": "X", "phone": "9000000001"}
    )
    assert denied.status_code == 403

    listed = (await client.get("/v1/api-keys", headers=t.headers)).json()
    assert "key" not in listed[0] and listed[0]["prefix"] == key["prefix"]


async def test_admin_scopes_are_not_grantable(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    resp = await client.post(
        "/v1/api-keys", headers=t.headers, json={"name": "evil", "scopes": ["api_keys:manage"]}
    )
    assert resp.status_code == 422


async def test_revoked_and_malformed_keys_rejected(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    key = await _key(client, t, ["customers:read"])
    assert (await client.delete(f"/v1/api-keys/{key['id']}", headers=t.headers)).status_code == 204
    for raw in (key["key"], "sw_deadbeef_nope", "not-a-key", key["key"][:-1] + "x"):
        assert (await client.get("/v1/customers", headers={"X-API-Key": raw})).status_code == 401


async def test_api_key_cannot_cross_tenants(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    a, b = await make_tenant("alpha"), await make_tenant("bravo")
    cust = await client.post(
        "/v1/customers", headers=b.headers, json={"name": "B", "phone": "9000000002"}
    )
    key = await _key(client, a, ["customers:read"])
    resp = await client.get(f"/v1/customers/{cust.json()['id']}", headers={"X-API-Key": key["key"]})
    assert resp.status_code == 404


# --- rate limits and quotas ---------------------------------------------------------------------


async def test_api_key_rate_limit_returns_429_with_headers(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    key = await _key(client, t, ["customers:read"], rate=3)
    headers = {"X-API-Key": key["key"]}
    responses = [await client.get("/v1/customers", headers=headers) for _ in range(4)]
    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    assert responses[0].headers["X-RateLimit-Limit"] == "3"
    assert responses[1].headers["X-RateLimit-Remaining"] == "1"
    assert int(responses[3].headers["Retry-After"]) > 0


async def test_login_brute_force_is_throttled(
    client: httpx.AsyncClient, make_tenant: MakeTenant, settings: Settings
) -> None:
    t = await make_tenant("acme")
    codes = [
        (
            await client.post(
                "/v1/auth/login",
                json={"email": t.admin_email, "password": "wrong", "tenant_slug": "acme"},
            )
        ).status_code
        for _ in range(settings.login_attempts_per_min)
    ]
    # make_tenant already logged in once, so the limit is hit one attempt early
    assert codes[:-1] == [401] * (settings.login_attempts_per_min - 1) and codes[-1] == 429


async def test_monthly_booking_quota(
    client: httpx.AsyncClient,
    make_tenant: MakeTenant,
    make_catalog: MakeCatalog,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "monthly_bookings_free", 2)
    t = await make_tenant("freebie", plan="free")
    c = await make_catalog(t)
    cust = (
        await client.post(
            "/v1/customers", headers=t.headers, json={"name": "Q", "phone": "9000000003"}
        )
    ).json()["id"]
    codes = []
    for hour in (10, 11, 12):
        resp = await client.post(
            "/v1/bookings",
            headers=t.headers | {"Idempotency-Key": f"quota-{hour}-key"},
            json={
                "service_id": c.service_id,
                "staff_id": c.staff_ids[0],
                "customer_id": cust,
                "start": f"2030-01-07T{hour}:00:00+05:30",
            },
        )
        codes.append(resp.status_code)
    assert codes == [201, 201, 429]
    assert resp.json()["error"]["code"] == "quota_exceeded"
