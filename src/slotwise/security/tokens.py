"""JWT access tokens (short-lived, stateless) + opaque refresh tokens (long-lived, hashed)."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from slotwise.config import Settings
from slotwise.errors import Unauthorized
from slotwise.models import Role, TenantPlan

ALGORITHM = "HS256"


def create_access_token(
    settings: Settings,
    *,
    user_id: UUID,
    tenant_id: UUID | None,
    role: Role | None,
    is_superadmin: bool,
    plan: TenantPlan | None = None,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "sub": str(user_id),
        "typ": "access",
        "tid": str(tenant_id) if tenant_id else None,
        "role": role.value if role else None,
        "sa": is_superadmin,
        "plan": plan.value if plan else None,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],  # pinned: never let the token choose "none" or RS/HS confusion
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("token expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid token") from exc
    if claims.get("typ") != "access":
        raise Unauthorized("invalid token type")
    return claims


def new_opaque_token(prefix: str) -> str:
    """256 bits of randomness. High entropy is why a fast hash (sha256) is fine for storage."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
