"""/api/platform/organizations/{org}/scenes | automations | holidays, and the Jalali calendar.

Reading needs ``device.view`` in the organization, changing ``automation.manage`` there. Every
device a scene or automation refers to must be one the organization owns or received; writes
need owner or operator access and a writable tag of the device's product, with a value of the
tag's type and range (checked again by the control-safety layer at run time).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from backend.automations import (
    TIME_TRIGGERS,
    AutomationIn,
    OfflineTrigger,
    SceneAction,
    SceneIn,
    ValueTrigger,
    WriteAction,
    next_runs,
)
from backend.deps import ServicesDep, client_addr, require
from backend.errors import Conflict, Invalid, NotFound
from backend.rbac import Principal
from hub import jalali
from hub.storage.automations import AutomationRecord, HolidayRecord, RunRecord, SceneRecord
from hub.storage.platform import PlatformError

router = APIRouter(prefix="/api/platform", tags=["automations"])
Viewer = Annotated[Principal, Depends(require("device.view"))]
Manager = Annotated[Principal, Depends(require("automation.manage"))]
Signed = Annotated[Principal, Depends(require("self"))]


def _orgs(p: Principal, permission: str, org_id: str) -> set[str] | None:
    if not p.can_in_org(permission, org_id):
        raise NotFound("organization not found")
    return p.orgs_with(permission)


def _nf(exc: PlatformError) -> Exception:
    return NotFound(str(exc)) if exc.code == "not_found" else Conflict(str(exc), code=exc.code)


async def _check_refs(svc: ServicesDep, org_id: str, *, writes: list[WriteAction],
                      reads: list[tuple[str, str]], scenes: list[int]) -> None:
    """Devices of this organization, existing tags, writable ones with a fitting value."""
    ids = sorted({w.device_id for w in writes} | {d for d, _ in reads})
    levels = await svc.store.accounts.access_levels({org_id}, ids) if ids else {}
    for device_id in ids:
        if device_id not in levels:
            raise Invalid(f"device {device_id} is not one of this organization's",
                          code="unknown_device")
    templates: dict[str, Any] = {}
    for device_id in ids:
        record = await svc.store.platform.device(None, device_id)
        templates[device_id] = await svc.store.platform.template(record.product_id,
                                                                 record.version)
    for device_id, tag in reads:
        try:
            templates[device_id].tag(tag)
        except KeyError:
            raise Invalid(f"{device_id} has no tag {tag}", code="unknown_tag") from None
    for w in writes:
        if levels[w.device_id] not in ("owner", "operator"):
            raise Invalid(f"device {w.device_id} is shared with this organization for viewing "
                          "only", code="view_only")
        try:
            t = templates[w.device_id].tag(w.tag)
        except KeyError:
            raise Invalid(f"{w.device_id} has no tag {w.tag}", code="unknown_tag") from None
        if str(t.access) != "read_write":
            raise Invalid(f"{w.tag} cannot be written", code="read_only_tag")
        if (t.data_type == "bool") != isinstance(w.value, bool):
            raise Invalid(f"{w.tag} takes a {t.data_type} value", code="invalid_value")
        if not isinstance(w.value, bool) and (
                t.min is not None and w.value < t.min or t.max is not None and w.value > t.max):
            raise Invalid(f"{w.tag}: {w.value} is outside {t.min} .. {t.max}",
                          code="invalid_value")
    for scene_id in scenes:
        try:
            scene = await svc.store.automations.scene(None, scene_id)
        except PlatformError:
            raise Invalid(f"scene {scene_id} not found", code="unknown_scene") from None
        if scene.org_id != org_id:
            raise Invalid(f"scene {scene_id} not found", code="unknown_scene")


async def _check_automation(svc: ServicesDep, org_id: str, spec: AutomationIn) -> None:
    reads = [(c.device_id, c.tag) for c in spec.conditions]
    if isinstance(spec.trigger, ValueTrigger):
        reads.append((spec.trigger.device_id, spec.trigger.tag))
    elif isinstance(spec.trigger, OfflineTrigger):           # offline: the device only
        levels = await svc.store.accounts.access_levels({org_id}, [spec.trigger.device_id])
        if spec.trigger.device_id not in levels:
            raise Invalid(f"device {spec.trigger.device_id} is not one of this "
                          "organization's", code="unknown_device")
    await _check_refs(svc, org_id, writes=[a for a in spec.actions if isinstance(a, WriteAction)],
                      reads=reads,
                      scenes=[a.scene_id for a in spec.actions if isinstance(a, SceneAction)])


# --- scenes ------------------------------------------------------------------------------------


@router.get("/organizations/{org_id}/scenes")
async def list_scenes(org_id: str, svc: ServicesDep, p: Viewer) -> list[SceneRecord]:
    return await svc.store.automations.scenes(_orgs(p, "device.view", org_id), org_id)


async def _save_scene(svc: ServicesDep, p: Principal, org_id: str, scene_id: int | None,
                      body: SceneIn, request: Request) -> SceneRecord:
    orgs = _orgs(p, "automation.manage", org_id)
    await _check_refs(svc, org_id, writes=[a for a in body.actions
                                           if isinstance(a, WriteAction)], reads=[], scenes=[])
    try:
        scene = await svc.store.automations.save_scene(
            orgs, org_id, scene_id, name=body.name, icon=body.icon,
            actions=[a.model_dump(mode="json") for a in body.actions], user_id=p.user_id)
    except PlatformError as exc:
        raise _nf(exc) from None
    await svc.store.add_auth_audit("scene_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "scene_id": scene.scene_id})
    return scene


@router.post("/organizations/{org_id}/scenes", status_code=201)
async def create_scene(org_id: str, body: SceneIn, svc: ServicesDep, p: Manager,
                       request: Request) -> SceneRecord:
    return await _save_scene(svc, p, org_id, None, body, request)


@router.put("/organizations/{org_id}/scenes/{scene_id}")
async def update_scene(org_id: str, scene_id: int, body: SceneIn, svc: ServicesDep,
                       p: Manager, request: Request) -> SceneRecord:
    return await _save_scene(svc, p, org_id, scene_id, body, request)


@router.delete("/organizations/{org_id}/scenes/{scene_id}", status_code=204)
async def delete_scene(org_id: str, scene_id: int, svc: ServicesDep, p: Manager,
                       request: Request) -> None:
    if not await svc.store.automations.delete_scene(_orgs(p, "automation.manage", org_id),
                                                    org_id, scene_id):
        raise NotFound("scene not found")
    await svc.store.add_auth_audit("scene_deleted", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "scene_id": scene_id})


@router.post("/organizations/{org_id}/scenes/{scene_id}/run")
async def run_scene(org_id: str, scene_id: int, svc: ServicesDep, p: Viewer,
                    request: Request) -> dict[str, Any]:
    """Run now, with the caller's own rights (a viewer's writes are refused one by one)."""
    try:
        scene = await svc.store.automations.scene(_orgs(p, "device.view", org_id), scene_id)
    except PlatformError as exc:
        raise _nf(exc) from None
    if scene.org_id != org_id:
        raise NotFound("scene not found")
    detail = await svc.automations.run_scene(p, scene)
    await svc.store.add_auth_audit("scene_run", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "scene_id": scene_id})
    return {"detail": detail}


# --- automations -------------------------------------------------------------------------------


class AutomationOut(AutomationRecord):
    next_runs: list[str] = []


async def _out(svc: ServicesDep, record: AutomationRecord) -> AutomationOut:
    spec = AutomationIn.from_record(record)
    upcoming: list[str] = []
    if record.enabled and isinstance(spec.trigger, TIME_TRIGGERS):
        eng = svc.automations
        now = eng.clock()
        today = now.astimezone(eng.tz).date()
        holidays = await eng.holidays(record.org_id, today, today + timedelta(days=400))
        upcoming = [t.isoformat() for t in next_runs(spec.trigger, now, eng.tz, holidays, 3)]
    return AutomationOut.model_validate(record.model_dump() | {"next_runs": upcoming})


@router.get("/organizations/{org_id}/automations")
async def list_automations(org_id: str, svc: ServicesDep, p: Viewer) -> list[AutomationOut]:
    rows = await svc.store.automations.automations(_orgs(p, "device.view", org_id), org_id)
    return [await _out(svc, r) for r in rows]


async def _save(svc: ServicesDep, p: Principal, org_id: str, automation_id: int | None,
                body: AutomationIn, request: Request) -> AutomationOut:
    orgs = _orgs(p, "automation.manage", org_id)
    await _check_automation(svc, org_id, body)
    try:
        record = await svc.store.automations.save_automation(
            orgs, org_id, automation_id, name=body.name, enabled=body.enabled,
            definition=body.definition(), user_id=p.user_id)
    except PlatformError as exc:
        raise _nf(exc) from None
    await svc.store.add_auth_audit("automation_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id,
                                           "automation_id": record.automation_id,
                                           "trigger": body.trigger.type})
    await _push(svc, org_id)
    return await _out(svc, record)


async def _push(svc: ServicesDep, org_id: str) -> None:
    """The home's devices get their local rules again (the automations changed)."""
    from backend.api.home import push_local_rules

    await push_local_rules(svc, org_id)


@router.post("/organizations/{org_id}/automations", status_code=201)
async def create_automation(org_id: str, body: AutomationIn, svc: ServicesDep, p: Manager,
                            request: Request) -> AutomationOut:
    return await _save(svc, p, org_id, None, body, request)


@router.put("/organizations/{org_id}/automations/{automation_id}")
async def update_automation(org_id: str, automation_id: int, body: AutomationIn,
                            svc: ServicesDep, p: Manager, request: Request) -> AutomationOut:
    return await _save(svc, p, org_id, automation_id, body, request)


@router.delete("/organizations/{org_id}/automations/{automation_id}", status_code=204)
async def delete_automation(org_id: str, automation_id: int, svc: ServicesDep, p: Manager,
                            request: Request) -> None:
    if not await svc.store.automations.delete_automation(
            _orgs(p, "automation.manage", org_id), org_id, automation_id):
        raise NotFound("automation not found")
    await svc.store.add_auth_audit("automation_deleted", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "automation_id": automation_id})
    await _push(svc, org_id)


async def _record(svc: ServicesDep, p: Principal, org_id: str, automation_id: int,
                  permission: str) -> AutomationRecord:
    try:
        record = await svc.store.automations.automation(_orgs(p, permission, org_id),
                                                        automation_id)
    except PlatformError as exc:
        raise _nf(exc) from None
    if record.org_id != org_id:
        raise NotFound("automation not found")
    return record


@router.post("/organizations/{org_id}/automations/{automation_id}/test")
async def test_automation(org_id: str, automation_id: int, svc: ServicesDep, p: Manager
                          ) -> dict[str, Any]:
    """Dry run now: are the conditions met, what would be written. Nothing is sent."""
    record = await _record(svc, p, org_id, automation_id, "automation.manage")
    return await svc.automations.run(record, AutomationIn.from_record(record), "test", dry=True)


@router.get("/organizations/{org_id}/automations/{automation_id}/runs")
async def automation_runs(org_id: str, automation_id: int, svc: ServicesDep, p: Viewer,
                          limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[RunRecord]:
    await _record(svc, p, org_id, automation_id, "device.view")
    return await svc.store.automations.runs(_orgs(p, "device.view", org_id), automation_id,
                                            limit)


# --- holidays and the calendar -----------------------------------------------------------------


class HolidayIn(BaseModel):
    date: str = Field(description="Jalali date YYYY/MM/DD")
    name: str = Field(min_length=1, max_length=80)


class HolidayOut(BaseModel):
    date: str                  # Jalali
    gregorian: date
    name: str
    source: str                # fixed | platform | organization
    holiday_id: int | None = None


@router.get("/organizations/{org_id}/holidays")
async def holidays(org_id: str, svc: ServicesDep, p: Viewer,
                   year: Annotated[int, Query(ge=1300, le=1500)]) -> list[HolidayOut]:
    """Holidays of a Jalali year: the fixed solar ones, the platform's (e.g. the lunar
    holidays of that year) and the organization's own."""
    orgs = _orgs(p, "device.view", org_id)
    start, end = jalali.to_gregorian(year, 1, 1), jalali.to_gregorian(year + 1, 1, 1)
    out = [HolidayOut(date=jalali.fmt(year, m, d), gregorian=jalali.to_gregorian(year, m, d),
                      name=name, source="fixed")
           for (m, d), name in jalali.FIXED_HOLIDAYS.items()
           if d <= jalali.month_length(year, m)]
    for h in await svc.store.automations.holidays(orgs, org_id, start,
                                                  end - timedelta(days=1)):
        out.append(HolidayOut(date=jalali.fmt(*jalali.to_jalali(h.day)), gregorian=h.day,
                              name=h.name, source="organization" if h.org_id else "platform",
                              holiday_id=h.holiday_id))
    return sorted(out, key=lambda h: h.gregorian)


@router.post("/organizations/{org_id}/holidays", status_code=201)
async def add_holiday(org_id: str, body: HolidayIn, svc: ServicesDep, p: Manager,
                      request: Request) -> HolidayRecord:
    try:
        day = jalali.to_gregorian(*jalali.parse(body.date))
    except ValueError as exc:
        raise Invalid(str(exc), code="invalid_date") from None
    try:
        record = await svc.store.automations.add_holiday(
            _orgs(p, "automation.manage", org_id), org_id, day, body.name, p.user_id)
    except PlatformError as exc:
        raise _nf(exc) from None
    await svc.store.add_auth_audit("holiday_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "date": body.date})
    return record


class CalendarDay(BaseModel):
    jalali: str
    gregorian: date
    weekday: int               # 0 = Saturday
    weekday_name: str
    month_name: str
    holiday: str | None = None


@router.get("/calendar")
async def calendar(svc: ServicesDep, p: Signed,
                   day: Annotated[str | None, Query(description="Jalali YYYY/MM/DD")] = None
                   ) -> CalendarDay:
    eng = svc.automations
    d = jalali.to_gregorian(*jalali.parse(day)) if day else \
        eng.clock().astimezone(eng.tz).date()
    jy, jm, jd = jalali.to_jalali(d)
    return CalendarDay(jalali=jalali.fmt(jy, jm, jd), gregorian=d, weekday=jalali.weekday(d),
                       weekday_name=jalali.WEEKDAYS_FA[jalali.weekday(d)],
                       month_name=jalali.MONTHS_FA[jm - 1], holiday=jalali.fixed_holiday(d))



class PresenceIn(BaseModel):
    event: Literal["arrive", "leave"]


@router.post("/organizations/{org_id}/presence")
async def presence(org_id: str, body: PresenceIn, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    """The app reports that its user arrived home or left (its place against the home's,
    while the app is open): the home's presence automations run."""
    _orgs(p, "device.view", org_id)
    launched = await svc.automations.presence(org_id, body.event, p.user_id or 0)
    return {"event": body.event, "automations": launched}
