"""Per-tenant API keys for server-to-server integrations.

Format: sw_<prefix>_<secret>. The 8-char prefix is stored in plain text (indexed, so lookup is
one query and a key can be identified in logs/UI without exposing it); only a SHA-256 hash of
the full key is stored. The secret has 256 bits of entropy, so a fast hash is safe here, unlike
passwords, which need Argon2 because humans pick guessable ones.
"""

import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.errors import NotFound, Unauthorized, ValidationFailed
from slotwise.models import ApiKey, TenantStatus
from slotwise.repositories.audit import AuditRepository
from slotwise.repositories.tenancy import TenantRepository
from slotwise.security.permissions import API_KEY_GRANTABLE, Permission
from slotwise.security.principal import Principal
from slotwise.security.tokens import hash_token

LAST_USED_RESOLUTION = timedelta(minutes=1)  # don't write to the DB on every single request


def _parse(raw: str) -> tuple[str, str] | None:
    parts = raw.split("_")
    if len(parts) != 3 or parts[0] != "sw" or len(parts[1]) != 8:
        return None
    return parts[1], parts[2]


class ApiKeyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        name: str,
        scopes: list[Permission],
        rate_limit_per_min: int,
    ) -> tuple[ApiKey, str]:
        not_grantable = set(scopes) - API_KEY_GRANTABLE
        if not_grantable:
            raise ValidationFailed(
                f"scopes not grantable to API keys: {sorted(p.value for p in not_grantable)}"
            )
        prefix = secrets.token_hex(4)
        raw = f"sw_{prefix}_{secrets.token_urlsafe(32).replace('_', '-')}"
        key = ApiKey(
            tenant_id=tenant_id,
            name=name,
            prefix=prefix,
            key_hash=hash_token(raw),
            scopes=sorted(p.value for p in set(scopes)),
            rate_limit_per_min=rate_limit_per_min,
            created_by=principal.actor_id if principal.actor_type == "user" else None,
        )
        self.session.add(key)
        await self.session.flush()
        AuditRepository(self.session, tenant_id).record(
            principal,
            action="api_key.created",
            entity="api_key",
            entity_id=key.id,
            diff={"scopes": key.scopes, "prefix": prefix},
        )
        return key, raw

    async def find_all(self, tenant_id: UUID) -> list[ApiKey]:
        result = await self.session.execute(
            select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at)
        )
        return list(result.scalars())

    async def revoke(self, principal: Principal, tenant_id: UUID, key_id: UUID) -> None:
        key = (
            await self.session.execute(
                select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if key is None:
            raise NotFound("api key not found")
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            AuditRepository(self.session, tenant_id).record(
                principal, action="api_key.revoked", entity="api_key", entity_id=key_id
            )

    async def authenticate(self, raw: str) -> tuple[Principal, int]:
        """Returns the principal and the key's per-minute rate limit."""
        parsed = _parse(raw)
        if parsed is None:
            raise Unauthorized("invalid api key")
        key = (
            await self.session.execute(select(ApiKey).where(ApiKey.prefix == parsed[0]))
        ).scalar_one_or_none()
        # compare_digest: constant-time, so response timing can't leak how much of a hash matched.
        if key is None or not hmac.compare_digest(key.key_hash, hash_token(raw)):
            raise Unauthorized("invalid api key")
        if key.revoked_at is not None:
            raise Unauthorized("api key revoked")
        tenant = await TenantRepository(self.session).get(key.tenant_id)
        if tenant is None or tenant.status is not TenantStatus.ACTIVE:
            raise Unauthorized("tenant suspended")

        now = datetime.now(UTC)
        if key.last_used_at is None or now - key.last_used_at > LAST_USED_RESOLUTION:
            key.last_used_at = now
            await self.session.commit()
        principal = Principal(
            actor_type="api_key",
            actor_id=key.id,
            tenant_id=key.tenant_id,
            scopes=frozenset(Permission(s) for s in key.scopes),
            plan=tenant.plan,
        )
        return principal, key.rate_limit_per_min
