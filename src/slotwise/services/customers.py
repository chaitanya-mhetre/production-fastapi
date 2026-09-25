from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.errors import Conflict, NotFound
from slotwise.models import Customer
from slotwise.pagination import Cursor
from slotwise.repositories.audit import AuditRepository
from slotwise.repositories.customers import CustomerRepository
from slotwise.security.principal import Principal


class CustomerService:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.repo = CustomerRepository(session, tenant_id)
        self.audit = AuditRepository(session, tenant_id)

    async def create(
        self, principal: Principal, *, name: str, phone: str, email: str | None
    ) -> Customer:
        customer = self.repo.add(name=name, phone=phone, email=email)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise Conflict("a customer with this phone already exists") from exc
        self.audit.record(
            principal, action="customer.created", entity="customer", entity_id=customer.id
        )
        return customer

    async def get(self, customer_id: UUID) -> Customer:
        customer = await self.repo.get(customer_id)
        if customer is None:
            raise NotFound("customer not found")
        return customer

    async def list(self, *, limit: int, cursor: str | None) -> tuple[list[Customer], str | None]:
        rows = await self.repo.list(
            limit=limit + 1, cursor=Cursor.decode(cursor) if cursor else None
        )
        next_cursor = (
            Cursor(rows[limit - 1].created_at, rows[limit - 1].id).encode()
            if len(rows) > limit
            else None
        )
        return rows[:limit], next_cursor

    async def delete(self, principal: Principal, customer_id: UUID) -> None:
        customer = await self.get(customer_id)
        await self.repo.delete(customer)
        self.audit.record(
            principal, action="customer.deleted", entity="customer", entity_id=customer_id
        )
