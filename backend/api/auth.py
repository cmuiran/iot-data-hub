"""/api/auth: login, refresh, logout, me, change password."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.auth import MfaChallenge, TokenPair
from backend.deps import PrincipalDep, ServicesDep, client_addr, require
from backend.errors import NotFound
from backend.rbac import Principal
from hub.drivers.base import utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=254)     # or the e-mail address
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    must_change_password: bool


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class MeOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    language: str
    must_change_password: bool
    mfa_enabled: bool
    mfa_enrollment_required: bool
    roles: list[dict[str, str | None]]
    permissions: dict[str, list[dict[str, str | None]]]
    auth_source: str = "local"
    email: str | None = None


class MfaChallengeOut(BaseModel):
    mfa_required: bool = True
    mfa_token: str
    expires_in: int


class MfaVerifyIn(BaseModel):
    mfa_token: str = Field(min_length=10, max_length=1000)
    code: str | None = Field(default=None, max_length=10)
    recovery_code: str | None = Field(default=None, max_length=20)


class MfaCodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class MfaDisableIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=10)


def _tokens(pair: TokenPair) -> TokenOut:
    return TokenOut(access_token=pair.access_token, refresh_token=pair.refresh_token,
                    expires_in=pair.expires_in, must_change_password=pair.must_change_password)


@router.post("/login", response_model=TokenOut | MfaChallengeOut)
async def login(body: LoginIn, request: Request, svc: ServicesDep) -> TokenOut | MfaChallengeOut:
    """Password step. With MFA enabled the answer is ``{mfa_required, mfa_token}``: send the
    token with a 6-digit code (or a recovery code) to /api/auth/mfa/verify."""
    result = await svc.auth.login(body.username, body.password, client_addr(request),
                                  request.headers.get("user-agent"))
    if isinstance(result, MfaChallenge):
        return MfaChallengeOut(mfa_token=result.mfa_token, expires_in=result.expires_in)
    return _tokens(result)


@router.post("/mfa/verify", response_model=TokenOut)
async def mfa_verify(body: MfaVerifyIn, request: Request, svc: ServicesDep) -> TokenOut:
    return _tokens(await svc.auth.verify_mfa(body.mfa_token, body.code, body.recovery_code,
                                             client_addr(request),
                                             request.headers.get("user-agent")))


@router.post("/mfa/setup")
async def mfa_setup(svc: ServicesDep, p: Annotated[Principal, Depends(require("self"))]
                    ) -> dict[str, str]:
    """Start enrolment: show ``otpauth_uri`` as a QR code (or type ``secret`` into the app)."""
    return await svc.auth.mfa_setup(p)


@router.post("/mfa/enable")
async def mfa_enable(body: MfaCodeIn, request: Request, svc: ServicesDep,
                     p: Annotated[Principal, Depends(require("self"))]) -> dict[str, list[str]]:
    """Confirm with the first code. The recovery codes are shown only this once."""
    return {"recovery_codes": await svc.auth.mfa_enable(p, body.code, client_addr(request))}


@router.post("/mfa/disable", status_code=204)
async def mfa_disable(body: MfaDisableIn, request: Request, svc: ServicesDep,
                      p: Annotated[Principal, Depends(require("self"))]) -> None:
    await svc.auth.mfa_disable(p, body.password, body.code, client_addr(request))


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, request: Request, svc: ServicesDep) -> TokenOut:
    return _tokens(await svc.auth.refresh(body.refresh_token, client_addr(request),
                                          request.headers.get("user-agent")))


@router.post("/logout", status_code=204)
async def logout(p: PrincipalDep, request: Request, svc: ServicesDep) -> None:
    await svc.auth.logout(p, client_addr(request))


@router.get("/me", response_model=MeOut)
async def me(p: Annotated[Principal, Depends(require("self"))]) -> MeOut:
    return MeOut(user_id=p.user_id, username=p.username, display_name=p.user.display_name,
                 language=p.user.language, must_change_password=p.user.must_change_password,
                 mfa_enabled=p.user.totp_enabled, mfa_enrollment_required=p.mfa_missing,
                 roles=[{"role": g.role_id, "site": g.site, "area": g.area} for g in p.grants],
                 permissions=p.effective(), auth_source=p.user.auth_source,
                 email=p.user.email)


@router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordIn, request: Request, svc: ServicesDep,
                          p: Annotated[Principal, Depends(require("self"))]) -> None:
    await svc.auth.change_password(p, body.current_password, body.new_password,
                                   client_addr(request))


class SessionOut(BaseModel):
    session_id: UUID
    created_at: datetime
    expires_at: datetime
    client_addr: str | None
    user_agent: str | None
    current: bool = False


@router.get("/sessions", response_model=list[SessionOut])
async def my_sessions(svc: ServicesDep, p: Annotated[Principal, Depends(require("self"))]
                      ) -> list[SessionOut]:
    rows = await svc.store.list_sessions(p.user_id, utcnow())
    return [SessionOut(**r, current=r["session_id"] == p.session_id) for r in rows]


@router.delete("/sessions/{session_id}", status_code=204)
async def end_session(session_id: UUID, request: Request, svc: ServicesDep,
                      p: Annotated[Principal, Depends(require("self"))]) -> None:
    session = await svc.store.get_session(session_id)
    if session is None or session.user_id != p.user_id:
        raise NotFound("no such session")
    await svc.store.revoke_session(session_id)
    await svc.store.add_auth_audit("session_revoked", p.username, user_id=p.user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request),
                                   detail={"session_id": str(session_id)})
