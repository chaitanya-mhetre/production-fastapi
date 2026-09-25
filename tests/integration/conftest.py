from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from slotwise.config import Settings, get_settings
from slotwise.db import Database
from slotwise.main import create_app
from slotwise.security.passwords import hash_password

SUPERADMIN_EMAIL = "root@slotwise.example.com"
SUPERADMIN_PASSWORD = "root-password-123"


@pytest.fixture(scope="session")
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def migrated(settings: Settings) -> Iterator[None]:
    cfg = Config("alembic.ini")
    cfg.attributes["url"] = settings.migration_database_url
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(scope="session")
async def owner_engine(settings: Settings, migrated: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.migration_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_db(owner_engine: AsyncEngine, settings: Settings) -> AsyncIterator[None]:
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()  # rate-limit counters and idempotency cache must not leak between tests
    await redis.aclose()
    async with owner_engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        )
        tables: list[str] = list(result.scalars())
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, is_superadmin) "
                "VALUES (gen_random_uuid(), :e, :p, 'Root', true)"
            ),
            {"e": SUPERADMIN_EMAIL, "p": hash_password(SUPERADMIN_PASSWORD)},
        )
    yield


@pytest.fixture(scope="session")
async def app(settings: Settings, migrated: None) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def db(app: FastAPI) -> Database:
    database: Database = app.state.db
    return database


@dataclass
class TenantCtx:
    id: str
    slug: str
    admin_token: str
    admin_email: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}


async def login(
    client: httpx.AsyncClient, email: str, password: str, slug: str | None = None
) -> str:
    body: dict[str, Any] = {"email": email, "password": password, "tenant_slug": slug}
    resp = await client.post("/v1/auth/login", json=body)
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


@pytest.fixture
async def superadmin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await login(client, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD)}"}


MakeTenant = Callable[..., Awaitable[TenantCtx]]


@pytest.fixture
def make_tenant(client: httpx.AsyncClient, superadmin_headers: dict[str, str]) -> MakeTenant:
    async def _make(slug: str, timezone: str = "Asia/Kolkata", plan: str = "pro") -> TenantCtx:
        email = f"admin@{slug}.example.com"
        resp = await client.post(
            "/v1/tenants",
            headers=superadmin_headers,
            json={
                "slug": slug,
                "name": slug.title(),
                "timezone": timezone,
                "plan": plan,
                "admin_email": email,
                "admin_password": "admin-password-1",
                "admin_name": "Admin",
            },
        )
        assert resp.status_code == 201, resp.text
        token = await login(client, email, "admin-password-1", slug)
        return TenantCtx(id=resp.json()["id"], slug=slug, admin_token=token, admin_email=email)

    return _make


@dataclass
class CatalogCtx:
    service_id: str
    staff_ids: list[str]


MakeCatalog = Callable[..., Awaitable[CatalogCtx]]


@pytest.fixture
def make_catalog(client: httpx.AsyncClient) -> MakeCatalog:
    """A 30-minute service with a 10-minute buffer, N staff working Mon–Fri 09:00–17:00."""

    async def _make(
        t: TenantCtx, staff_count: int = 1, duration: int = 30, buffer: int = 10
    ) -> CatalogCtx:
        svc = await client.post(
            "/v1/services",
            headers=t.headers,
            json={
                "name": "Consultation",
                "duration_min": duration,
                "price_paise": 50000,
                "buffer_min": buffer,
            },
        )
        assert svc.status_code == 201, svc.text
        staff_ids = []
        for i in range(staff_count):
            st = await client.post(
                "/v1/staff",
                headers=t.headers,
                json={"display_name": f"Dr {i}", "service_ids": [svc.json()["id"]]},
            )
            assert st.status_code == 201, st.text
            wh = await client.put(
                f"/v1/staff/{st.json()['id']}/working-hours",
                headers=t.headers,
                json={
                    "items": [
                        {"weekday": d, "start_time": "09:00", "end_time": "17:00"} for d in range(5)
                    ]
                },
            )
            assert wh.status_code == 200, wh.text
            staff_ids.append(st.json()["id"])
        return CatalogCtx(service_id=svc.json()["id"], staff_ids=staff_ids)

    return _make
