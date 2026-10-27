"""/api/account: self-service accounts (backend.accounts).

Without sign-in: sign-up, e-mail verification, resend, forgot / reset password (answers do not
reveal whether an address has an account). Signed in: the caller's organizations, leaving one,
the invitations addressed to the caller, accepting an invitation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.deps import ServicesDep, client_addr, require
from backend.rbac import Principal
from hub.models import SEGMENT_PATTERN
from hub.storage.accounts import InvitationRecord, MembershipRecord

router = APIRouter(prefix="/api/account", tags=["account"])
Me = Annotated[Principal, Depends(require("self"))]


class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=128)
    language: Literal["fa", "en"] = "fa"
    invitation_token: str | None = Field(default=None, min_length=20, max_length=200)


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class EmailIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=1, max_length=256)


class AcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    # a device share: which of the caller's organizations receives it (default: personal)
    organization: str | None = Field(default=None, pattern=f"^{SEGMENT_PATTERN}$")


@router.post("/signup", status_code=202)
async def signup(body: SignupIn, svc: ServicesDep, request: Request) -> dict[str, Any]:
    return await svc.accounts.signup(
        email=body.email, password=body.password, display_name=body.display_name.strip(),
        language=body.language, invitation_token=body.invitation_token,
        client_addr=client_addr(request))


@router.post("/verify")
async def verify(body: TokenIn, svc: ServicesDep, request: Request) -> dict[str, Any]:
    return await svc.accounts.verify(body.token, client_addr(request))


@router.post("/verify/resend", status_code=202)
async def resend(body: EmailIn, svc: ServicesDep, request: Request) -> dict[str, Any]:
    return await svc.accounts.resend(body.email, client_addr(request))


@router.post("/password/forgot", status_code=202)
async def forgot(body: EmailIn, svc: ServicesDep, request: Request) -> dict[str, Any]:
    return await svc.accounts.forgot(body.email, client_addr(request))


@router.post("/password/reset")
async def reset(body: ResetIn, svc: ServicesDep, request: Request) -> dict[str, Any]:
    return await svc.accounts.reset(body.token, body.new_password, client_addr(request))


@router.get("/organizations")
async def my_organizations(svc: ServicesDep, p: Me) -> list[MembershipRecord]:
    return await svc.accounts.memberships(p)


@router.delete("/organizations/{org_id}", status_code=204)
async def leave(org_id: str, svc: ServicesDep, p: Me, request: Request) -> None:
    await svc.accounts.leave(p, org_id, client_addr(request))


@router.get("/invitations")
async def my_invitations(svc: ServicesDep, p: Me) -> list[InvitationRecord]:
    return await svc.accounts.my_invitations(p)


@router.post("/invitations/accept")
async def accept(body: AcceptIn, svc: ServicesDep, p: Me, request: Request) -> dict[str, Any]:
    return await svc.accounts.accept(p, body.token, body.organization, client_addr(request))
