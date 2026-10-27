"""/api/users and /api/roles: user management (permission user.manage)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from backend.deps import ServicesDep, client_addr, require
from backend.errors import Conflict, Invalid, NotFound
from backend.rbac import Principal
from backend.security import hash_password
from hub.drivers.base import utcnow
from hub.models import SEGMENT_PATTERN
from hub.platform.devices import PLATFORM_SITE
from hub.storage.records import RoleGrant, UserRecord

router = APIRouter(tags=["users"])
Admin = Annotated[Principal, Depends(require("user.manage"))]
Language = Literal["fa", "en"]


class GrantIn(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    organization: str | None = Field(default=None, pattern=f"^{SEGMENT_PATTERN}$")
    site: str | None = Field(default=None, pattern=f"^{SEGMENT_PATTERN}$")
    area: str | None = Field(default=None, pattern=f"^{SEGMENT_PATTERN}$")


class UserIn(BaseModel):
    username: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    language: Language = "fa"
    roles: list[GrantIn] = Field(default_factory=list)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None
    language: Language | None = None
    must_change_password: bool | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)


class GrantOut(BaseModel):
    user_role_id: int
    role: str
    organization: str | None = None
    site: str | None
    area: str | None
    granted_at: datetime


class UserOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    is_active: bool
    language: str
    must_change_password: bool
    locked_until: datetime | None
    last_login_at: datetime | None
    created_at: datetime
    roles: list[GrantOut]
    mfa_enabled: bool = False
    auth_source: str = "local"          # ldap: password and roles come from the directory


def _grant_out(g: RoleGrant) -> GrantOut:
    return GrantOut(user_role_id=g.user_role_id, role=g.role_id, organization=g.org_id,
                    site=g.site, area=g.area, granted_at=g.granted_at)


async def _user_out(svc: ServicesDep, user: UserRecord) -> UserOut:
    return UserOut(**user.model_dump(include=set(UserOut.model_fields)
                                     - {"roles", "mfa_enabled"}),
                   roles=[_grant_out(g) for g in await svc.store.grants(user.user_id)],
                   mfa_enabled=user.totp_enabled)


async def _get(svc: ServicesDep, user_id: int) -> UserRecord:
    user = await svc.store.get_user(user_id)
    if user is None:
        raise NotFound("no such user")
    return user


def _local_only(user: UserRecord, what: str) -> None:
    if user.auth_source == "ldap":
        raise Conflict(f"{user.username} signs in with Active Directory: {what} come from the "
                       "directory", code="directory_account")


async def _check_grant(svc: ServicesDep, grant: GrantIn) -> None:
    if grant.role not in await svc.store.role_permissions():
        raise Invalid(f"unknown role {grant.role}", code="unknown_role")
    if grant.area is not None and grant.site is None:
        raise Invalid("an area scope needs a site", code="invalid_scope")
    if grant.organization is not None:
        if grant.site is not None:
            raise Invalid("grant for an organization or for a site, not both",
                          code="invalid_scope")
        known = {o.id for o in svc.hub.report.building.organizations}
        known |= await svc.store.platform.organization_ids()
        if grant.organization not in known:
            raise Invalid(f"unknown organization {grant.organization} (config/building.yaml "
                          "or the platform)", code="unknown_organization")
    if grant.site == PLATFORM_SITE:
        raise Invalid(f"site {PLATFORM_SITE} holds platform devices: their access comes from "
                      "the device's organizations, not from site grants", code="invalid_scope")


async def _guard_last_admin(svc: ServicesDep, *, disabling: int | None = None,
                            removing: RoleGrant | None = None) -> None:
    """Refuse a change that would leave no active user with a global admin grant."""
    for user in await svc.store.list_users():
        if not user.is_active or user.user_id == disabling:
            continue
        for g in await svc.store.grants(user.user_id):
            if removing is not None and g.user_role_id == removing.user_role_id:
                continue
            if g.role_id == "admin" and g.site is None and g.org_id is None:
                return
    raise Conflict("this would remove the last administrator", code="last_admin")


@router.get("/api/roles")
async def roles(svc: ServicesDep, _: Admin) -> dict[str, list[str]]:
    return {role: sorted(perms) for role, perms in sorted((await svc.store.role_permissions())
                                                          .items())}


@router.get("/api/users", response_model=list[UserOut])
async def list_users(svc: ServicesDep, _: Admin) -> list[UserOut]:
    return [await _user_out(svc, u) for u in await svc.store.list_users()]


@router.post("/api/users", response_model=UserOut, status_code=201)
async def create_user(body: UserIn, request: Request, svc: ServicesDep, p: Admin) -> UserOut:
    svc.auth.check_password(body.password, body.username)
    for grant in body.roles:
        await _check_grant(svc, grant)
    try:
        user = await svc.store.create_user(body.username, body.display_name,
                                           hash_password(body.password), language=body.language,
                                           must_change_password=True)
    except pg_errors.UniqueViolation as exc:
        raise Conflict("username already exists", code="username_taken") from exc
    for grant in body.roles:
        await svc.store.grant_role(user.user_id, grant.role, grant.site, grant.area, p.user_id,
                                   grant.organization)
    await svc.store.add_auth_audit("user_created", user.username, user_id=user.user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request),
                                   detail={"roles": [g.model_dump() for g in body.roles]})
    return await _user_out(svc, user)


@router.get("/api/users/{user_id}", response_model=UserOut)
async def get_user(user_id: int, svc: ServicesDep, _: Admin) -> UserOut:
    return await _user_out(svc, await _get(svc, user_id))


@router.patch("/api/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserPatch, request: Request, svc: ServicesDep,
                      p: Admin) -> UserOut:
    user = await _get(svc, user_id)
    fields = body.model_dump(exclude_none=True)
    if fields.get("is_active") is False:
        if user_id == p.user_id:
            raise Conflict("you cannot disable yourself", code="self_disable")
        await _guard_last_admin(svc, disabling=user_id)
    updated = await svc.store.update_user(user_id, **fields)
    assert updated is not None
    if fields.get("is_active") is False:
        await svc.store.revoke_user_sessions(user_id)
    event = "user_disabled" if fields.get("is_active") is False else "user_updated"
    await svc.store.add_auth_audit(event, user.username, user_id=user_id, actor_id=p.user_id,
                                   client_addr=client_addr(request), detail=fields)
    return await _user_out(svc, updated)


@router.post("/api/users/{user_id}/password", status_code=204)
async def reset_password(user_id: int, body: PasswordReset, request: Request, svc: ServicesDep,
                         p: Admin) -> None:
    user = await _get(svc, user_id)
    _local_only(user, "the password and its changes")
    await svc.auth.set_password(user, body.new_password, must_change=True)
    await svc.store.revoke_user_sessions(user_id)
    await svc.store.add_auth_audit("password_changed", user.username, user_id=user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request),
                                   detail={"reset_by_admin": True})


@router.post("/api/users/{user_id}/mfa/reset", status_code=204)
async def reset_mfa(user_id: int, request: Request, svc: ServicesDep, p: Admin) -> None:
    """The user lost the authenticator: remove MFA and end their sessions."""
    await svc.auth.mfa_reset(await _get(svc, user_id), p, client_addr(request))


@router.post("/api/users/{user_id}/unlock", status_code=204)
async def unlock(user_id: int, request: Request, svc: ServicesDep, p: Admin) -> None:
    user = await _get(svc, user_id)
    await svc.store.update_user(user_id, failed_logins=0, locked_until=None)
    await svc.store.add_auth_audit("unlocked", user.username, user_id=user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request))


@router.post("/api/users/{user_id}/roles", response_model=GrantOut, status_code=201)
async def grant_role(user_id: int, body: GrantIn, request: Request, svc: ServicesDep,
                     p: Admin) -> GrantOut:
    user = await _get(svc, user_id)
    _local_only(user, "roles (directory groups, config/ldap.yaml)")
    await _check_grant(svc, body)
    try:
        grant = await svc.store.grant_role(user_id, body.role, body.site, body.area, p.user_id,
                                           body.organization)
    except pg_errors.UniqueViolation as exc:
        raise Conflict("the user already has this role for this scope", code="duplicate") from exc
    await svc.store.add_auth_audit("role_granted", user.username, user_id=user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request),
                                   detail=body.model_dump())
    return _grant_out(grant)


@router.delete("/api/users/{user_id}/roles/{user_role_id}", status_code=204)
async def revoke_role(user_id: int, user_role_id: int, request: Request, svc: ServicesDep,
                      p: Admin) -> None:
    user = await _get(svc, user_id)
    _local_only(user, "roles (directory groups, config/ldap.yaml)")
    grant = next((g for g in await svc.store.grants(user_id) if g.user_role_id == user_role_id),
                 None)
    if grant is None:
        raise NotFound("no such role grant")
    if grant.role_id == "admin" and grant.site is None:
        await _guard_last_admin(svc, removing=grant)
    await svc.store.revoke_role(user_role_id)
    await svc.store.add_auth_audit("role_revoked", user.username, user_id=user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request),
                                   detail={"role": grant.role_id, "site": grant.site,
                                           "area": grant.area})


@router.get("/api/users/{user_id}/sessions")
async def user_sessions(user_id: int, svc: ServicesDep, _: Admin) -> list[dict[str, object]]:
    await _get(svc, user_id)
    return await svc.store.list_sessions(user_id, utcnow())


@router.delete("/api/users/{user_id}/sessions", status_code=204)
async def end_user_sessions(user_id: int, request: Request, svc: ServicesDep, p: Admin) -> None:
    """Log a user out everywhere (e.g. a lost laptop)."""
    user = await _get(svc, user_id)
    await svc.store.revoke_user_sessions(user_id)
    await svc.store.add_auth_audit("session_revoked", user.username, user_id=user_id,
                                   actor_id=p.user_id, client_addr=client_addr(request),
                                   detail={"all": True})
