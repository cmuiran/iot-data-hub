"""/api/config: devices, alarm rules and control policy, validated and applied live.

All routes need ``config.edit`` (engineer, admin). See backend/config_admin.py.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel

from backend.config_admin import ConfigAdmin
from backend.deps import ServicesDep, require
from backend.rbac import Principal

router = APIRouter(prefix="/api/config", tags=["config"])
Engineer = Annotated[Principal, Depends(require("config.edit"))]
JsonBody = Annotated[dict[str, Any], Body()]


class EnabledIn(BaseModel):
    enabled: bool


def _admin(svc: ServicesDep) -> ConfigAdmin:
    return svc.config_admin


# --- devices ------------------------------------------------------------------------------------


@router.get("/devices/{device_id}")
async def get_device(device_id: str, svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    return _admin(svc).device_data(device_id)


@router.post("/devices/validate")
async def validate_device(body: JsonBody, svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    """Dry run: schema errors and the issues the device would cause. Nothing is written."""
    return _admin(svc).validate_device(body)


@router.post("/devices", status_code=201)
async def create_device(body: JsonBody, svc: ServicesDep, p: Engineer) -> dict[str, Any]:
    return await _admin(svc).create_device(p, body)


@router.put("/devices/{device_id}")
async def replace_device(device_id: str, body: JsonBody, svc: ServicesDep, p: Engineer
                         ) -> dict[str, Any]:
    return await _admin(svc).replace_device(p, device_id, body)


@router.patch("/devices/{device_id}")
async def set_enabled(device_id: str, body: EnabledIn, svc: ServicesDep, p: Engineer
                      ) -> dict[str, Any]:
    return await _admin(svc).set_enabled(p, device_id, body.enabled)


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, svc: ServicesDep, p: Engineer) -> dict[str, Any]:
    return await _admin(svc).delete_device(p, device_id)


# --- alarm rules ----------------------------------------------------------------------------------


@router.get("/alarms")
async def get_alarms(svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    return svc.report.alarms.model_dump(mode="json")


@router.put("/alarms/rules")
async def put_alarm_rule(body: JsonBody, svc: ServicesDep, p: Engineer) -> dict[str, Any]:
    return await _admin(svc).put_alarm_rule(p, body)


@router.delete("/alarms/rules/{tag_path}")
async def delete_alarm_rule(tag_path: str, svc: ServicesDep, p: Engineer) -> dict[str, Any]:
    return await _admin(svc).delete_alarm_rule(p, tag_path)


# --- control policy -------------------------------------------------------------------------------


@router.get("/control")
async def get_control(svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    return svc.report.control.model_dump(mode="json")


@router.put("/control/defaults")
async def put_control_defaults(body: JsonBody, svc: ServicesDep, p: Engineer
                               ) -> dict[str, Any]:
    return await _admin(svc).put_control_policy(p, None, body)


@router.put("/control/tags/{tag_path}")
async def put_control_policy(tag_path: str, body: JsonBody, svc: ServicesDep, p: Engineer
                             ) -> dict[str, Any]:
    return await _admin(svc).put_control_policy(p, tag_path, body)


@router.delete("/control/tags/{tag_path}")
async def delete_control_policy(tag_path: str, svc: ServicesDep, p: Engineer
                                ) -> dict[str, Any]:
    return await _admin(svc).delete_control_policy(p, tag_path)


# --- reload ---------------------------------------------------------------------------------------


@router.post("/reload")
async def reload(svc: ServicesDep, p: Engineer) -> dict[str, Any]:
    """Re-read config/ after a manual edit and apply it (only changed drivers restart)."""
    return await _admin(svc).reload_from_disk(p)
