from uuid import UUID

from sqlalchemy import select, tuple_

from slotwise.models import Customer
from slotwise.pagination import Cursor
from slotwise.repositories.base import TenantScopedRepository


class CustomerRepository(TenantScopedRepository):
    async def get(self, customer_id: UUID) -> Customer | None:
        result = await self.session.execute(
            select(Customer).where(Customer.id == customer_id, Customer.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def list(self, *, limit: int, cursor: Cursor | None = None) -> list[Customer]:
        stmt = select(Customer).where(Customer.tenant_id == self.tenant_id)
        if cursor:
            stmt = stmt.where(
                tuple_(Customer.created_at, Customer.id) > (cursor.created_at, cursor.id)
            )
        stmt = stmt.order_by(Customer.created_at, Customer.id).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    def add(self, *, name: str, phone: str, email: str | None) -> Customer:
        customer = Customer(tenant_id=self.tenant_id, name=name, phone=phone, email=email)
        self.session.add(customer)
        return customer

    async def delete(self, customer: Customer) -> None:
        await self.session.delete(customer)
