"""Authentication: login issues a tenant-scoped access token."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.errors import Unauthorized
from slotwise.models import Role, TenantPlan, TenantStatus
from slotwise.repositories.tenancy import MembershipRepository, TenantRepository, UserRepository
from slotwise.security.passwords import verify_password
from slotwise.security.tokens import create_access_token
from slotwise.services.refresh_tokens import RefreshTokenService


@dataclass(frozen=True, slots=True)
class LoginResult:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: UUID
    tenant_id: UUID | None
    role: Role | None
    is_superadmin: bool
    plan: TenantPlan | None = None


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
        plan: TenantPlan | None = None
        if tenant_slug is not None:
            tenant = await self.tenants.get_by_slug(tenant_slug)
            membership = await self.memberships.get(user.id, tenant.id) if tenant else None
            if tenant is None or membership is None:
                raise Unauthorized("invalid credentials")
            if tenant.status is not TenantStatus.ACTIVE:
                raise Unauthorized("tenant suspended")
            tenant_id, role, plan = tenant.id, membership.role, tenant.plan
        elif not user.is_superadmin:
            raise Unauthorized("tenant_slug is required")

        return self.issue(user.id, tenant_id, role, user.is_superadmin, plan)

    async def refresh(self, raw_refresh_token: str) -> LoginResult:
        rotated = await RefreshTokenService(self.session, self.settings).rotate(raw_refresh_token)
        user = await self.users.get(rotated.user_id)
        if user is None:
            raise Unauthorized("invalid refresh token")
        role: Role | None = None
        plan: TenantPlan | None = None
        if rotated.tenant_id is not None:
            # Re-check on every refresh, so removing a member or suspending a tenant takes effect
            # within one access-token lifetime (15 minutes) even for long-lived sessions.
            tenant = await self.tenants.get(rotated.tenant_id)
            membership = await self.memberships.get(user.id, rotated.tenant_id)
            if tenant is None or membership is None or tenant.status is not TenantStatus.ACTIVE:
                raise Unauthorized("membership no longer valid")
            role, plan = membership.role, tenant.plan
        return self._access(
            user.id, rotated.tenant_id, role, user.is_superadmin, rotated.refresh_token, plan
        )

    async def logout(self, raw_refresh_token: str) -> None:
        await RefreshTokenService(self.session, self.settings).revoke(raw_refresh_token)

    def issue(
        self,
        user_id: UUID,
        tenant_id: UUID | None,
        role: Role | None,
        is_superadmin: bool,
        plan: TenantPlan | None = None,
    ) -> LoginResult:
        refresh, _ = RefreshTokenService(self.session, self.settings).issue(user_id, tenant_id)
        return self._access(user_id, tenant_id, role, is_superadmin, refresh, plan)

    def _access(
        self,
        user_id: UUID,
        tenant_id: UUID | None,
        role: Role | None,
        is_superadmin: bool,
        refresh_token: str,
        plan: TenantPlan | None = None,
    ) -> LoginResult:
        token = create_access_token(
            self.settings,
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            is_superadmin=is_superadmin,
            plan=plan,
        )
        return LoginResult(
            access_token=token,
            refresh_token=refresh_token,
            expires_in=self.settings.access_token_ttl_seconds,
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            is_superadmin=is_superadmin,
            plan=plan,
        )
