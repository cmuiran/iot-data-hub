"""/api/control: write requests, two-person confirmation (all through backend.control)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt

from backend.control import WriteResult
from backend.deps import ServicesDep, client_addr, require, require_writer
from backend.errors import NotFound
from backend.rbac import Principal, split_path
from hub.platform.devices import PLATFORM_SITE
from hub.storage.records import PendingWrite

router = APIRouter(prefix="/api/control", tags=["control"])
Writer = Annotated[Principal, Depends(require_writer())]
Confirmer = Annotated[Principal, Depends(require("self"))]


class WriteIn(BaseModel):
    tag: str = Field(min_length=7, max_length=263)
    value: StrictBool | StrictInt | StrictFloat  # no "250" -> 250 coercion for commands
    reason: str | None = Field(default=None, max_length=500)


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class PolicyOut(BaseModel):
    tag: str
    enabled: bool
    confirm: bool
    min_interval_s: float
    max_step: float | None
    interlocks: list[dict[str, Any]]


async def _visible(svc: ServicesDep, p: Principal, tag_path: str) -> bool:
    site, area, device_id, _ = split_path(tag_path)
    if p.can("data.read", site, area):
        return True
    if site == PLATFORM_SITE:        # platform devices: seen through the device's access
        orgs = {g.org_id for g in p.grants if g.org_id and "device.view" in
                p.role_permissions.get(g.role_id, frozenset())}
        return bool(orgs) and bool(await svc.store.accounts.access_levels(orgs, [device_id]))
    return False


@router.post("/write", response_model=WriteResult)
async def write(body: WriteIn, request: Request, svc: ServicesDep, p: Writer) -> WriteResult:
    result = await svc.control.request_write(p, body.tag, body.value, body.reason,
                                             client_addr(request))
    # a tag the user cannot see answers like an unknown one (the attempt is audited all the same)
    if result.result == "rejected_rbac" and not await _visible(svc, p, body.tag):
        raise NotFound(f"unknown tag {body.tag}")
    return result


@router.get("/pending", response_model=list[PendingWrite])
async def pending(svc: ServicesDep, p: Confirmer) -> list[PendingWrite]:
    out = []
    for item in await svc.store.list_pending():
        site, area, _, _ = split_path(item.tag_path)
        if item.requested_by == p.user_id or p.can("write.confirm", site, area):
            out.append(item)
    return out


@router.post("/pending/{request_id}/confirm", response_model=WriteResult)
async def confirm(request_id: UUID, request: Request, svc: ServicesDep, p: Confirmer
                  ) -> WriteResult:
    return await svc.control.confirm(p, request_id, client_addr(request))


@router.post("/pending/{request_id}/reject", response_model=WriteResult)
async def reject(request_id: UUID, body: RejectIn, request: Request, svc: ServicesDep,
                 p: Confirmer) -> WriteResult:
    return await svc.control.reject(p, request_id, body.reason, client_addr(request))


@router.get("/policy/{tag_path}", response_model=PolicyOut)
async def policy(tag_path: str, svc: ServicesDep, p: Writer) -> PolicyOut:
    svc.control.tag_for(tag_path)  # 404 for unknown tags
    if not await _visible(svc, p, tag_path):
        raise NotFound(f"unknown tag {tag_path}")
    pol = svc.control.control.policy_for(tag_path)
    device, tag = svc.hub.tags[tag_path]
    enabled = (svc.settings.control_enabled and device.enabled and str(tag.access) == "read_write")
    return PolicyOut(tag=tag_path, enabled=enabled, confirm=pol.confirm,
                     min_interval_s=pol.min_interval_s, max_step=pol.max_step,
                     interlocks=[i.model_dump() for i in pol.interlocks])
