"""Authentication: login issues a tenant-scoped access token."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.errors import Unauthorized
from slotwise.models import Role, TenantStatus
from slotwise.repositories.tenancy import MembershipRepository, TenantRepository, UserRepository
from slotwise.security.passwords import verify_password
from slotwise.security.tokens import create_access_token


@dataclass(frozen=True, slots=True)
class LoginResult:
    access_token: str
    expires_in: int
    user_id: UUID
    tenant_id: UUID | None
    role: Role | None
    is_superadmin: bool


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.users = UserRepository(session)
        self.tenants = TenantRepository(session)
        self.memberships = MembershipRepository(session)

    async def login(self, email: str, password: str, tenant_slug: str | None) -> LoginResult:
        user = await self.users.get_by_email(email)
        # Always run the hash check, even for unknown emails, and give one generic error:
        # the response must not reveal whether the email exists.
        if not verify_password(user.password_hash if user else None, password) or user is None:
            raise Unauthorized("invalid credentials")

        tenant_id: UUID | None = None
        role: Role | None = None
        if tenant_slug is not None:
            tenant = await self.tenants.get_by_slug(tenant_slug)
            membership = await self.memberships.get(user.id, tenant.id) if tenant else None
            if tenant is None or membership is None:
                raise Unauthorized("invalid credentials")
            if tenant.status is not TenantStatus.ACTIVE:
                raise Unauthorized("tenant suspended")
            tenant_id, role = tenant.id, membership.role
        elif not user.is_superadmin:
            raise Unauthorized("tenant_slug is required")

        return self.issue(user.id, tenant_id, role, user.is_superadmin)

    def issue(
        self, user_id: UUID, tenant_id: UUID | None, role: Role | None, is_superadmin: bool
    ) -> LoginResult:
        token = create_access_token(
            self.settings,
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            is_superadmin=is_superadmin,
        )
        return LoginResult(
            access_token=token,
            expires_in=self.settings.access_token_ttl_seconds,
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            is_superadmin=is_superadmin,
        )
