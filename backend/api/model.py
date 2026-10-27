"""/api/model: the building information model (config/building.yaml), read-only, plus the
maintenance history of equipment.

Visibility follows ``data.read``: an equipment is visible when the user may read one of its
points (or, without points, its device / its site); a location is visible when the user may read
its whole site, or when it contains a visible equipment (so area-limited users see the path to
their equipment and nothing else). Hidden objects answer like unknown ones (404).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.deps import ServicesDep, client_addr, require
from backend.errors import Forbidden, NotFound
from backend.rbac import Principal, split_path
from hub.building import BuildingModel
from hub.models import Equipment
from hub.storage.records import MaintenanceRecord

router = APIRouter(prefix="/api/model", tags=["model"])
Reader = Annotated[Principal, Depends(require("data.read"))]


class LocationOut(BaseModel):
    id: str
    kind: str
    name: str
    parent: str | None
    site: str | None                 # inherited from the campus
    organization: str | None
    level: int | None = None
    area_m2: float | None = None
    description: str | None = None
    haystack: list[str] = []
    brick_class: str | None = None
    equipment: int = 0               # visible equipment in this location and below


class PointOut(BaseModel):
    role: str
    tag: str
    description: str | None = None
    unit: str | None = None
    data_type: str
    access: str
    write_class: str | None = None
    min: float | None = None
    max: float | None = None
    haystack: list[str] = []
    brick_class: str | None = None
    value: Any = None
    ts: datetime | None = None
    received_at: datetime | None = None
    quality: str | None = None
    reason: str | None = None


class EquipmentOut(BaseModel):
    id: str
    name: str
    type: str
    location: str
    location_path: list[str]         # names from the campus down
    system: str | None = None
    device: str | None = None
    status: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    installed_on: str | None = None
    warranty_until: str | None = None
    description: str | None = None
    documents: list[dict[str, str]] = []
    haystack: list[str] = []
    brick_class: str | None = None
    points: list[PointOut] | None = None


class SystemOut(BaseModel):
    id: str
    kind: str
    name: str
    building: str
    description: str | None = None


class MaintenanceIn(BaseModel):
    performed_at: datetime
    kind: Literal["inspection", "preventive", "corrective", "calibration", "replacement", "note"]
    description: str = Field(min_length=1, max_length=2000)
    performed_by: str | None = Field(default=None, max_length=200)
    work_order: str | None = Field(default=None, max_length=100)


# --- visibility -----------------------------------------------------------------------------------


def _model(svc: ServicesDep) -> BuildingModel:
    return svc.hub.report.building_model


def _tag_readable(p: Principal, path: str) -> bool:
    site, area, _, _ = split_path(path)
    return p.can("data.read", site, area)


def _equipment_visible(svc: ServicesDep, p: Principal, e: Equipment) -> bool:
    if e.points:
        return any(_tag_readable(p, pt.tag) for pt in e.points)
    device = svc.hub.devices.get(e.device) if e.device else None
    if device is not None:
        return p.can("data.read", device.site, device.area)
    site = _model(svc).site_of(e.location)
    return site is not None and p.can("data.read", site, None)


def _visible_sets(svc: ServicesDep, p: Principal) -> tuple[set[str], set[str]]:
    """(visible location ids, visible equipment ids)."""
    model = _model(svc)
    equipment = {e.id for e in model.file.equipment if _equipment_visible(svc, p, e)}
    locations = {loc.id for loc in model.file.locations
                 if (site := model.site_of(loc.id)) is not None and p.can("data.read", site, None)}
    for eid in equipment:
        locations |= {loc.id for loc in model.ancestors(model.equipment[eid].location)}
    return locations, equipment


def _location_out(model: BuildingModel, loc_id: str, equipment: set[str]) -> LocationOut:
    loc = model.locations[loc_id]
    site = model.site_of(loc_id)
    inside = set(model.subtree(loc_id))
    count = sum(1 for e in model.file.equipment if e.id in equipment and e.location in inside)
    return LocationOut(id=loc.id, kind=str(loc.kind), name=loc.name, parent=loc.parent,
                       site=site, organization=model.organization_of_site(site) if site else None,
                       level=loc.level, area_m2=loc.area_m2, description=loc.description,
                       haystack=list(loc.haystack), brick_class=loc.brick_class,
                       equipment=count)


def _point_out(svc: ServicesDep, point: Any) -> PointOut | None:
    found = svc.hub.tags.get(point.tag)
    if found is None:
        return None
    _device, tag = found
    sample = svc.hub.latest.get(point.tag)
    return PointOut(role=point.role, tag=point.tag, description=point.description or
                    tag.description, unit=tag.unit, data_type=str(tag.data_type),
                    access=str(tag.access), write_class=tag.write_class and str(tag.write_class),
                    min=tag.min, max=tag.max, haystack=list(point.haystack),
                    brick_class=point.brick_class,
                    value=sample.value if sample else None,
                    ts=sample.timestamp if sample else None,
                    received_at=sample.received_at if sample else None,
                    quality=str(sample.quality) if sample else None,
                    reason=str(sample.reason) if sample and sample.reason else None)


def _equipment_out(svc: ServicesDep, p: Principal, e: Equipment, detail: bool) -> EquipmentOut:
    model = _model(svc)
    points = None
    if detail:
        points = [out for pt in e.points if _tag_readable(p, pt.tag)
                  and (out := _point_out(svc, pt)) is not None]
    return EquipmentOut(
        id=e.id, name=e.name, type=e.type, location=e.location,
        location_path=[loc.name for loc in reversed(model.ancestors(e.location))],
        system=e.system, device=e.device, status=str(e.status), manufacturer=e.manufacturer,
        model=e.model, serial_number=e.serial_number,
        installed_on=e.installed_on.isoformat() if e.installed_on else None,
        warranty_until=e.warranty_until.isoformat() if e.warranty_until else None,
        description=e.description, documents=[d.model_dump() for d in e.documents],
        haystack=list(e.haystack), brick_class=e.brick_class, points=points)


def _get_equipment(svc: ServicesDep, p: Principal, equipment_id: str) -> Equipment:
    e = _model(svc).equipment.get(equipment_id)
    if e is None or not _equipment_visible(svc, p, e):
        raise NotFound("no such equipment")
    return e


# --- routes ---------------------------------------------------------------------------------------


@router.get("/locations", response_model=list[LocationOut])
async def locations(svc: ServicesDep, p: Reader) -> list[LocationOut]:
    """Every visible location, parents before children (build the tree with ``parent``)."""
    model = _model(svc)
    visible, equipment = _visible_sets(svc, p)
    order = [loc_id for root in model.children.get(None, []) for loc_id in model.subtree(root)]
    return [_location_out(model, loc_id, equipment) for loc_id in order if loc_id in visible]


@router.get("/locations/{location_id}")
async def location(location_id: str, svc: ServicesDep, p: Reader) -> dict[str, Any]:
    model = _model(svc)
    visible, equipment = _visible_sets(svc, p)
    if location_id not in visible:
        raise NotFound("no such location")
    return {
        "location": _location_out(model, location_id, equipment),
        "path": [_location_out(model, loc.id, equipment)
                 for loc in reversed(model.ancestors(location_id))],
        "children": [_location_out(model, c, equipment)
                     for c in model.children.get(location_id, []) if c in visible],
        "equipment": [_equipment_out(svc, p, e, detail=False)
                      for e in model.equipment_in(location_id) if e.id in equipment],
    }


@router.get("/systems", response_model=list[SystemOut])
async def systems(svc: ServicesDep, p: Reader) -> list[SystemOut]:
    visible, _ = _visible_sets(svc, p)
    return [SystemOut(id=s.id, kind=str(s.kind), name=s.name, building=s.building,
                      description=s.description)
            for s in _model(svc).file.systems if s.building in visible]


@router.get("/equipment", response_model=list[EquipmentOut])
async def equipment_list(svc: ServicesDep, p: Reader, location: str | None = None,
                         system: str | None = None, type: str | None = None,  # noqa: A002
                         status: str | None = None) -> list[EquipmentOut]:
    model = _model(svc)
    inside = set(model.subtree(location)) if location else None
    out = []
    for e in model.file.equipment:
        if (inside is not None and e.location not in inside) or \
                (system and e.system != system) or (type and e.type != type) or \
                (status and str(e.status) != status):
            continue
        if _equipment_visible(svc, p, e):
            out.append(_equipment_out(svc, p, e, detail=False))
    return out


@router.get("/equipment/{equipment_id}", response_model=EquipmentOut)
async def equipment(equipment_id: str, svc: ServicesDep, p: Reader) -> EquipmentOut:
    return _equipment_out(svc, p, _get_equipment(svc, p, equipment_id), detail=True)


@router.get("/points/{tag_path}")
async def point(tag_path: str, svc: ServicesDep, p: Reader) -> dict[str, Any]:
    """Which equipment and role a tag belongs to (for the tag/device pages)."""
    if tag_path not in svc.hub.tags or not _tag_readable(p, tag_path):
        raise NotFound("unknown tag")
    found = _model(svc).point_of_tag.get(tag_path)
    if found is None:
        return {"tag": tag_path, "equipment": None, "role": None}
    equipment_id, role = found
    e = _model(svc).equipment[equipment_id]
    return {"tag": tag_path, "equipment": _equipment_out(svc, p, e, detail=False), "role": role}


@router.get("/equipment/{equipment_id}/maintenance", response_model=list[MaintenanceRecord])
async def maintenance(equipment_id: str, svc: ServicesDep, p: Reader,
                      limit: int = 200) -> list[MaintenanceRecord]:
    _get_equipment(svc, p, equipment_id)
    return await svc.store.maintenance_records(equipment_id, min(max(limit, 1), 1000))


@router.post("/equipment/{equipment_id}/maintenance", response_model=MaintenanceRecord,
             status_code=201)
async def add_maintenance(equipment_id: str, body: MaintenanceIn, request: Request,
                          svc: ServicesDep, p: Reader) -> MaintenanceRecord:
    e = _get_equipment(svc, p, equipment_id)
    scopes = [split_path(pt.tag)[:2] for pt in e.points] or [
        (_model(svc).site_of(e.location), None)]
    if not any(p.can("maintenance.write", site, area) for site, area in scopes):
        raise Forbidden("missing permission maintenance.write for this equipment")
    if not await svc.store.equipment_known(equipment_id):
        raise NotFound("equipment not in the database yet (configuration not synchronised)")
    record = await svc.store.add_maintenance_record(
        equipment_id=equipment_id, performed_at=body.performed_at, kind=body.kind,
        description=body.description, performed_by=body.performed_by,
        work_order=body.work_order, created_by=p.user_id)
    await svc.store.add_auth_audit("maintenance_recorded", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"equipment": equipment_id, "kind": body.kind,
                                           "record_id": record.record_id})
    return record
