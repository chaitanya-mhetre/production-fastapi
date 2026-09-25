"""Database engines and sessions.

Tenant isolation, layer 2 (layer 1 = repositories always filter by tenant_id):
every transaction opened by a *tenant session* runs
    SELECT set_config('app.tenant_id', '<uuid>', true)
The third argument `true` makes the setting transaction-local, so it can never leak to the
next request that reuses the pooled connection. Postgres RLS policies read that setting.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import NullPool

from slotwise.config import Settings

TENANT_KEY = "tenant_id"


@event.listens_for(Session, "after_begin")
def _set_tenant_on_begin(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    tenant_id = session.info.get(TENANT_KEY)
    if tenant_id is not None:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )


def make_engine(url: str, settings: Settings, *, pooled: bool = True) -> AsyncEngine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if pooled:
        kwargs |= {"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow}
    else:
        kwargs["poolclass"] = NullPool
    return create_async_engine(url, **kwargs)


class Database:
    """Owns the engine + session factory. One instance per process (API or worker)."""

    def __init__(self, url: str, settings: Settings, *, pooled: bool = True) -> None:
        self.engine = make_engine(url, settings, pooled=pooled)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self, tenant_id: UUID | None = None) -> AsyncIterator[AsyncSession]:
        """A session. With tenant_id, every transaction in it is RLS-scoped to that tenant."""
        async with self.sessionmaker() as session:
            if tenant_id is not None:
                session.info[TENANT_KEY] = tenant_id
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


EXCLUSION_VIOLATION = "23P01"
UNIQUE_VIOLATION = "23505"


def sqlstate(exc: BaseException) -> str | None:
    """The Postgres error code behind a SQLAlchemy DBAPIError, e.g. '23P01'."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(code) if code else None
