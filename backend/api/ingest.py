"""/ingest/v1: native devices over HTTP, and the platform broker's auth / ACL hooks.

Devices sign in with their credential: ``Authorization: Bearer <device secret>``.

* ``POST /ingest/v1/devices/{id}/telemetry``  ``{"ts": ..., "values": {...}}``
* ``POST /ingest/v1/devices/{id}/state``      ``{"state": "online" | "offline"}``
* ``POST /ingest/v1/devices/{id}/ack``        ``{"command_id": ..., "status": ...}``
* ``GET  /ingest/v1/devices/{id}/commands``   waiting commands (they count as sent)

Broker hooks (EMQX HTTP authentication / authorization, mosquitto-go-auth http backend),
called by the broker with ``X-Hub-Hook-Token: <HUB_BROKER_HOOK_TOKEN>``:

* ``POST /ingest/v1/broker/auth``  ``{"username": <device_id>, "password": <secret>}``
* ``POST /ingest/v1/broker/acl``   ``{"username": <device_id>, "topic": ..., "action":
  "publish" | "subscribe"}``

Both answer ``{"result": "allow" | "deny"}`` (HTTP 200 either way, as the brokers expect).
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from pydantic import BaseModel, Field

from backend.deps import ServicesDep, client_addr
from backend.errors import ApiError, Forbidden, Invalid, TooManyRequests, Unauthorized
from backend.ingest import DeviceAuthError
from backend.mqtt_ingest import acl_allows
from hub.drivers.native import NativeDriver

router = APIRouter(prefix="/ingest/v1", tags=["ingest"])
Payload = Annotated[Any, Body()]


async def _device(svc: ServicesDep, device_id: str, authorization: str | None,
                  request: Request) -> NativeDriver:
    scheme, _, secret = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secret:
        raise Unauthorized("Authorization: Bearer <device secret>", code="device_auth")
    try:
        return await svc.ingest.authenticate(device_id, secret.strip(), client_addr(request),
                                             "http")
    except DeviceAuthError as exc:
        if "too many" in str(exc):
            raise TooManyRequests(str(exc), code="device_auth_throttled") from None
        raise Unauthorized(str(exc), code="device_auth") from None


def _invalid(exc: ValueError) -> ApiError:
    return Invalid(str(exc), code="invalid_message")


@router.post("/devices/{device_id}/telemetry")
async def telemetry(device_id: str, payload: Payload, svc: ServicesDep, request: Request,
                    authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    driver = await _device(svc, device_id, authorization, request)
    try:
        n = await svc.ingest.telemetry(driver, payload)
    except ValueError as exc:
        raise _invalid(exc) from None
    return {"accepted": n, "commands": len(svc.ingest.queues.get(device_id, ()))}


@router.post("/devices/{device_id}/state")
async def state(device_id: str, payload: Payload, svc: ServicesDep, request: Request,
                authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    driver = await _device(svc, device_id, authorization, request)
    await svc.ingest.state(driver, payload)
    return {"ok": True}


@router.post("/devices/{device_id}/ack")
async def ack(device_id: str, payload: Payload, svc: ServicesDep, request: Request,
              authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    driver = await _device(svc, device_id, authorization, request)
    try:
        status = await svc.ingest.ack(driver, payload, "http")
    except ValueError as exc:
        raise _invalid(exc) from None
    return {"status": status}


@router.get("/devices/{device_id}/commands")
async def commands(device_id: str, svc: ServicesDep, request: Request,
                   authorization: Annotated[str | None, Header()] = None
                   ) -> list[dict[str, Any]]:
    driver = await _device(svc, device_id, authorization, request)
    return await svc.ingest.fetch(driver)


class BrokerAuthIn(BaseModel):
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)
    clientid: str | None = Field(default=None, max_length=200)


class BrokerAclIn(BaseModel):
    username: str = Field(default="", max_length=200)
    topic: str = Field(default="", max_length=500)
    action: str = Field(default="", max_length=20)


def _hook(svc: ServicesDep, token: str | None) -> None:
    expected = svc.settings.broker_hook_token
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise Forbidden("broker hook token missing or wrong", code="hook_token")


@router.post("/broker/auth")
async def broker_auth(body: BrokerAuthIn, svc: ServicesDep, request: Request,
                      x_hub_hook_token: Annotated[str | None, Header()] = None
                      ) -> dict[str, str]:
    _hook(svc, x_hub_hook_token)
    try:
        await svc.ingest.authenticate(body.username, body.password, client_addr(request),
                                      "mqtt")
    except DeviceAuthError:
        return {"result": "deny"}
    return {"result": "allow"}


@router.post("/broker/acl")
async def broker_acl(body: BrokerAclIn, svc: ServicesDep,
                     x_hub_hook_token: Annotated[str | None, Header()] = None
                     ) -> dict[str, str]:
    _hook(svc, x_hub_hook_token)
    allowed = svc.ingest.driver(body.username) is not None and acl_allows(
        body.username, body.topic, body.action, svc.settings.platform_topic_prefix)
    return {"result": "allow" if allowed else "deny"}
