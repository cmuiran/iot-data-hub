"""/api/dashboards: saved dashboard layouts (the layout JSON belongs to the frontend).

Every user with data.read may keep dashboards; ``shared`` ones are visible (read-only) to all
users. Only the owner edits; the owner or an admin deletes. A shared dashboard shows only the
tags each viewer may read — the layout holds tag paths, the data API filters them.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field, field_validator

from backend.deps import ServicesDep, require
from backend.errors import Conflict, Forbidden, NotFound
from backend.rbac import Principal

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])
Reader = Annotated[Principal, Depends(require("data.read"))]
MAX_LAYOUT_BYTES = 256 * 1024


class DashboardIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    layout: dict[str, Any] = Field(default_factory=dict)
    shared: bool = False
    is_default: bool = False

    @field_validator("layout")
    @classmethod
    def _size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_LAYOUT_BYTES:
            raise ValueError(f"layout larger than {MAX_LAYOUT_BYTES // 1024} KB")
        return value


class DashboardSummary(BaseModel):
    dashboard_id: int
    name: str
    owner: str
    owner_id: int
    shared: bool
    is_default: bool
    updated_at: datetime


class DashboardOut(DashboardSummary):
    layout: dict[str, Any]
    created_at: datetime


async def _get(svc: ServicesDep, p: Principal, dashboard_id: int) -> dict[str, Any]:
    d = await svc.store.get_dashboard(dashboard_id)
    if d is None or (d["owner_id"] != p.user_id and not d["shared"]):
        raise NotFound("no such dashboard")
    return d


@router.get("", response_model=list[DashboardSummary])
async def list_dashboards(svc: ServicesDep, p: Reader) -> list[dict[str, Any]]:
    return await svc.store.list_dashboards(p.user_id)


@router.post("", response_model=DashboardOut, status_code=201)
async def create_dashboard(body: DashboardIn, svc: ServicesDep, p: Reader) -> dict[str, Any]:
    try:
        return await svc.store.save_dashboard(p.user_id, body.name, body.layout, body.shared,
                                              body.is_default)
    except pg_errors.UniqueViolation as exc:
        raise Conflict("you already have a dashboard with this name", code="duplicate") from exc


@router.get("/{dashboard_id}", response_model=DashboardOut)
async def get_dashboard(dashboard_id: int, svc: ServicesDep, p: Reader) -> dict[str, Any]:
    return await _get(svc, p, dashboard_id)


@router.put("/{dashboard_id}", response_model=DashboardOut)
async def update_dashboard(dashboard_id: int, body: DashboardIn, svc: ServicesDep, p: Reader
                           ) -> dict[str, Any]:
    d = await _get(svc, p, dashboard_id)
    if d["owner_id"] != p.user_id:
        raise Forbidden("only the owner can change a dashboard")
    try:
        return await svc.store.save_dashboard(p.user_id, body.name, body.layout, body.shared,
                                              body.is_default, dashboard_id)
    except pg_errors.UniqueViolation as exc:
        raise Conflict("you already have a dashboard with this name", code="duplicate") from exc


@router.delete("/{dashboard_id}", status_code=204)
async def delete_dashboard(dashboard_id: int, svc: ServicesDep, p: Reader) -> None:
    d = await _get(svc, p, dashboard_id)
    if d["owner_id"] != p.user_id and not p.can("user.manage"):
        raise Forbidden("only the owner or an administrator can delete a dashboard")
    if not await svc.store.delete_dashboard(dashboard_id):
        raise NotFound("no such dashboard")

