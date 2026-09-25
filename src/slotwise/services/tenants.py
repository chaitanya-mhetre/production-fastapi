"""Tenant lifecycle and membership management."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.errors import Conflict, NotFound
from slotwise.models import Role, Tenant, TenantPlan, TenantStatus, User
from slotwise.repositories.audit import AuditRepository
from slotwise.repositories.tenancy import MembershipRepository, TenantRepository, UserRepository
from slotwise.security.passwords import hash_password
from slotwise.security.principal import Principal


class TenantService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tenants = TenantRepository(session)
        self.users = UserRepository(session)
        self.memberships = MembershipRepository(session)

    async def create_tenant(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        slug: str,
        name: str,
        timezone: str,
        plan: TenantPlan,
        admin_email: str,
        admin_password: str,
        admin_name: str,
    ) -> Tenant:
        if await self.tenants.get_by_slug(slug):
            raise Conflict(f"tenant slug {slug!r} is taken")
        # The id is chosen by the caller so the session can be RLS-scoped to the new tenant
        # *before* the transaction starts (the audit row below is subject to RLS).
        tenant = Tenant(
            id=tenant_id,
            slug=slug,
            name=name,
            timezone=timezone,
            plan=plan,
            status=TenantStatus.ACTIVE,
        )
        self.tenants.add(tenant)
        admin = await self._get_or_create_user(admin_email, admin_password, admin_name)
        await self.session.flush()
        self.memberships.add(admin.id, tenant.id, Role.TENANT_ADMIN)
        AuditRepository(self.session, tenant.id).record(
            principal, action="tenant.created", entity="tenant", entity_id=tenant.id
        )
        await self._flush_or_conflict()
        return tenant

    async def get(self, tenant_id: UUID) -> Tenant:
        tenant = await self.tenants.get(tenant_id)
        if tenant is None:
            raise NotFound("tenant not found")
        return tenant

    async def update(
        self, principal: Principal, tenant_id: UUID, *, name: str | None, timezone: str | None
    ) -> Tenant:
        tenant = await self.get(tenant_id)
        before = {"name": tenant.name, "timezone": tenant.timezone}
        if name is not None:
            tenant.name = name
        if timezone is not None:
            tenant.timezone = timezone
        AuditRepository(self.session, tenant_id).record(
            principal,
            action="tenant.updated",
            entity="tenant",
            entity_id=tenant_id,
            diff={"before": before, "after": {"name": tenant.name, "timezone": tenant.timezone}},
        )
        return tenant

    async def add_member(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        email: str,
        full_name: str,
        password: str,
        role: Role,
    ) -> User:
        user = await self._get_or_create_user(email, password, full_name)
        await self.session.flush()
        if await self.memberships.get(user.id, tenant_id):
            raise Conflict("user is already a member of this tenant")
        self.memberships.add(user.id, tenant_id, role)
        AuditRepository(self.session, tenant_id).record(
            principal,
            action="member.added",
            entity="user",
            entity_id=user.id,
            diff={"role": role.value},
        )
        await self._flush_or_conflict()
        return user

    async def list_members(self, tenant_id: UUID) -> list[tuple[Role, User]]:
        return [(m.role, u) for m, u in await self.memberships.list_for_tenant(tenant_id)]

    async def _get_or_create_user(self, email: str, password: str, full_name: str) -> User:
        # An existing user keeps their password: never let tenant creation overwrite credentials.
        user = await self.users.get_by_email(email)
        if user is None:
            user = User(email=email, password_hash=hash_password(password), full_name=full_name)
            self.users.add(user)
        return user

    async def _flush_or_conflict(self) -> None:
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise Conflict("resource already exists") from exc
