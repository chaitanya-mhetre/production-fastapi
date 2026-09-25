from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from slotwise.api.deps import RedisDep, SessionDep, SettingsDep
from slotwise.schemas.tenancy import LoginIn, TokenOut
from slotwise.security.rate_limit import RateLimiter
from slotwise.services.auth import AuthService, LoginResult

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class RefreshIn(BaseModel):
    refresh_token: str


def _out(result: LoginResult) -> TokenOut:
    return TokenOut(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@router.post("/login")
async def login(
    body: LoginIn, request: Request, session: SessionDep, settings: SettingsDep, redis: RedisDep
) -> TokenOut:
    # Brute-force brake: per (client IP, email). Keyed on email too, so one NAT'd office
    # can't lock everyone out, and on IP so one attacker can't lock out a victim globally.
    client_ip = request.client.host if request.client else "unknown"
    await RateLimiter(redis).hit(
        f"login:{client_ip}:{body.email.lower()}", limit=settings.login_attempts_per_min
    )
    result = await AuthService(session, settings).login(body.email, body.password, body.tenant_slug)
    await session.commit()
    return _out(result)


@router.post("/refresh")
async def refresh(body: RefreshIn, session: SessionDep, settings: SettingsDep) -> TokenOut:
    result = await AuthService(session, settings).refresh(body.refresh_token)
    await session.commit()
    return _out(result)


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, session: SessionDep, settings: SettingsDep) -> Response:
    await AuthService(session, settings).logout(body.refresh_token)
    await session.commit()
    return Response(status_code=204)
