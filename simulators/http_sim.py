"""A JSON/REST device for tests and local development (uvicorn + a small ASGI app).

Every configured device is served under ``/<device_id>``: each tag ``path`` answers a JSON
document whose structure follows the tags' JSON paths (``$.data.temp`` -> ``{"data":
{"temp": 23.4}}``). Optional authentication is checked like a real device would (basic,
bearer or an API-key header). Fault injection: ``status`` (HTTP status to answer with),
``garbage`` (answer non-JSON), ``offline`` (close the server).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import uvicorn

from hub.codec import Value
from hub.models import DataType, HttpDevice, HttpTag, parse_json_path
from simulators.mqtt_sim import free_port
from simulators.s7_sim import animated


def _put(doc: Any, parts: tuple[str | int, ...], value: object) -> Any:
    """Set ``value`` at the JSON path inside ``doc`` (creating objects/arrays as needed)."""
    head, rest = parts[0], parts[1:]
    if isinstance(head, int):
        doc = doc if isinstance(doc, list) else []
        while len(doc) <= head:
            doc.append(None)
        doc[head] = _put(doc[head], rest, value) if rest else value
        return doc
    doc = doc if isinstance(doc, dict) else {}
    doc[head] = _put(doc.get(head), rest, value) if rest else value
    return doc


class SimulatedHttpDevice:
    def __init__(self, device: HttpDevice, *, auth: dict[str, str] | None = None) -> None:
        self.device = device
        self.values: dict[str, Value | None] = {t.name: _initial(t) for t in device.tags}
        # {"basic": "user:pw"} | {"bearer": tok} | {"header": (n, v)}
        self.auth = auth or {}
        self.status = 200
        self.garbage = False
        self.requests = 0
        self._t = 0.0

    def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for tag in self.device.tags:
            if tag.data_type not in (DataType.BOOL, DataType.STRING):
                self.values[tag.name] = animated(tag, self._t)

    def document(self, path: str) -> object:
        doc: Any = {}
        for tag in self.device.tags:
            if tag.path == path and self.values.get(tag.name) is not None:
                doc = _put(doc, parse_json_path(tag.json_path), self.values[tag.name])
        conn = self.device.connection
        if conn.timestamp_path:
            stamp = datetime.now(UTC)
            ts: object = {"unix_s": stamp.timestamp(), "unix_ms": int(stamp.timestamp() * 1000),
                          "iso8601": stamp.isoformat()}[str(conn.timestamp_format)]
            doc = _put(doc, parse_json_path(conn.timestamp_path), ts)
        return doc

    def authorized(self, headers: dict[str, str]) -> bool:
        if "basic" in self.auth:
            expected = "Basic " + base64.b64encode(self.auth["basic"].encode()).decode()
            return headers.get("authorization") == expected
        if "bearer" in self.auth:
            return headers.get("authorization") == f"Bearer {self.auth['bearer']}"
        if "header" in self.auth:
            name, value = self.auth["header"]
            return headers.get(name.lower()) == value
        return True


def _initial(tag: HttpTag) -> Value:
    if tag.data_type is DataType.BOOL:
        return False
    if tag.data_type is DataType.STRING:
        return tag.name.upper()
    return animated(tag, 0.0)


class HttpSimServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1",
                 tls: tuple[str, str] | None = None) -> None:
        """``tls`` = (certificate file, key file): serve HTTPS."""
        self.host = host
        self.port = port or free_port()
        self.tls = tls
        self.devices: dict[str, SimulatedHttpDevice] = {}
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def add(self, sim: SimulatedHttpDevice) -> SimulatedHttpDevice:
        self.devices[sim.device.device_id] = sim
        return sim

    def base_url(self, device_id: str) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self.host}:{self.port}/{device_id}"

    async def _app(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        device_id, _, rest = scope["path"].lstrip("/").partition("/")
        path = f"/{rest}" if rest else ""
        query = scope.get("query_string", b"").decode()
        if query:
            path += f"?{query}"
        sim = self.devices.get(device_id)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        status, body, ctype = 404, b'{"error": "not found"}', "application/json"
        if sim is not None:
            sim.requests += 1
            if not sim.authorized(headers):
                status, body = 401, b'{"error": "unauthorized"}'
            elif sim.status != 200:
                status, body = sim.status, b'{"error": "simulated failure"}'
            elif sim.garbage:
                status, body, ctype = 200, b"<html>gateway error</html>", "text/html"
            else:
                status, body = 200, json.dumps(sim.document(path)).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype.encode())]})
        await send({"type": "http.response.body", "body": body})

    async def start(self) -> None:
        config = uvicorn.Config(self._app, host=self.host, port=self.port, loop="none",
                                interface="asgi3",
                                lifespan="off", log_level="warning", access_log=False,
                                ssl_certfile=self.tls[0] if self.tls else None,
                                ssl_keyfile=self.tls[1] if self.tls else None)
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._task, 5)
            self._server = None

    async def __aenter__(self) -> HttpSimServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


__all__ = ["HttpSimServer", "SimulatedHttpDevice"]
