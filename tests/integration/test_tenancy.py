"""M1 acceptance: login, tenant lifecycle, RBAC, and cross-tenant isolation at both layers."""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from slotwise.db import Database
from tests.integration.conftest import MakeTenant, login


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["postgres"] == "ok"


async def test_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"x-request-id": "abc123"})
    assert resp.headers["x-request-id"] == "abc123"


async def test_login_rejects_bad_password_and_unknown_email_identically(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    bad_pw = await client.post(
        "/v1/auth/login", json={"email": t.admin_email, "password": "nope", "tenant_slug": "acme"}
    )
    unknown = await client.post(
        "/v1/auth/login",
        json={"email": "ghost@x.example.com", "password": "nope", "tenant_slug": "acme"},
    )
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()  # no user-enumeration signal


async def test_tenant_creation_requires_superadmin(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    resp = await client.post("/v1/tenants", headers=t.headers, json={})
    assert resp.status_code in (403, 422)
    resp = await client.post(
        "/v1/tenants",
        headers=t.headers,
        json={
            "slug": "evil",
            "name": "E",
            "admin_email": "e@e.example.com",
            "admin_password": "0123456789",
            "admin_name": "E",
        },
    )
    assert resp.status_code == 403


async def test_duplicate_slug_conflicts(
    client: httpx.AsyncClient, make_tenant: MakeTenant, superadmin_headers: dict[str, str]
) -> None:
    await make_tenant("acme")
    resp = await client.post(
        "/v1/tenants",
        headers=superadmin_headers,
        json={
            "slug": "acme",
            "name": "A",
            "admin_email": "x@x.example.com",
            "admin_password": "0123456789",
            "admin_name": "X",
        },
    )
    assert resp.status_code == 409


async def test_get_and_update_own_tenant(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    resp = await client.patch("/v1/tenant", headers=t.headers, json={"name": "Acme Clinic"})
    assert resp.status_code == 200 and resp.json()["name"] == "Acme Clinic"
    bad_tz = await client.patch("/v1/tenant", headers=t.headers, json={"timezone": "Mars/Base"})
    assert bad_tz.status_code == 422


async def test_rbac_staff_cannot_write_customers(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    resp = await client.post(
        "/v1/tenant/members",
        headers=t.headers,
        json={
            "email": "staff@acme.example.com",
            "full_name": "S",
            "password": "staff-password-1",
            "role": "staff",
        },
    )
    assert resp.status_code == 201
    token = await login(client, "staff@acme.example.com", "staff-password-1", "acme")
    staff = {"Authorization": f"Bearer {token}"}
    denied = await client.post(
        "/v1/customers", headers=staff, json={"name": "C", "phone": "9999999999"}
    )
    assert denied.status_code == 403
    assert (await client.get("/v1/tenant/members", headers=staff)).status_code == 403


async def test_customer_crud_and_pagination(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    t = await make_tenant("acme")
    for i in range(5):
        r = await client.post(
            "/v1/customers", headers=t.headers, json={"name": f"C{i}", "phone": f"90000000{i:02d}"}
        )
        assert r.status_code == 201
    dup = await client.post(
        "/v1/customers", headers=t.headers, json={"name": "X", "phone": "9000000000"}
    )
    assert dup.status_code == 409

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/v1/customers", headers=t.headers, params=params)).json()
        seen += [c["name"] for c in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"C{i}" for i in range(5)]


async def test_cross_tenant_access_returns_404(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    a, b = await make_tenant("alpha"), await make_tenant("bravo")
    created = await client.post(
        "/v1/customers", headers=b.headers, json={"name": "Bravo's", "phone": "9111111111"}
    )
    bravo_customer = created.json()["id"]

    assert (
        await client.get(f"/v1/customers/{bravo_customer}", headers=a.headers)
    ).status_code == 404
    assert (
        await client.delete(f"/v1/customers/{bravo_customer}", headers=a.headers)
    ).status_code == 404
    listed = (await client.get("/v1/customers", headers=a.headers)).json()["items"]
    assert listed == []


async def test_rls_hides_rows_even_without_where_clause(
    client: httpx.AsyncClient, make_tenant: MakeTenant, db: Database
) -> None:
    """Layer 2 proof: raw SQL with NO tenant filter, as the app role, still sees only one tenant."""
    a, b = await make_tenant("alpha"), await make_tenant("bravo")
    await client.post(
        "/v1/customers", headers=a.headers, json={"name": "A1", "phone": "9000000001"}
    )
    await client.post(
        "/v1/customers", headers=b.headers, json={"name": "B1", "phone": "9000000002"}
    )

    async with db.session(tenant_id=uuid.UUID(a.id)) as session:
        names: list[str] = list(
            (await session.execute(text("SELECT name FROM customers"))).scalars()
        )
    assert names == ["A1"]

    async with db.session() as session:  # no tenant set at all → nothing visible
        count: int = (await session.execute(text("SELECT count(*) FROM customers"))).scalar_one()
    assert count == 0


async def test_rls_blocks_writing_into_another_tenant(
    make_tenant: MakeTenant, db: Database
) -> None:
    a, b = await make_tenant("alpha"), await make_tenant("bravo")
    async with db.session(tenant_id=uuid.UUID(a.id)) as session:
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, name, phone) "
                    "VALUES (gen_random_uuid(), :t, 'sneaky', '9000000009')"
                ),
                {"t": b.id},
            )


async def test_audit_log_written_in_same_transaction(
    client: httpx.AsyncClient, make_tenant: MakeTenant, db: Database
) -> None:
    t = await make_tenant("acme")
    await client.post(
        "/v1/customers",
        headers=t.headers,
        json={"name": "C", "phone": "9000000003"},
        params={},
    )
    async with db.session(tenant_id=uuid.UUID(t.id)) as session:
        actions: list[str] = list(
            (await session.execute(text("SELECT action FROM audit_logs ORDER BY id")))
            .scalars()
            .all()
        )
    assert actions == ["tenant.created", "customer.created"]
