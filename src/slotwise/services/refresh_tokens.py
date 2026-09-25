"""Rotating refresh tokens with reuse detection (OAuth 2.0 Security BCP).

Every refresh returns a NEW refresh token and retires the old one. All tokens descended from
one login share a `family_id`. If a retired token is ever presented again, someone is replaying
a stolen token (or the real client is, after the thief already rotated it). We can't tell which,
so the whole family is revoked: both parties are logged out, and the real user just signs in again.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.errors import Unauthorized
from slotwise.models import RefreshToken
from slotwise.security.tokens import hash_token, new_opaque_token


@dataclass(frozen=True, slots=True)
class Rotated:
    user_id: UUID
    tenant_id: UUID | None
    refresh_token: str


class RefreshTokenReuse(Unauthorized):
    code = "refresh_token_reused"


class RefreshTokenService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def issue(
        self, user_id: UUID, tenant_id: UUID | None, family_id: UUID | None = None
    ) -> tuple[str, RefreshToken]:
        raw = new_opaque_token("rt")
        row = RefreshToken(
            id=uuid.uuid4(),
            user_id=user_id,
            tenant_id=tenant_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
        )
        self.session.add(row)
        return raw, row

    async def rotate(self, raw: str) -> Rotated:
        row = await self._find(raw, lock=True)  # FOR UPDATE serialises concurrent refreshes
        now = datetime.now(UTC)
        if row is None:
            raise Unauthorized("invalid refresh token")
        if row.revoked_at is not None:
            await self._revoke_family(row.family_id, now)
            await self.session.commit()  # the revocation must persist even though we reject
            raise RefreshTokenReuse("refresh token reuse detected; all sessions revoked")
        if row.expires_at <= now:
            raise Unauthorized("refresh token expired")

        new_raw, new_row = self.issue(row.user_id, row.tenant_id, row.family_id)
        await self.session.flush()
        row.revoked_at = now
        row.replaced_by = new_row.id
        return Rotated(user_id=row.user_id, tenant_id=row.tenant_id, refresh_token=new_raw)

    async def revoke(self, raw: str) -> None:
        row = await self._find(raw, lock=False)
        if row is not None:
            await self._revoke_family(row.family_id, datetime.now(UTC))

    async def _find(self, raw: str, *, lock: bool) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw))
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
