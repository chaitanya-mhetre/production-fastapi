from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from slotwise.api.deps import TenantSessionDep, require, tenant_of
from slotwise.schemas.common import Page
from slotwise.schemas.customers import CustomerIn, CustomerOut
from slotwise.security.permissions import Permission as P
from slotwise.security.principal import Principal
from slotwise.services.customers import CustomerService

router = APIRouter(prefix="/v1/customers", tags=["customers"])

Reader = Annotated[Principal, Depends(require(P.CUSTOMERS_READ))]
Writer = Annotated[Principal, Depends(require(P.CUSTOMERS_WRITE))]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_customer(
    body: CustomerIn, principal: Writer, session: TenantSessionDep
) -> CustomerOut:
    customer = await CustomerService(session, tenant_of(principal)).create(
        principal, name=body.name, phone=body.phone, email=body.email
    )
    await session.commit()
    return CustomerOut.model_validate(customer)


@router.get("")
async def list_customers(
    principal: Reader,
    session: TenantSessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[CustomerOut]:
    rows, next_cursor = await CustomerService(session, tenant_of(principal)).page(
        limit=limit, cursor=cursor
    )
    return Page(items=[CustomerOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/{customer_id}")
async def get_customer(
    customer_id: UUID, principal: Reader, session: TenantSessionDep
) -> CustomerOut:
    return CustomerOut.model_validate(
        await CustomerService(session, tenant_of(principal)).get(customer_id)
    )


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(
    customer_id: UUID, principal: Writer, session: TenantSessionDep
) -> Response:
    await CustomerService(session, tenant_of(principal)).delete(principal, customer_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
