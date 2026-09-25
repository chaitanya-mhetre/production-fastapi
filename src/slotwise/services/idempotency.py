"""Idempotency-Key support for POST endpoints (Stripe-style semantics).

Mobile clients on bad networks retry. Without idempotency, a retried "create booking" whose first
response was lost creates a second booking. With it:

    same key + same body, finished      → replay the stored response (header Idempotent-Replayed)
    same key + different body           → 422 idempotency_key_reused
    same key while first still running  → 409 idempotency_in_progress
    first attempt crashed mid-way       → its lock expires (locked_until) and a retry may proceed

Storage:
- Postgres `idempotency_keys` is the source of truth. The claim is an INSERT on the primary key,
  so exactly one request wins, even across API instances.
- The stored response is written in the SAME transaction as the business change: either both the
  booking and its idempotent response commit, or neither does.
- Redis caches finished responses so a replay costs one GET instead of a DB round-trip.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from slotwise.config import Settings
from slotwise.db import Database
from slotwise.errors import (
    AppError,
    IdempotencyInProgress,
    IdempotencyKeyRequired,
    IdempotencyKeyReused,
    ValidationFailed,
)
from slotwise.models import IdempotencyKey

REPLAY_HEADER = "Idempotent-Replayed"


def request_fingerprint(method: str, path: str, body: dict[str, Any] | None) -> str:
    canonical = json.dumps(body or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method.upper()} {path}\n{canonical}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: dict[str, Any]

    def to_response(self) -> JSONResponse:
        return JSONResponse(self.body, status_code=self.status, headers={REPLAY_HEADER: "true"})


class IdempotencyContext:
    def __init__(
        self,
        *,
        db: Database,
        redis: Redis,
        settings: Settings,
        tenant_id: UUID,
        key: str,
        fingerprint: str,
    ) -> None:
        if not 8 <= len(key) <= 255:
            raise ValidationFailed("Idempotency-Key must be 8-255 characters")
        self.db = db
        self.redis = redis
        self.settings = settings
        self.tenant_id = tenant_id
        self.key = key
        self.fingerprint = fingerprint

    @property
    def cache_key(self) -> str:
        return f"idem:{self.tenant_id}:{self.key}"

    async def claim(self) -> StoredResponse | None:
        """Returns a stored response to replay, or None if this request now owns the key."""
        cached = await self.redis.get(self.cache_key)
        if cached:
            data = json.loads(cached)
            if data["fingerprint"] != self.fingerprint:
                raise IdempotencyKeyReused("this Idempotency-Key was used with a different request")
            return StoredResponse(data["status"], data["body"])

        now = datetime.now(UTC)
        lock_until = now + timedelta(seconds=self.settings.idempotency_lock_seconds)
        async with self.db.session(tenant_id=self.tenant_id) as session, session.begin():
            inserted = await session.execute(
                insert(IdempotencyKey)
                .values(
                    tenant_id=self.tenant_id,
                    key=self.key,
                    request_hash=self.fingerprint,
                    locked_until=lock_until,
                )
                .on_conflict_do_nothing()
                .returning(IdempotencyKey.key)
            )
            if inserted.scalar_one_or_none() is not None:
                return None  # we own it

            row = (
                await session.execute(
                    select(IdempotencyKey)
                    .where(
                        IdempotencyKey.tenant_id == self.tenant_id, IdempotencyKey.key == self.key
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if row.request_hash != self.fingerprint:
                raise IdempotencyKeyReused("this Idempotency-Key was used with a different request")
            if row.response_status is not None and row.response_body is not None:
                stored = StoredResponse(row.response_status, row.response_body)
                await self._cache(stored)
                return stored
            if row.locked_until and row.locked_until > now:
                raise IdempotencyInProgress("a request with this Idempotency-Key is in progress")
            # The previous owner died without finishing: take over its expired lock.
            row.locked_until = lock_until
            return None

    async def complete(self, session: AsyncSession, stored: StoredResponse) -> None:
        """Record the response in the caller's transaction (commits atomically with the change)."""
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.tenant_id == self.tenant_id, IdempotencyKey.key == self.key)
            .values(response_status=stored.status, response_body=stored.body, locked_until=None)
        )

    async def complete_in_own_transaction(self, stored: StoredResponse) -> None:
        async with self.db.session(tenant_id=self.tenant_id) as session, session.begin():
            await self.complete(session, stored)
        await self._cache(stored)

    async def release(self) -> None:
        """Unexpected failure (bug, DB down): forget the claim so the client can retry."""
        async with self.db.session(tenant_id=self.tenant_id) as session, session.begin():
            await session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == self.tenant_id,
                    IdempotencyKey.key == self.key,
                    IdempotencyKey.response_status.is_(None),
                )
            )

    async def _cache(self, stored: StoredResponse) -> None:
        payload = {"fingerprint": self.fingerprint, "status": stored.status, "body": stored.body}
        await self.redis.set(
            self.cache_key,
            json.dumps(payload, default=str),
            ex=self.settings.idempotency_ttl_seconds,
        )

    async def after_commit(self, stored: StoredResponse) -> None:
        await self._cache(stored)


Handler = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


async def run_idempotent(
    ctx: IdempotencyContext | None, session: AsyncSession, handler: Handler
) -> JSONResponse:
    """Run `handler` (which does the business change but does not commit) exactly once per key.

    Deterministic business errors (4xx AppError such as slot_taken) are stored too, so a retry
    gets the same answer. Unexpected errors release the key so the client can retry.
    """
    if ctx is None:
        status, body = await handler()
        await session.commit()
        return JSONResponse(body, status_code=status)

    replay = await ctx.claim()
    if replay is not None:
        return replay.to_response()
    try:
        status, body = await handler()
        stored = StoredResponse(status, body)
        await ctx.complete(session, stored)
        await session.commit()
    except AppError as exc:
        await session.rollback()
        if exc.status_code < 500:
            await ctx.complete_in_own_transaction(
                StoredResponse(
                    exc.status_code, {"error": {"code": exc.code, "message": exc.message}}
                )
            )
        else:
            await ctx.release()
        raise
    except BaseException:
        await session.rollback()
        await ctx.release()
        raise
    await ctx.after_commit(stored)
    return JSONResponse(body, status_code=status)


def require_key(key: str | None) -> str:
    if not key:
        raise IdempotencyKeyRequired("Idempotency-Key header is required for this endpoint")
    return key
