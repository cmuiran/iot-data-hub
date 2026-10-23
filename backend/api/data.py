"""/api/devices, /api/tags, /api/alarms: read access, filtered to what the user may see."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.deps import ServicesDep, client_addr, require
from backend.errors import Forbidden, Invalid, NotFound
from backend.rbac import Principal, split_path
from hub.drivers.base import utcnow
from hub.storage.records import AlarmRecord, HistoryPoint, LatestValue

router = APIRouter(tags=["data"])
Reader = Annotated[Principal, Depends(require("data.read"))]
AlarmAcker = Annotated[Principal, Depends(require("alarm.ack"))]


class TagOut(BaseModel):
    tag: str
    name: str
    description: str | None
    data_type: str
    unit: str | None
    access: str
    min: float | None
    max: float | None
    value: Any = None
    ts: datetime | None = None
    quality: str | None = None


class DeviceOut(BaseModel):
    device_id: str
    name: str
    site: str
    area: str
    protocol: str
    enabled: bool
    description: str | None
    state: str
    last_seen: datetime | None
    last_error: str | None
    tags: list[TagOut] | None = None


class AckIn(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


class ShelveIn(BaseModel):
    source: str = Field(min_length=3, max_length=300)
    minutes: int = Field(ge=1, le=24 * 60)       # shelving is always temporary (ISA-18.2)
    reason: str = Field(min_length=3, max_length=500)


class AlarmOut(AlarmRecord):
    shelved: dict[str, Any] | None = None


def _visible(p: Principal, site: str, area: str) -> bool:
    return p.can("data.read", site, area)


def _tag_out(svc: ServicesDep, path: str) -> TagOut:
    device, tag = svc.hub.tags[path]
    sample = svc.hub.latest.get(path)
    return TagOut(tag=path, name=tag.name, description=tag.description,
                  data_type=str(tag.data_type), unit=tag.unit, access=str(tag.access),
                  min=tag.min, max=tag.max,
                  value=sample.value if sample else None, ts=sample.timestamp if sample else None,
                  quality=str(sample.quality) if sample else None)


def _device_out(svc: ServicesDep, device_id: str, with_tags: bool) -> DeviceOut:
    d = svc.hub.devices[device_id]
    status = svc.hub.statuses.get(device_id)
    tags = [_tag_out(svc, d.tag_path(t.name)) for t in d.tags] if with_tags else None
    return DeviceOut(device_id=d.device_id, name=d.name, site=d.site, area=d.area,
                     protocol=d.protocol, enabled=d.enabled, description=d.description,
                     state=str(status.state) if status else "unknown",
                     last_seen=status.last_seen if status else None,
                     last_error=status.last_error if status else None, tags=tags)


# --- devices / tags -----------------------------------------------------------------------------


@router.get("/api/devices", response_model=list[DeviceOut])
async def devices(svc: ServicesDep, p: Reader, site: str | None = None,
                  area: str | None = None) -> list[DeviceOut]:
    out = []
    for device_id, d in sorted(svc.hub.devices.items()):
        if (site and d.site != site) or (area and d.area != area):
            continue
        if _visible(p, d.site, d.area):
            out.append(_device_out(svc, device_id, with_tags=False))
    return out


@router.get("/api/devices/{device_id}", response_model=DeviceOut)
async def device(device_id: str, svc: ServicesDep, p: Reader) -> DeviceOut:
    d = svc.hub.devices.get(device_id)
    if d is None or not _visible(p, d.site, d.area):
        raise NotFound("no such device")
    return _device_out(svc, device_id, with_tags=True)


@router.get("/api/tags", response_model=list[TagOut])
async def tags(svc: ServicesDep, p: Reader, site: str | None = None, area: str | None = None,
               device_id: str | None = None,
               paths: Annotated[list[str] | None, Query(alias="tag")] = None) -> list[TagOut]:
    wanted = set(paths) if paths else None
    out = []
    for path in sorted(svc.hub.tags):
        s, a, d, _ = split_path(path)
        if (site and s != site) or (area and a != area) or (device_id and d != device_id):
            continue
        if wanted is not None and path not in wanted:
            continue
        if _visible(p, s, a):
            out.append(_tag_out(svc, path))
    return out


@router.get("/api/tags/{tag_path}/latest", response_model=LatestValue)
async def tag_latest(tag_path: str, svc: ServicesDep, p: Reader) -> LatestValue:
    _check_tag(svc, p, tag_path)
    sample = svc.hub.latest.get(tag_path)
    if sample is not None:
        return LatestValue(tag_path=tag_path, value=sample.value, ts=sample.timestamp,
                           quality=sample.quality)
    stored = await svc.store.latest([tag_path])
    if not stored:
        raise NotFound("no value yet", code="no_value")
    return stored[0]


def _check_tag(svc: ServicesDep, p: Principal, tag_path: str) -> None:
    if tag_path not in svc.hub.tags:
        raise NotFound("no such tag")
    site, area, _, _ = split_path(tag_path)
    if not _visible(p, site, area):
        raise NotFound("no such tag")


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or utcnow()
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None:
        raise Invalid("start/end need a UTC offset (e.g. 2026-10-01T00:00:00Z)",
                      code="naive_datetime")
    if start >= end:
        raise Invalid("start must be before end", code="invalid_range")
    if end - start > timedelta(days=366):
        raise Invalid("at most one year per request", code="invalid_range")
    return start, end


@router.get("/api/tags/{tag_path}/history", response_model=list[HistoryPoint])
async def history(tag_path: str, svc: ServicesDep, p: Reader, start: datetime | None = None,
                  end: datetime | None = None,
                  max_points: Annotated[int, Query(ge=10, le=10_000)] = 1000
                  ) -> list[HistoryPoint]:
    _check_tag(svc, p, tag_path)
    s, e = _window(start, end)
    return await svc.store.history(tag_path, s, e, max_points)


@router.get("/api/tags/{tag_path}/history.csv")
async def history_csv(tag_path: str, svc: ServicesDep, p: Reader, start: datetime | None = None,
                      end: datetime | None = None) -> StreamingResponse:
    _check_tag(svc, p, tag_path)
    s, e = _window(start, end)
    points = await svc.store.history(tag_path, s, e, max_points=100_000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_utc", "value", "avg", "min", "max", "count", "quality"])
    for pt in points:
        writer.writerow([pt.ts.isoformat(), "" if pt.value is None else pt.value,
                         "" if pt.avg is None else pt.avg, "" if pt.min is None else pt.min,
                         "" if pt.max is None else pt.max, pt.count, pt.quality])
    name = tag_path.replace(".", "_")
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


# --- alarms ---------------------------------------------------------------------------------


def _alarm_visible(svc: ServicesDep, p: Principal, alarm: AlarmRecord) -> bool:
    if alarm.tag_path:
        site, area, _, _ = split_path(alarm.tag_path)
    elif alarm.device_id and alarm.device_id in svc.hub.devices:
        d = svc.hub.devices[alarm.device_id]
        site, area = d.site, d.area
    else:
        return p.can("data.read", None)  # device gone from config: anyone who reads data
    return _visible(p, site, area)


@router.get("/api/alarms", response_model=list[AlarmOut])
async def active_alarms(svc: ServicesDep, p: Reader) -> list[AlarmOut]:
    shelves = await svc.store.shelves(utcnow())
    return [AlarmOut(**a.model_dump(), shelved=shelves.get(a.source))
            for a in await svc.store.active_alarms() if _alarm_visible(svc, p, a)]


def _source_scope(svc: ServicesDep, source: str) -> tuple[str, str]:
    if source.startswith("device:"):
        d = svc.hub.devices.get(source.removeprefix("device:"))
        if d is None:
            raise NotFound("no such device")
        return d.site, d.area
    if source not in svc.hub.tags:
        raise NotFound("no such tag")
    site, area, _, _ = split_path(source)
    return site, area


@router.get("/api/alarms/shelves")
async def list_shelves(svc: ServicesDep, p: Reader) -> list[dict[str, Any]]:
    out = []
    for source, shelf in (await svc.store.shelves(utcnow())).items():
        try:
            site, area = _source_scope(svc, source)
        except NotFound:
            continue
        if _visible(p, site, area):
            out.append(shelf)
    return out


@router.post("/api/alarms/shelve", status_code=204)
async def shelve(body: ShelveIn, request: Request, svc: ServicesDep, p: AlarmAcker) -> None:
    site, area = _source_scope(svc, body.source)
    if not p.can("alarm.ack", site, area):
        raise Forbidden("no alarm.ack permission for this area")
    until = utcnow() + timedelta(minutes=body.minutes)
    await svc.store.shelve(body.source, p.user_id, body.reason, until)
    await svc.store.add_auth_audit("alarm_shelved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"source": body.source, "until": until.isoformat(),
                                           "reason": body.reason})
    svc.hub.bus.publish({"type": "alarm", "action": "shelved", "source": body.source,
                         "until": until.isoformat(), "by": p.username})


@router.delete("/api/alarms/shelve/{source}", status_code=204)
async def unshelve(source: str, request: Request, svc: ServicesDep, p: AlarmAcker) -> None:
    site, area = _source_scope(svc, source)
    if not p.can("alarm.ack", site, area):
        raise Forbidden("no alarm.ack permission for this area")
    if not await svc.store.unshelve(source):
        raise NotFound("not shelved")
    await svc.store.add_auth_audit("alarm_unshelved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request), detail={"source": source})
    svc.hub.bus.publish({"type": "alarm", "action": "unshelved", "source": source,
                         "by": p.username})


@router.get("/api/alarms/history", response_model=list[AlarmRecord])
async def alarm_history(svc: ServicesDep, p: Reader, since: datetime | None = None,
                        limit: Annotated[int, Query(ge=1, le=5000)] = 500) -> list[AlarmRecord]:
    return [a for a in await svc.store.alarm_history(since, limit) if _alarm_visible(svc, p, a)]


@router.post("/api/alarms/{alarm_id}/ack", response_model=AlarmRecord)
async def ack_alarm(alarm_id: int, body: AckIn, request: Request, svc: ServicesDep,
                    p: AlarmAcker) -> AlarmRecord:
    alarm = await svc.store.get_alarm(alarm_id)
    if alarm is None or not _alarm_visible(svc, p, alarm):
        raise NotFound("no such alarm")
    site = area = None
    if alarm.tag_path:
        site, area, _, _ = split_path(alarm.tag_path)
    elif alarm.device_id in svc.hub.devices:
        site, area = svc.hub.devices[alarm.device_id].site, svc.hub.devices[alarm.device_id].area
    if not p.can("alarm.ack", site, area):
        raise Forbidden("no alarm.ack permission for this area")
    acked = await svc.store.ack_alarm(alarm_id, p.user_id, body.comment)
    if acked is None:
        acked = alarm  # already acknowledged: idempotent
    else:
        await svc.store.add_auth_audit("alarm_acked", p.username, user_id=p.user_id,
                                       client_addr=client_addr(request),
                                       detail={"alarm_id": alarm_id, "source": alarm.source})
        svc.hub.bus.publish({"type": "alarm", "action": "acked", "alarm_id": alarm_id,
                             "source": alarm.source, "by": p.username})
    return acked
