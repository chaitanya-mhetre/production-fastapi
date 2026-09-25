import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from slotwise.api.deps import DbDep, TenantSessionDep, require, require_superadmin, tenant_of
from slotwise.schemas.tenancy import (
    MemberCreateIn,
    MemberOut,
    TenantCreateIn,
    TenantOut,
    TenantUpdateIn,
)
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal
from slotwise.services.tenants import TenantService

router = APIRouter(prefix="/v1", tags=["tenants"])


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreateIn,
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: DbDep,
) -> TenantOut:
    tenant_id = uuid.uuid4()
    async with db.session(tenant_id=tenant_id) as session:
        tenant = await TenantService(session).create_tenant(
            principal,
            tenant_id=tenant_id,
            slug=body.slug,
            name=body.name,
            timezone=body.timezone,
            plan=body.plan,
            admin_email=body.admin_email,
            admin_password=body.admin_password,
            admin_name=body.admin_name,
        )
        await session.commit()
    return TenantOut.model_validate(tenant)


@router.get("/tenant")
async def get_tenant(
    principal: Annotated[Principal, Depends(require(P.TENANT_READ))], session: TenantSessionDep
) -> TenantOut:
    return TenantOut.model_validate(await TenantService(session).get(tenant_of(principal)))


@router.patch("/tenant")
async def update_tenant(
    body: TenantUpdateIn,
    principal: Annotated[Principal, Depends(require(P.TENANT_MANAGE))],
    session: TenantSessionDep,
) -> TenantOut:
    tenant = await TenantService(session).update(
        principal, tenant_of(principal), name=body.name, timezone=body.timezone
    )
    await session.commit()
    return TenantOut.model_validate(tenant)


@router.post("/tenant/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    body: MemberCreateIn,
    principal: Annotated[Principal, Depends(require(P.MEMBERS_MANAGE))],
    session: TenantSessionDep,
) -> MemberOut:
    user = await TenantService(session).add_member(
        principal,
        tenant_of(principal),
        email=body.email,
        full_name=body.full_name,
        password=body.password,
        role=body.role,
    )
    await session.commit()
    return MemberOut(user_id=user.id, email=user.email, full_name=user.full_name, role=body.role)


@router.get("/tenant/members")
async def list_members(
    principal: Annotated[Principal, Depends(require(P.MEMBERS_MANAGE))],
    session: TenantSessionDep,
) -> list[MemberOut]:
    members = await TenantService(session).list_members(tenant_of(principal))
    return [
        MemberOut(user_id=u.id, email=u.email, full_name=u.full_name, role=role)
        for role, u in members
    ]
