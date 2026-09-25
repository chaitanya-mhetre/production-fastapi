from fastapi import APIRouter

from slotwise.api.deps import SessionDep, SettingsDep
from slotwise.schemas.tenancy import LoginIn, TokenOut
from slotwise.services.auth import AuthService

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginIn, session: SessionDep, settings: SettingsDep) -> TokenOut:
    result = await AuthService(session, settings).login(body.email, body.password, body.tenant_slug)
    return TokenOut(access_token=result.access_token, expires_in=result.expires_in)
