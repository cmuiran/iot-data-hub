"""Northbound MQTT: publish tag values to another broker (head office, SCADA, cloud).

config/northbound.yaml lists targets; each target is one MQTT connection that publishes

* ``<topic_prefix>/<site>/<area>/<device_id>/<tag>`` -> ``{"v", "ts", "q", "u"}`` when a value or
  its quality changes, and again every ``heartbeat_s`` (so a consumer that starts late or
  missed a message catches up without retained messages);
* ``<topic_prefix>/status`` (retained) -> ``{"state": "online"|"offline", "node", "ts"}``:
  ``offline`` on a clean stop (shutdown, HA standby), and from the broker itself (last will)
  when the hub disappears.

While the broker is unreachable messages wait in a bounded queue (``queue_max``, oldest
dropped first and counted) and are sent in order after the reconnect. Only the active HA node
publishes. Nothing received from the broker is ever written to a device (output only).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
import random
from collections import deque
from datetime import UTC, datetime
from typing import Any

import aiomqtt

from hub.drivers.mqtt_json import broker_tls_context
from hub.models import Broker, NorthboundTarget

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class NorthboundPublisher:
    def __init__(self, services: Any, target: NorthboundTarget, broker: Broker) -> None:
        self.services = services
        self.target = target
        self.broker = broker
        self.queue: deque[tuple[str, bytes]] = deque(maxlen=target.queue_max)
        self.stats = {"published": 0, "dropped": 0, "reconnects": 0}
        self.connected = False
        self.last_error: str | None = None
        self._last: dict[str, tuple[Any, str, float]] = {}   # tag -> (value, quality, sent at)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # --- what to publish ----------------------------------------------------------------------

    def wanted(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, p) for p in self.target.tags)

    def topic(self, path: str) -> str:
        return f"{self.target.topic_prefix}/{path.replace('.', '/')}"

    def offer(self, sample: dict[str, Any], *, force: bool = False) -> bool:
        """Queue a sample (bus event format) if it changed, is due, or ``force``."""
        path = sample["tag"]
        if not self.wanted(path):
            return False
        now = asyncio.get_running_loop().time()
        last = self._last.get(path)
        if last is not None and not force:
            value, quality, sent = last
            changed = value != sample["value"] or quality != sample["quality"]
            if not changed and now - sent < self.target.heartbeat_s:
                return False
            if changed and now - sent < self.target.min_interval_s:
                return False
        found = self.services.hub.tags.get(path)
        unit = found[1].unit if found else None
        payload = {"v": sample["value"], "ts": sample["ts"].replace("+00:00", "Z"),
                   "q": sample["quality"], "u": unit}
        if len(self.queue) == self.queue.maxlen:
            self.stats["dropped"] += 1
        self.queue.append((self.topic(path), json.dumps(payload).encode()))
        self._last[path] = (sample["value"], sample["quality"], now)
        self._wake.set()
        return True

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [asyncio.create_task(self._consume(), name=f"nb:{self.target.name}:in"),
                       asyncio.create_task(self._send_loop(), name=f"nb:{self.target.name}:out"),
                       asyncio.create_task(self._heartbeat(), name=f"nb:{self.target.name}:hb")]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self.connected = False

    async def _consume(self) -> None:
        bus = self.services.hub.bus
        while True:
            queue = bus.subscribe()
            # publish the current values first, so a fresh consumer has everything
            for path, s in list(self.services.hub.latest.items()):
                self.offer({"tag": path, "value": s.value, "ts": s.timestamp.isoformat(),
                            "quality": str(s.quality)}, force=path not in self._last)
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        log.warning("northbound %s: fell behind the live bus; resubscribing",
                                    self.target.name)
                        break
                    if event.get("type") == "samples":
                        for sample in event["samples"]:
                            self.offer(sample)
            finally:
                bus.unsubscribe(queue)

    async def _heartbeat(self) -> None:
        period = max(1.0, min(self.target.heartbeat_s / 5, 30.0))
        while True:
            await asyncio.sleep(period)
            for path, s in list(self.services.hub.latest.items()):
                self.offer({"tag": path, "value": s.value, "ts": s.timestamp.isoformat(),
                            "quality": str(s.quality)})

    def _status(self, state: str) -> bytes:
        return json.dumps({"state": state, "node": self.services.settings.node_id,
                           "ts": _now()}).encode()

    def _client(self) -> aiomqtt.Client:
        b = self.broker
        username = os.environ.get(b.username_env) if b.username_env else None
        password = os.environ.get(b.password_env) if b.password_env else None
        status_topic = f"{self.target.topic_prefix}/status"
        return aiomqtt.Client(
            b.host, b.port, username=username, password=password, keepalive=b.keepalive_s,
            identifier=f"hub-nb-{self.services.settings.node_id}-{self.target.name}",
            will=aiomqtt.Will(status_topic, self._status("offline"), qos=1, retain=True),
            tls_context=broker_tls_context(b) if b.tls else None, timeout=10)

    async def _send_loop(self) -> None:
        delay = 1.0
        status_topic = f"{self.target.topic_prefix}/status"
        while True:
            try:
                async with self._client() as client:
                    self.connected, self.last_error, delay = True, None, 1.0
                    log.info("northbound %s: connected to %s:%d", self.target.name,
                             self.broker.host, self.broker.port)
                    await client.publish(status_topic, self._status("online"), qos=1, retain=True)
                    try:
                        while True:
                            while self.queue:
                                topic, payload = self.queue[0]
                                await client.publish(topic, payload, qos=self.target.qos,
                                                     retain=self.target.retain)
                                self.queue.popleft()       # only once the broker took it
                                self.stats["published"] += 1
                            self._wake.clear()
                            if not self.queue:
                                await self._wake.wait()
                    except asyncio.CancelledError:
                        # clean stop (shutdown, HA standby): say so, the will is only for crashes
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(client.publish(
                                status_topic, self._status("offline"), qos=1, retain=True), 2)
                        raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep trying; values wait in the queue
                if self.connected:
                    self.stats["reconnects"] += 1
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("northbound %s: %s (retry in %.0f s, %d queued)", self.target.name,
                            self.last_error, delay, len(self.queue))
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))  # nosec B311  # jitter
                delay = min(delay * 2, 60.0)

    def describe(self) -> dict[str, Any]:
        return {"name": self.target.name, "broker": self.broker.broker_id,
                "topic_prefix": self.target.topic_prefix, "connected": self.connected,
                "queued": len(self.queue), "last_error": self.last_error, **self.stats}


class Northbound:
    """All targets of config/northbound.yaml."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.publishers: list[NorthboundPublisher] = []
        self._running = False

    def _build(self) -> list[NorthboundPublisher]:
        report = self.services.report
        out = []
        for target in report.northbound.targets:
            broker = report.brokers.get(target.broker_ref)
            if broker is not None:
                out.append(NorthboundPublisher(self.services, target, broker))
        return out

    async def start(self) -> None:
        self.publishers = self._build()
        for p in self.publishers:
            await p.start()
        self._running = True

    async def stop(self) -> None:
        for p in self.publishers:
            await p.stop()
        self._running = False

    async def reload(self) -> None:
        if self._running:
            await self.stop()
            await self.start()

    def describe(self) -> list[dict[str, Any]]:
        return [p.describe() for p in self.publishers]
