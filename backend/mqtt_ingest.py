"""Platform MQTT broker for native devices (HUB_PLATFORM_BROKER = a broker in brokers.yaml).

One subscription for all devices (not a connection per device):

    <prefix>d/+/telemetry   device -> hub   {"ts": ..., "values": {...}}
    <prefix>d/+/state       device -> hub   {"state": "online" | "offline"}  (use it as LWT)
    <prefix>d/+/ack         device -> hub   {"command_id": ..., "status": ...}
    <prefix>d/+/ota/status  device -> hub   {"job_id": ..., "status": ...}  (firmware)
    <prefix>d/<id>/ota      hub -> device   a firmware offer
    <prefix>d/<id>/cmd      hub -> device   {"command_id": ..., "tag": ..., "value": ...}

With HUB_PLATFORM_SHARED_GROUP the hub subscribes as ``$share/<group>/...`` so several hub
nodes share the load. The device id in the topic is trusted because the broker only lets a
device use its own topics: configure the broker's HTTP authentication and ACL hooks to
``/ingest/v1/broker/auth`` and ``/ingest/v1/broker/acl`` (backend.api.ingest). Without them
this transport is not authenticated; use HTTP or TCP instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

import aiomqtt

from backend.ingest import Ingest
from hub.drivers.mqtt_json import broker_tls_context
from hub.models import Broker

log = logging.getLogger(__name__)
MAX_PAYLOAD = 64 * 1024


class MqttIngest:
    def __init__(self, ingest: Ingest, broker: Broker, *, prefix: str = "",
                 shared_group: str | None = None, node_id: str = "hub") -> None:
        self.ingest = ingest
        self.broker = broker
        self.prefix = prefix
        self.shared = f"$share/{shared_group}/" if shared_group else ""
        self.node_id = node_id
        self.client: aiomqtt.Client | None = None
        self.connected = False
        self._task: asyncio.Task[None] | None = None
        self.counters = {"messages": 0, "dropped": 0}

    def _client(self) -> aiomqtt.Client:
        b = self.broker
        return aiomqtt.Client(
            b.host, b.port, keepalive=b.keepalive_s,
            username=os.environ.get(b.username_env) if b.username_env else None,
            password=os.environ.get(b.password_env) if b.password_env else None,
            identifier=f"hub-platform-{self.node_id}",
            tls_context=broker_tls_context(b) if b.tls else None, timeout=10)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ingest:mqtt")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.connected = False

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with self._client() as client:
                    self.client = client
                    for kind in ("telemetry", "state", "ack", "ota/status"):
                        await client.subscribe(f"{self.shared}{self.prefix}d/+/{kind}", qos=1)
                    self.connected = True
                    delay = 1.0
                    log.info("platform MQTT ingest connected to %s:%d", self.broker.host,
                             self.broker.port)
                    async for message in client.messages:
                        await self._message(str(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect with backoff
                log.warning("platform MQTT ingest: %s; retrying in %.0f s", exc, delay)
            finally:
                self.connected = False
                self.client = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _message(self, topic: str, payload: Any) -> None:
        self.counters["messages"] += 1
        rest = topic.removeprefix(self.prefix)
        parts = rest.split("/")
        if len(parts) not in (3, 4) or parts[0] != "d":
            return
        device_id, kind = parts[1], "/".join(parts[2:])
        driver = self.ingest.driver(device_id)
        if driver is None or "mqtt" not in driver.device.connection.transports:
            self.counters["dropped"] += 1
            return
        try:
            if not isinstance(payload, bytes | bytearray) or len(payload) > MAX_PAYLOAD:
                raise ValueError("payload too large")
            data = json.loads(payload)
            if kind == "telemetry":
                await self.ingest.telemetry(driver, data)
            elif kind == "state":
                await self.ingest.state(driver, data)
            elif kind == "ack":
                await self.ingest.ack(driver, data, "mqtt")
            elif kind == "ota/status":               # a firmware job report
                await self.ingest.ota(driver, data)
        except (ValueError, TypeError) as exc:
            self.counters["dropped"] += 1
            log.info("platform MQTT: message from %s refused: %s", device_id, exc)

    async def publish_command(self, device_id: str, message: dict[str, Any]) -> None:
        await self.publish(f"d/{device_id}/cmd", message)

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        if self.client is None:
            raise ConnectionError("platform broker not connected")
        await self.client.publish(f"{self.prefix}{topic}",
                                  json.dumps(message, separators=(",", ":")), qos=1)


def acl_allows(device_id: str, topic: str, action: str, prefix: str = "") -> bool:
    """A device may publish its telemetry / state / ack and subscribe to its commands, firmware
    offers, local rules and settings only."""
    own = f"{prefix}d/{device_id}/"
    if action == "publish":
        return topic in (own + "telemetry", own + "state", own + "ack", own + "ota/status")
    if action == "subscribe":
        return topic in (own + "cmd", own + "ota", own + "rules", own + "config")
    return False
