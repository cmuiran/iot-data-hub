"""/api/calc: calculated tags — list, formula versions, and preview before publishing.

Publishing (creating or changing a calculated device) goes through /api/config/devices and
needs ``config.edit`` + ``calc.publish``. The preview evaluates a definition once against the
current live values and stores nothing; it may only read tags the user may read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError

from backend.deps import ServicesDep, require
from backend.errors import Forbidden, Invalid, NotFound
from backend.rbac import Principal, split_path
from hub.calc.engine import definition_version, preview
from hub.calc.expr import CalcError
from hub.models import CalcDevice, CalcTag

router = APIRouter(prefix="/api/calc", tags=["calc"])
Reader = Annotated[Principal, Depends(require("data.read"))]


class PreviewIn(BaseModel):
    formula: str = Field(min_length=1, max_length=2000)
    inputs: dict[str, str] = Field(default_factory=dict)
    unit: str | None = None
    data_type: Literal["float64", "float32", "bool"] = "float64"
    missing: Literal["bad", "skip"] = "bad"
    stale_s: float = Field(default=300, ge=1, le=86_400)
    tag: str | None = Field(default=None, description="path the result will have (self-check)")


class InputValue(BaseModel):
    tag: str
    value: Any = None
    ts: datetime | None = None
    quality: str | None = None


class PreviewOut(BaseModel):
    valid: bool
    error: str | None = None
    error_position: int | None = None
    value: Any = None
    unit: str | None = None
    quality: str | None = None
    reason: str | None = None
    version: str | None = None
    inputs: list[InputValue] = []
    warnings: list[str] = []


class CalcOut(BaseModel):
    tag: str
    device_id: str
    name: str
    description: str | None
    formula: str
    inputs: dict[str, str]
    unit: str | None
    data_type: str
    version: str
    value: Any = None
    ts: datetime | None = None
    quality: str | None = None
    reason: str | None = None


def _readable(p: Principal, path: str) -> bool:
    site, area, _, _ = split_path(path)
    return p.can("data.read", site, area)


@router.get("/tags", response_model=list[CalcOut])
async def calc_tags(svc: ServicesDep, p: Reader) -> list[CalcOut]:
    out = []
    for device in svc.hub.devices.values():
        if not isinstance(device, CalcDevice):
            continue
        for tag in device.tags:
            path = device.tag_path(tag.name)
            if not _readable(p, path):
                continue
            s = svc.hub.latest.get(path)
            out.append(CalcOut(tag=path, device_id=device.device_id, name=tag.name,
                               description=tag.description, formula=tag.formula,
                               inputs=dict(tag.inputs), unit=tag.unit,
                               data_type=str(tag.data_type), version=definition_version(tag),
                               value=s.value if s else None, ts=s.timestamp if s else None,
                               quality=str(s.quality) if s else None,
                               reason=str(s.reason) if s and s.reason else None))
    return sorted(out, key=lambda c: c.tag)


@router.get("/tags/{tag_path}/versions")
async def versions(tag_path: str, svc: ServicesDep, p: Reader) -> list[dict[str, Any]]:
    """Every definition that was in force, newest first (valid_from = when it took effect)."""
    found = svc.hub.tags.get(tag_path)
    if found is None or not isinstance(found[1], CalcTag) or not _readable(p, tag_path):
        raise NotFound("no such calculated tag")
    return await svc.store.calc_versions(tag_path)


@router.post("/preview", response_model=PreviewOut)
async def calc_preview(body: PreviewIn, svc: ServicesDep, p: Reader) -> PreviewOut:
    try:
        tag = CalcTag(name="preview", formula=body.formula, inputs=body.inputs, unit=body.unit,
                      data_type=body.data_type, missing=body.missing, stale_s=body.stale_s)
    except ValidationError as exc:
        raise Invalid("definition is not valid", code="calc_invalid",
                      extra={"errors": [e["msg"] for e in exc.errors(include_url=False)]}
                      ) from exc
    for path in body.inputs.values():
        if path in svc.hub.tags and not _readable(p, path):
            raise Forbidden(f"no data.read for {path}")
    report = svc.hub.report
    catalogue = {path: t for path, (_d, t) in svc.hub.tags.items()}
    try:
        result = preview(tag, body.tag or "preview.preview.preview.preview", catalogue,
                         svc.hub.latest, svc.hub.clock(), report.building_model)
    except CalcError as exc:
        return PreviewOut(valid=False, error=exc.message, error_position=exc.pos)
    hidden = [path for path in result.inputs if not _readable(p, path)]
    if hidden:     # e.g. through sum_of(...) over a building with areas the user cannot read
        raise Forbidden(f"the formula reads tags you may not read ({len(hidden)})")
    s = result.sample
    return PreviewOut(
        valid=True, value=s.value, unit=body.unit, quality=str(s.quality),
        reason=str(s.reason) if s.reason else None, version=definition_version(tag),
        warnings=result.warnings,
        inputs=[InputValue(tag=path, value=v.value if v else None, ts=v.timestamp if v else None,
                           quality=str(v.quality) if v else None)
                for path, v in sorted(result.inputs.items())])
