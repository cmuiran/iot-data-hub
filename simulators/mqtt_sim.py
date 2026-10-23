"""MQTT simulation: an embedded broker (amqtt) and JSON devices built from device config.

A simulated device publishes telemetry whose shape follows the tags' ``json_path`` (so the hub
driver's mapping is exercised for real), adds a device timestamp when ``timestamp_path`` is set,
announces ``online`` on its ``status_topic`` (retained), and applies ``{"tag": value}``
commands received on ``command_topic`` to its next telemetry message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import socket
from datetime import UTC, datetime
from typing import Any

import aiomqtt

from hub.codec import Value
from hub.models import DataType, MqttDevice, TimestampFormat, parse_json_path

log = logging.getLogger(__name__)
for _name in ("amqtt", "transitions", "passlib"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EmbeddedBroker:
    """Anonymous MQTT broker on 127.0.0.1 for tests and local development."""

    def __init__(self, port: int | None = None) -> None:
        self.host = "127.0.0.1"
        self.port = port or free_port()
        self._broker: Any = None

    async def start(self) -> None:
        from amqtt.broker import Broker

        config = {
            "listeners": {"default": {"type": "tcp", "bind": f"{self.host}:{self.port}"}},
            "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin":
                        {"allow_anonymous": True}},
        }
        self._broker = Broker(config)
        await self._broker.start()

    async def stop(self) -> None:
        if self._broker is not None:
            with contextlib.suppress(Exception):
                await self._broker.shutdown()
            self._broker = None

    async def __aenter__(self) -> EmbeddedBroker:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


def _set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    parts = parse_json_path(path)
    node: Any = doc
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        nxt = parts[i + 1] if not last else None
        if isinstance(part, int):
            while len(node) <= part:
                node.append(None)
            if last:
                node[part] = value
            else:
                if node[part] is None:
                    node[part] = [] if isinstance(nxt, int) else {}
                node = node[part]
        else:
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, [] if isinstance(nxt, int) else {})


class SimulatedMqttDevice:
    def __init__(self, device: MqttDevice, host: str, port: int, topic: str | None = None) -> None:
        self.device = device
        self.host = host
        self.port = port
        c = device.connection
        # Wildcard telemetry filters need a concrete topic to publish on.
        self.topic = topic or c.telemetry_topic.replace("+", device.device_id).replace("#", "data")
        self.values: dict[str, Value] = {t.name: _initial(t.data_type, t.min, t.max)
                                         for t in device.tags}
        self.commands: list[dict[str, Any]] = []
        self._client: aiomqtt.Client | None = None
        self._listener: asyncio.Task[None] | None = None
        self._t = 0.0

    async def connect(self) -> None:
        c = self.device.connection
        will = (aiomqtt.Will(c.status_topic, "offline", qos=1, retain=True)
                if c.status_topic else None)
        self._client = aiomqtt.Client(self.host, self.port,
                                      identifier=f"sim-{self.device.device_id}", will=will)
        await self._client.__aenter__()
        if c.command_topic:
            await self._client.subscribe(c.command_topic, qos=1)
            self._listener = asyncio.create_task(self._listen())
        if c.status_topic:
            await self._client.publish(c.status_topic, "online", qos=1, retain=True)

    async def disconnect(self, *, graceful: bool = True) -> None:
        c = self.device.connection
        if self._listener:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
        if self._client is not None:
            if graceful and c.status_topic:
                with contextlib.suppress(Exception):
                    await self._client.publish(c.status_topic, "offline", qos=1, retain=True)
            with contextlib.suppress(Exception):
                await self._client.__aexit__(None, None, None)
            self._client = None

    async def _listen(self) -> None:
        assert self._client is not None
        async for message in self._client.messages:
            try:
                command = json.loads(message.payload)  # type: ignore[arg-type]
            except ValueError:
                continue
            if isinstance(command, dict):
                self.commands.append(command)
                for name, value in command.items():
                    if name in self.values:
                        self.values[name] = value
                await self.publish()

    def payload(self, now: datetime | None = None) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        c = self.device.connection
        if c.timestamp_path and c.timestamp_format:
            now = now or datetime.now(UTC)
            ts: Any = {
                TimestampFormat.UNIX_S: int(now.timestamp()),
                TimestampFormat.UNIX_MS: int(now.timestamp() * 1000),
                TimestampFormat.ISO8601: now.isoformat(),
            }[c.timestamp_format]
            _set_path(doc, c.timestamp_path, ts)
        for tag in self.device.tags:
            _set_path(doc, tag.json_path, self.values[tag.name])
        return doc

    async def publish(self, payload: dict[str, Any] | bytes | None = None) -> None:
        assert self._client is not None, "connect() first"
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload if payload is not None else self.payload()
        )
        await self._client.publish(self.topic, body, qos=self.device.connection.qos)

    def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for tag in self.device.tags:
            if tag.access == "read" and tag.data_type in (DataType.FLOAT32, DataType.FLOAT64):
                lo = tag.min if tag.min is not None else 0.0
                hi = tag.max if tag.max is not None else 100.0
                self.values[tag.name] = round(
                    (lo + hi) / 2 + (hi - lo) * 0.3 * math.sin(self._t / 10 + len(tag.name)), 2
                )


def _initial(data_type: DataType, lo: float | None, hi: float | None) -> Value:
    if data_type is DataType.BOOL:
        return False
    if data_type is DataType.STRING:
        return "ok"
    mid = ((lo if lo is not None else 0) + (hi if hi is not None else 100)) / 2
    return float(mid) if data_type in (DataType.FLOAT32, DataType.FLOAT64) else int(mid)
