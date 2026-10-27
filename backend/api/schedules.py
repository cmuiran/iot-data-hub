"""/api/schedules: scheduled / sequenced commands (see backend/schedules.py).

Creating a schedule needs ``tag.write`` for every tag it writes (checked again at every run).
Only the owner or a user with ``config.edit`` may change, run or delete a schedule.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from psycopg import errors as pg_errors

from backend.deps import ServicesDep, client_addr, require_writer
from backend.errors import Conflict, Forbidden, Invalid, NotFound
from backend.rbac import Principal, split_path, write_permission
from backend.schedules import ScheduleIn, WaitStep, WriteStep
from hub.drivers.base import utcnow
from hub.models import Access

router = APIRouter(prefix="/api/schedules", tags=["schedules"])
Writer = Annotated[Principal, Depends(require_writer())]


def _check_steps(svc: ServicesDep, p: Principal, body: ScheduleIn) -> None:
    for i, step in enumerate(body.steps, start=1):
        if isinstance(step, WriteStep):
            tag = step.write.tag
            found = svc.hub.tags.get(tag)
            if found is None:
                raise Invalid(f"step {i}: unknown tag {tag}", code="schedule_invalid")
            site, area, _, _ = split_path(tag)
            if not p.can_write(found[1].write_class, site, area):
                raise Forbidden(f"step {i}: no {write_permission(found[1].write_class)} for {tag}")
            if found[1].access is not Access.READ_WRITE:
                raise Invalid(f"step {i}: {tag} is read-only", code="schedule_invalid")
            if svc.control.control.policy_for(tag).confirm:
                raise Invalid(f"step {i}: {tag} needs a second person and cannot be scheduled",
                              code="schedule_invalid")
        elif isinstance(step, WaitStep):
            tag = step.wait.tag
            if tag not in svc.hub.tags:
                raise Invalid(f"step {i}: unknown tag {tag}", code="schedule_invalid")
            site, area, _, _ = split_path(tag)
            if not p.can("data.read", site, area):
                raise Forbidden(f"step {i}: cannot read {tag}")


async def _mine(svc: ServicesDep, p: Principal, schedule_id: int) -> dict[str, Any]:
    s = await svc.store.get_schedule(schedule_id)
    if s is None:
        raise NotFound("no such schedule")
    if s["owner_id"] != p.user_id and not p.can("config.edit"):
        raise Forbidden("only the owner or an engineer can do this")
    return s


@router.get("")
async def list_schedules(svc: ServicesDep, _: Writer) -> list[dict[str, Any]]:
    return await svc.store.list_schedules()


@router.post("", status_code=201)
async def create_schedule(body: ScheduleIn, request: Request, svc: ServicesDep, p: Writer
                          ) -> dict[str, Any]:
    _check_steps(svc, p, body)
    try:
        saved = await svc.store.save_schedule(
            name=body.name, description=body.description, owner_id=p.user_id,
            enabled=body.enabled, cron=body.cron, timezone=body.timezone,
            steps=[s.model_dump(mode="json") for s in body.steps],
            next_run_at=body.next_run(utcnow()))
    except pg_errors.UniqueViolation as exc:
        raise Conflict("a schedule with this name exists", code="duplicate") from exc
    await svc.store.add_auth_audit("schedule_created", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"schedule_id": saved["schedule_id"],
                                           "name": body.name, "cron": body.cron})
    return saved


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: int, svc: ServicesDep, _: Writer) -> dict[str, Any]:
    s = await svc.store.get_schedule(schedule_id)
    if s is None:
        raise NotFound("no such schedule")
    return s


@router.put("/{schedule_id}")
async def update_schedule(schedule_id: int, body: ScheduleIn, request: Request,
                          svc: ServicesDep, p: Writer) -> dict[str, Any]:
    await _mine(svc, p, schedule_id)
    _check_steps(svc, p, body)
    try:
        saved = await svc.store.save_schedule(
            name=body.name, description=body.description, owner_id=p.user_id,
            enabled=body.enabled, cron=body.cron, timezone=body.timezone,
            steps=[s.model_dump(mode="json") for s in body.steps],
            next_run_at=body.next_run(utcnow()), schedule_id=schedule_id)
    except pg_errors.UniqueViolation as exc:
        raise Conflict("a schedule with this name exists", code="duplicate") from exc
    await svc.store.add_auth_audit("schedule_updated", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"schedule_id": schedule_id, "enabled": body.enabled})
    return saved


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int, request: Request, svc: ServicesDep, p: Writer
                          ) -> None:
    s = await _mine(svc, p, schedule_id)
    await svc.store.delete_schedule(schedule_id)
    await svc.store.add_auth_audit("schedule_deleted", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"schedule_id": schedule_id, "name": s["name"]})


@router.post("/{schedule_id}/run", status_code=202)
async def run_now(schedule_id: int, svc: ServicesDep, p: Writer) -> dict[str, Any]:
    s = await _mine(svc, p, schedule_id)
    if svc.scheduler is None:
        raise Conflict("the scheduler is not running on this node", code="standby")
    svc.scheduler.launch(s, "manual")
    return {"schedule_id": schedule_id, "started": True}


@router.get("/{schedule_id}/runs")
async def runs(schedule_id: int, svc: ServicesDep, _: Writer) -> list[dict[str, Any]]:
    if await svc.store.get_schedule(schedule_id) is None:
        raise NotFound("no such schedule")
    return await svc.store.schedule_runs(schedule_id)
