"""Northbound MQTT: publish tag values to another broker (head office, SCADA, cloud).

config/northbound.yaml lists targets; each target is one MQTT connection that publishes

* ``<topic_prefix>/<site>/<area>/<device_id>/<tag>`` -> ``{"v", "ts", "q", "u"}`` when a value or
  its quality changes, and again every ``heartbeat_s`` (so a consumer that starts late or
  missed a message catches up without retained messages);
* ``<topic_prefix>/status`` (retained) -> ``{"state": "online"|"offline", "node", "ts"}``:
  ``offline`` on a clean stop (shutdown, HA standby), and from the broker itself (last will)
  when the hub disappears.

While the broker is unreachable messages wait in a bounded memory queue (``queue_max``) and,
with ``store_and_forward`` (default), spill to a queue on disk (``HUB_SPOOL_DIR``, survives
restarts, bounded by ``spool_max_mb``, oldest dropped first and counted). After the reconnect
the disk queue is sent first, then the memory queue, so the order is kept. Only the active HA node
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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import aiomqtt

from hub.drivers.mqtt_json import broker_tls_context
from hub.models import Broker, KafkaTarget, NorthboundTarget
from hub.spool import DiskQueue

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class NorthboundPublisher:
    SPOOL_PREFIX = "northbound"

    def __init__(self, services: Any, target: NorthboundTarget, broker: Broker) -> None:
        self.services = services
        self.target = target
        self.broker = broker
        spool_dir = getattr(services.settings, "spool_dir", None)
        self.spool = (DiskQueue(spool_dir / f"{self.SPOOL_PREFIX}-{target.name}.db",
                                target.spool_max_mb * 2**20)
                      if spool_dir is not None and target.store_and_forward else None)
        # with a disk spool the memory queue only buffers until the next spill (it starts at
        # half of queue_max), so a burst is never dropped; the disk limit is the bound
        self.queue: deque[tuple[str, bytes]] = deque(
            maxlen=None if self.spool is not None else target.queue_max)
        self.stats = {"published": 0, "dropped": 0, "reconnects": 0}
        self.connected = False
        self.last_error: str | None = None
        self._last: dict[str, tuple[Any, str, float]] = {}   # tag -> (value, quality, sent at)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._spilling: asyncio.Task[None] | None = None

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
        self.queue.append((self.topic(path), self._encode(path, payload)))
        if (self.spool is not None and not self.connected
                and len(self.queue) >= max(1, self.target.queue_max // 2)
                and (self._spilling is None or self._spilling.done())):
            self._spilling = asyncio.create_task(self._spill())
        self._last[path] = (sample["value"], sample["quality"], now)
        self._wake.set()
        return True

    def _encode(self, path: str, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload).encode()

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
                            await self._send_spool(client)      # older messages first
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
                if self.spool is not None:
                    with contextlib.suppress(Exception):
                        await self._spill()
                log.warning("northbound %s: %s (retry in %.0f s, %d queued, %d on disk)",
                            self.target.name, self.last_error, delay, len(self.queue),
                            self.spool.count if self.spool is not None else 0)
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))  # nosec B311  # jitter
                delay = min(delay * 2, 60.0)

    # --- store & forward -------------------------------------------------------------------

    async def _spill(self) -> None:
        """Move the memory queue to disk, oldest first."""
        assert self.spool is not None
        items = []
        while self.queue:
            topic, payload = self.queue.popleft()
            items.append(topic.encode() + b"\n" + payload)
        if items:
            self.stats["dropped"] += await self.spool.put(items)

    async def _send_spool(self, client: aiomqtt.Client) -> None:
        if self.spool is None:
            return
        if self._spilling is not None and not self._spilling.done():
            await self._spilling
        while self.spool.count:
            last_id = None
            try:
                for item_id, blob in await self.spool.peek(100):
                    topic, _, payload = blob.partition(b"\n")
                    await client.publish(topic.decode(), payload, qos=self.target.qos,
                                         retain=self.target.retain)
                    last_id = item_id
                    self.stats["published"] += 1
            finally:
                if last_id is not None:
                    await self.spool.ack(last_id)          # only what the broker took

    def describe(self) -> dict[str, Any]:
        return {"name": self.target.name, "type": "mqtt", "broker": self.broker.broker_id,
                "topic_prefix": self.target.topic_prefix, "connected": self.connected,
                "queued": len(self.queue), "last_error": self.last_error,
                "on_disk": self.spool.count if self.spool is not None else 0, **self.stats}


class KafkaPublisher(NorthboundPublisher):
    """The same change detection, heartbeat and store & forward, sent to an Apache Kafka topic:
    key = tag path, value = ``{"tag", "v", "ts", "q", "u", "node"}``. A batch leaves the queue
    only after Kafka acknowledged every message of it (``acks: all`` + idempotent producer)."""

    SPOOL_PREFIX = "kafka"
    BATCH = 500

    def __init__(self, services: Any, target: KafkaTarget,
                 producer_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(services, target, None)   # type: ignore[arg-type]
        self.target: KafkaTarget = target          # type: ignore[assignment]
        self._producer_factory = producer_factory or self._producer

    def topic(self, path: str) -> str:
        return path                                # the message key

    def _encode(self, path: str, payload: dict[str, Any]) -> bytes:
        return json.dumps({"tag": path, **payload,
                           "node": self.services.settings.node_id}).encode()

    def _producer(self) -> Any:
        from aiokafka import AIOKafkaProducer
        from aiokafka.helpers import create_ssl_context

        t = self.target

        def env(name: str | None, what: str) -> str | None:
            if name is None:
                return None
            value = os.environ.get(name)
            if not value:
                raise RuntimeError(f"environment variable {name} ({what}) is not set")
            return value

        kwargs: dict[str, Any] = {
            "bootstrap_servers": t.bootstrap_servers,
            "client_id": f"hub-{self.services.settings.node_id}-{t.name}",
            "acks": t.acks, "enable_idempotence": t.acks == "all",
            "compression_type": None if t.compression == "none" else t.compression,
            "linger_ms": t.linger_ms, "security_protocol": t.security_protocol,
            "request_timeout_ms": 30_000,
        }
        if t.security_protocol in ("SSL", "SASL_SSL"):
            kwargs["ssl_context"] = create_ssl_context(
                cafile=env(t.ca_file_env, "Kafka CA file"),
                certfile=env(t.client_certificate_env, "Kafka client certificate"),
                keyfile=env(t.client_private_key_env, "Kafka client key"))
        if t.security_protocol == "SASL_SSL":
            kwargs |= {"sasl_mechanism": t.sasl_mechanism,
                       "sasl_plain_username": env(t.username_env, "Kafka user"),
                       "sasl_plain_password": env(t.password_env, "Kafka password")}
        return AIOKafkaProducer(**kwargs)

    async def _deliver(self, producer: Any, items: list[tuple[str, bytes]]) -> None:
        futures = [await producer.send(self.target.topic, value=payload, key=key.encode())
                   for key, payload in items]
        await asyncio.gather(*futures)             # every message acknowledged

    async def _send_spool(self, client: Any) -> None:
        if self.spool is None:
            return
        if self._spilling is not None and not self._spilling.done():
            await self._spilling
        while self.spool.count:
            rows = await self.spool.peek(self.BATCH)
            items = []
            for _, blob in rows:
                key, _, payload = blob.partition(b"\n")
                items.append((key.decode(), payload))
            await self._deliver(client, items)
            await self.spool.ack(rows[-1][0])
            self.stats["published"] += len(items)

    async def _send_loop(self) -> None:
        delay = 1.0
        while True:
            producer = None
            try:
                producer = self._producer_factory()
                await producer.start()
                self.connected, self.last_error, delay = True, None, 1.0
                log.info("kafka %s: connected to %s", self.target.name,
                         ",".join(self.target.bootstrap_servers))
                while True:
                    await self._send_spool(producer)          # older messages first
                    while self.queue:
                        batch = [self.queue[i] for i in range(min(self.BATCH, len(self.queue)))]
                        await self._deliver(producer, batch)
                        for _ in batch:
                            self.queue.popleft()               # only once Kafka took them
                        self.stats["published"] += len(batch)
                    self._wake.clear()
                    if not self.queue:
                        await self._wake.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep trying; values wait in the queue
                if self.connected:
                    self.stats["reconnects"] += 1
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.spool is not None:
                    with contextlib.suppress(Exception):
                        await self._spill()
                log.warning("kafka %s: %s (retry in %.0f s, %d queued, %d on disk)",
                            self.target.name, self.last_error, delay, len(self.queue),
                            self.spool.count if self.spool is not None else 0)
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))  # nosec B311  # jitter
                delay = min(delay * 2, 60.0)
            finally:
                if producer is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(producer.stop(), 10)

    def describe(self) -> dict[str, Any]:
        return {"name": self.target.name, "type": "kafka",
                "bootstrap_servers": self.target.bootstrap_servers, "topic": self.target.topic,
                "connected": self.connected, "queued": len(self.queue),
                "last_error": self.last_error,
                "on_disk": self.spool.count if self.spool is not None else 0, **self.stats}


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
        for kafka in report.northbound.kafka:
            out.append(KafkaPublisher(self.services, kafka))
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
