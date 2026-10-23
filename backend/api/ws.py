"""/api/ws: live data over WebSocket.

Protocol (JSON text frames):

1. client -> ``{"type": "auth", "token": "<access token>"}`` within 10 s (never in the URL, so
   tokens do not end up in proxy logs)
2. server -> ``{"type": "snapshot", "tags": [...], "devices": [...], "alarms": [...]}``
3. server -> ``samples`` / ``status`` / ``alarm`` / ``write`` events, filtered to the user's
   data.read scope; client may narrow with ``{"type": "subscribe", "tags": [...]}`` (empty = all)
4. ``{"type": "ping"}`` -> ``{"type": "pong"}``

Close codes: 4401 not authenticated / session ended, 4403 no data.read, 1013 client too slow
or the node is an HA standby.
The session is re-checked every 60 s, so logout or disabling a user ends the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.errors import ApiError
from backend.rbac import Principal, split_path
from backend.services import Services

log = logging.getLogger(__name__)
router = APIRouter()

AUTH_TIMEOUT_S = 10.0
RECHECK_S = 60.0


class _Filter:
    def __init__(self, svc: Services, principal: Principal) -> None:
        self.svc = svc
        self.p = principal
        self.only: set[str] | None = None

    def tag(self, path: str) -> bool:
        if self.only is not None and path not in self.only:
            return False
        try:
            site, area, _, _ = split_path(path)
        except ValueError:
            return False
        return self.p.can("data.read", site, area)

    def device(self, device_id: str | None) -> bool:
        d = self.svc.hub.devices.get(device_id or "")
        return d is not None and self.p.can("data.read", d.site, d.area)

    def event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if kind == "samples":
            samples = [s for s in event["samples"] if self.tag(s["tag"])]
            return {"type": "samples", "samples": samples} if samples else None
        if kind == "status":
            return event if self.device(event.get("device_id")) else None
        if kind == "alarm":
            source = event.get("source", "")
            if source.startswith("device:"):
                return event if self.device(source.removeprefix("device:")) else None
            return event if self.tag(source) else None
        if kind == "write":
            return event if self.tag(event.get("tag", "")) else None
        return None


async def _snapshot(svc: Services, f: _Filter) -> dict[str, Any]:
    tags = [{"tag": s.tag, "value": s.value, "ts": s.timestamp.isoformat(),
             "quality": str(s.quality)} for path, s in sorted(svc.hub.latest.items())
            if f.tag(path)]
    devices = [{"device_id": d, "state": str(st.state),
                "last_seen": st.last_seen.isoformat() if st.last_seen else None,
                "last_error": st.last_error}
               for d, st in sorted(svc.hub.statuses.items()) if f.device(d)]
    alarms = []
    for a in await svc.store.active_alarms():
        if (a.tag_path and f.tag(a.tag_path)) or (a.device_id and f.device(a.device_id)):
            alarms.append(a.model_dump(mode="json"))
    return {"type": "snapshot", "tags": tags, "devices": devices, "alarms": alarms}


@router.websocket("/api/ws")
async def live(ws: WebSocket) -> None:
    svc: Services = ws.app.state.services
    await ws.accept()
    if svc.role != "active":   # HA standby: the client should reconnect to the active node
        await ws.close(code=1013)
        return
    try:
        first = await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_S)
        if not isinstance(first, dict) or first.get("type") != "auth" or not first.get("token"):
            raise ApiError("first message must be auth")
        principal = await svc.auth.principal(str(first["token"]))
    except (TimeoutError, ApiError, ValueError, json.JSONDecodeError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await ws.close(code=4401)
        return
    if not principal.can("data.read"):
        await ws.close(code=4403)
        return

    f = _Filter(svc, principal)
    queue = svc.hub.bus.subscribe()
    token = str(first["token"])
    try:
        await ws.send_json(await _snapshot(svc, f))
        reader = asyncio.create_task(_read_client(ws, f))
        loop = asyncio.get_running_loop()
        next_check = loop.time() + RECHECK_S
        while not reader.done():
            try:
                event = await asyncio.wait_for(queue.get(), 1.0)
            except TimeoutError:
                event = {}
            if event is None:  # dropped by the bus: this client is too slow
                await ws.close(code=1013)
                break
            if event:
                out = f.event(event)
                if out is not None:
                    await ws.send_json(out)
            if loop.time() >= next_check:
                next_check = loop.time() + RECHECK_S
                try:
                    f.p = await svc.auth.principal(token)
                except ApiError:
                    await ws.close(code=4401)
                    break
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket stream failed")
    finally:
        svc.hub.bus.unsubscribe(queue)


async def _read_client(ws: WebSocket, f: _Filter) -> None:
    while True:
        message = await ws.receive_json()
        if not isinstance(message, dict):
            continue
        if message.get("type") == "ping":
            await ws.send_json({"type": "pong"})
        elif message.get("type") == "subscribe":
            tags = message.get("tags") or []
            f.only = {str(t) for t in tags} if tags else None
            await ws.send_json({"type": "subscribed", "tags": sorted(f.only or [])})
