from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from slotwise.api.deps import TenantSessionDep, require, tenant_of
from slotwise.schemas.api_keys import ApiKeyCreatedOut, ApiKeyIn, ApiKeyOut
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal
from slotwise.services.api_keys import ApiKeyService

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])

Admin = Annotated[Principal, Depends(require(P.API_KEYS_MANAGE))]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyIn, principal: Admin, session: TenantSessionDep
) -> ApiKeyCreatedOut:
    key, raw = await ApiKeyService(session).create(
        principal,
        tenant_of(principal),
        name=body.name,
        scopes=body.scopes,
        rate_limit_per_min=body.rate_limit_per_min,
    )
    await session.commit()
    return ApiKeyCreatedOut.model_validate(
        {**ApiKeyOut.model_validate(key).model_dump(), "key": raw}
    )


@router.get("")
async def list_api_keys(principal: Admin, session: TenantSessionDep) -> list[ApiKeyOut]:
    return [
        ApiKeyOut.model_validate(k)
        for k in await ApiKeyService(session).find_all(tenant_of(principal))
    ]


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(key_id: UUID, principal: Admin, session: TenantSessionDep) -> Response:
    await ApiKeyService(session).revoke(principal, tenant_of(principal), key_id)
    await session.commit()
    return Response(status_code=204)
