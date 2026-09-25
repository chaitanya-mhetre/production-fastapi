"""Global tables (no RLS): tenants, users, memberships."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.models import Role, Tenant, TenantMembership, User


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: UUID) -> Tenant | None:
        return await self.session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self.session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()

    def add(self, tenant: Tenant) -> None:
        self.session.add(tenant)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    def add(self, user: User) -> None:
        self.session.add(user)


class MembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: UUID, tenant_id: UUID) -> TenantMembership | None:
        return await self.session.get(TenantMembership, (user_id, tenant_id))

    async def list_for_tenant(self, tenant_id: UUID) -> list[tuple[TenantMembership, User]]:
        result = await self.session.execute(
            select(TenantMembership, User)
            .join(User, User.id == TenantMembership.user_id)
            .where(TenantMembership.tenant_id == tenant_id)
            .order_by(User.email)
        )
        return [(m, u) for m, u in result.tuples()]

    def add(self, user_id: UUID, tenant_id: UUID, role: Role) -> TenantMembership:
        membership = TenantMembership(user_id=user_id, tenant_id=tenant_id, role=role)
        self.session.add(membership)
        return membership
