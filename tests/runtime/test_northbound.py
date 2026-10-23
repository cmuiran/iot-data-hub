"""Northbound MQTT publisher against the embedded broker: changes, filters, heartbeat, the queue
while the broker is down, and the online/offline status."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import aiomqtt
import pytest

from backend.northbound import NorthboundPublisher
from hub.models import Broker, NorthboundTarget, Quality, Sample
from hub.runtime import LiveBus, sample_event
from simulators.mqtt_sim import EmbeddedBroker, free_port

pytestmark = pytest.mark.integration

POWER = "kish.substation.meter_01.active_power"
TEMP = "kish.warehouse.env_sensor_01.temperature"


def services() -> Any:
    unit = SimpleNamespace(unit="kW")
    return SimpleNamespace(
        hub=SimpleNamespace(bus=LiveBus(), latest={},
                            tags={POWER: (None, unit),
                                  TEMP: (None, SimpleNamespace(unit="degC"))}),
        settings=SimpleNamespace(node_id="node_a"))


def sample(path: str, value: Any, quality: Quality = Quality.GOOD) -> Sample:
    return Sample(tag=path, value=value, timestamp=datetime.now(UTC), quality=quality)


def push(svc: Any, *samples: Sample) -> None:
    for s in samples:
        svc.hub.latest[s.tag] = s
    svc.hub.bus.publish(sample_event(list(samples)))


class Listener:
    def __init__(self, port: int, topic: str) -> None:
        self.port, self.topic = port, topic
        self.messages: list[tuple[str, dict[str, Any], bool]] = []
        self._task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()

    async def _run(self) -> None:
        async with aiomqtt.Client("127.0.0.1", self.port) as client:
            await client.subscribe(self.topic, qos=1)
            self.ready.set()
            async for m in client.messages:
                self.messages.append((str(m.topic), json.loads(m.payload), m.retain))

    async def __aenter__(self) -> Listener:
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self.ready.wait(), 10)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self._task

    def values(self, path: str) -> list[Any]:
        topic = "ho/" + path.replace(".", "/")
        return [p["v"] for t, p, _ in self.messages if t == topic]

    async def wait(self, predicate: Any, timeout: float = 10.0) -> None:
        async def _loop() -> None:
            while not predicate():
                await asyncio.sleep(0.05)
        await asyncio.wait_for(_loop(), timeout)


def target(**kw: Any) -> NorthboundTarget:
    return NorthboundTarget(name="ho", broker_ref="b", topic_prefix="ho",
                            tags=["kish.substation.*"], **kw)


async def test_publishes_changes_of_matching_tags() -> None:
    svc = services()
    push(svc, sample(POWER, 10.0))           # already known when the publisher starts
    async with EmbeddedBroker() as broker, Listener(broker.port, "ho/#") as lis:
        pub = NorthboundPublisher(svc, target(), Broker(broker_id="b", host="127.0.0.1",
                                                        port=broker.port))
        await pub.start()
        try:
            await lis.wait(lambda: lis.values(POWER) == [10.0])
            push(svc, sample(POWER, 10.0), sample(TEMP, 21.0))   # unchanged; not matching
            push(svc, sample(POWER, 12.5))
            push(svc, sample(POWER, 12.5, Quality.BAD))          # quality change is a change
            await lis.wait(lambda: len(lis.values(POWER)) == 3)
            await asyncio.sleep(0.3)
            assert lis.values(POWER) == [10.0, 12.5, 12.5] and lis.values(TEMP) == []
            payload = next(p for t, p, _ in lis.messages if t.endswith("active_power"))
            assert set(payload) == {"v", "ts", "q", "u"} and payload["u"] == "kW"
            assert payload["ts"].endswith("Z")
            status = [p for t, p, _ in lis.messages if t == "ho/status"]
            assert status and status[0]["state"] == "online" and status[0]["node"] == "node_a"
            assert pub.describe()["published"] >= 3 and pub.connected
        finally:
            await pub.stop()
        await lis.wait(lambda: any(t == "ho/status" and p["state"] == "offline"
                                   for t, p, _ in lis.messages))


async def test_heartbeat_and_rate_limit() -> None:
    svc = services()
    pub = NorthboundPublisher(svc, target(heartbeat_s=60, min_interval_s=5),
                              Broker(broker_id="b", host="127.0.0.1", port=1))
    s = {"tag": POWER, "value": 1.0, "ts": datetime.now(UTC).isoformat(), "quality": "good"}
    assert pub.offer(s) is True
    assert pub.offer(s) is False                                   # unchanged, not due
    assert pub.offer(s | {"value": 2.0}) is False                  # changed, but too soon
    value, quality, sent = pub._last[POWER]  # noqa: SLF001
    pub._last[POWER] = (value, quality, sent - 6)  # noqa: SLF001
    assert pub.offer(s | {"value": 2.0}) is True                   # 6 s later: sent
    pub._last[POWER] = (2.0, "good", sent - 61)  # noqa: SLF001
    assert pub.offer(s | {"value": 2.0}) is True                   # heartbeat
    assert pub.offer({"tag": TEMP, "value": 1, "ts": s["ts"], "quality": "good"}) is False


async def test_queue_while_the_broker_is_down() -> None:
    svc = services()
    port = free_port()
    pub = NorthboundPublisher(svc, target(queue_max=10), Broker(broker_id="b", host="127.0.0.1",
                                                                port=port))
    await pub.start()
    try:
        for i in range(15):
            push(svc, sample(POWER, float(i)))
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        assert not pub.connected and len(pub.queue) == 10 and pub.stats["dropped"] == 5
        for _ in range(100):            # a refused connect can take ~2 s on Windows
            if pub.last_error:
                break
            await asyncio.sleep(0.1)
        assert pub.last_error
        async with EmbeddedBroker(port) as broker, Listener(broker.port, "ho/#") as lis:
            await lis.wait(lambda: len(lis.values(POWER)) == 10, timeout=30)
            assert lis.values(POWER) == [float(i) for i in range(5, 15)]   # in order, oldest lost
            assert len(pub.queue) == 0 and pub.connected
    finally:
        await pub.stop()
