"""FastAPI dependencies: services, the authenticated principal, permission checks."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request

from backend.errors import Forbidden, Unauthorized
from backend.rbac import Principal
from backend.services import Services


def services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(services)]


def client_addr(request: Request) -> str | None:
    return request.client.host if request.client else None


async def principal(request: Request, svc: ServicesDep) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token", code="not_authenticated")
    return await svc.auth.principal(token.strip())


PrincipalDep = Annotated[Principal, Depends(principal)]


def require(permission: str) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Global permission, or 'anywhere at all' for scoped ones (finer checks in the route)."""

    async def _check(p: PrincipalDep) -> Principal:
        if p.user.must_change_password and permission != "self":
            raise Forbidden("change your password first", code="password_change_required")
        if permission != "self" and not p.can(permission):
            raise Forbidden(f"missing permission {permission}")
        return p

    return _check
