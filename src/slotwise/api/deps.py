"""FastAPI dependencies: DI for settings, DB sessions, the current principal, permission checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings, get_settings
from slotwise.context import tenant_id_var
from slotwise.db import Database
from slotwise.errors import Forbidden, Unauthorized
from slotwise.models import Role
from slotwise.security.permissions import Permission
from slotwise.security.principal import Principal
from slotwise.security.tokens import decode_access_token

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DbDep = Annotated[Database, Depends(get_db)]


async def get_principal(request: Request, settings: SettingsDep, db: DbDep) -> Principal:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token")
    claims = decode_access_token(settings, token)
    principal = Principal(
        actor_type="user",
        actor_id=UUID(claims["sub"]),
        tenant_id=UUID(claims["tid"]) if claims.get("tid") else None,
        role=Role(claims["role"]) if claims.get("role") else None,
        is_superadmin=bool(claims.get("sa")),
    )
    if principal.tenant_id is not None:
        tenant_id_var.set(principal.tenant_id)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: `principal: Annotated[Principal, Depends(require(P.X))]`."""

    async def checker(principal: PrincipalDep) -> Principal:
        if principal.tenant_id is None:
            raise Forbidden("a tenant-scoped token is required")
        if not principal.can(permission):
            raise Forbidden(f"missing permission {permission.value}")
        return principal

    return checker


async def require_superadmin(principal: PrincipalDep) -> Principal:
    if not principal.is_superadmin:
        raise Forbidden("superadmin only")
    return principal


async def get_session(db: DbDep) -> AsyncIterator[AsyncSession]:
    """Unscoped session: only for global tables (login, tenant creation)."""
    async with db.session() as session:
        yield session


async def get_tenant_session(principal: PrincipalDep, db: DbDep) -> AsyncIterator[AsyncSession]:
    """RLS-scoped session for the principal's tenant. Routes commit explicitly."""
    if principal.tenant_id is None:
        raise Forbidden("a tenant-scoped token is required")
    async with db.session(tenant_id=principal.tenant_id) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


def tenant_of(principal: Principal) -> UUID:
    if principal.tenant_id is None:  # require() already guarantees this; keeps mypy honest
        raise Forbidden("a tenant-scoped token is required")
    return principal.tenant_id
