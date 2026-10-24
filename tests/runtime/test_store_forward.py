"""Store & forward: the disk queue, samples across a database outage and a restart, and the
northbound MQTT queue across a broker outage."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.northbound import NorthboundPublisher
from hub.config_loader import load_config
from hub.models import Broker, NorthboundTarget, Quality, Sample
from hub.runtime import Hub, decode_samples, encode_samples
from hub.spool import DiskQueue
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from simulators.mqtt_sim import EmbeddedBroker, free_port
from tests.runtime.test_hub import FlakyStore, wait_until, write_config

pytestmark = pytest.mark.integration


# --- the disk queue ----------------------------------------------------------------------------


async def test_disk_queue_is_fifo_persistent_and_bounded(tmp_path: Path) -> None:
    q = DiskQueue(tmp_path / "q.db", max_bytes=1000)
    assert await q.put([b"a" * 100, b"b" * 100, b"c" * 100]) == 0
    first = await q.peek(2)
    assert [item for _, item in first] == [b"a" * 100, b"b" * 100]
    await q.ack(first[0][0])                                # only "a" was delivered
    assert q.count == 2
    q.close()
    q = DiskQueue(tmp_path / "q.db", max_bytes=1000)        # after a restart
    assert q.count == 2 and q.size_bytes == 200
    assert [item for _, item in await q.peek(5)] == [b"b" * 100, b"c" * 100]
    dropped = await q.put([bytes([65 + i]) * 300 for i in range(3)])   # 1100 bytes > 1000
    assert dropped == 1 and q.dropped == 1 and q.size_bytes == 1000    # the oldest went
    q.close()


def test_sample_encoding_keeps_types() -> None:
    t = datetime(2026, 10, 24, 8, 0, 0, 123000, tzinfo=UTC)
    samples = [Sample(tag="a.b.c.d", value=v, timestamp=t, quality=q)
               for v, q in ((True, Quality.GOOD), (7, Quality.GOOD), (2.5, Quality.UNCERTAIN),
                            ("text", Quality.GOOD), (None, Quality.BAD))]
    assert decode_samples(encode_samples(samples)) == samples


# --- samples ---------------------------------------------------------------------------------


@pytest.fixture
async def sim() -> Any:
    async with ModbusSimServer() as server:
        yield server


async def _changing(simdev: SimulatedModbusDevice, stop: asyncio.Event) -> None:
    i = 0
    while not stop.is_set():                     # changing values pass the history filter
        simdev.set("voltage_l1", 220.0 + i)
        i += 1
        await asyncio.sleep(0.03)


async def test_samples_wait_on_disk_through_an_outage_and_a_restart(
        sim: ModbusSimServer, tmp_path: Path) -> None:
    report = load_config(write_config(tmp_path, sim.port), env={})
    simdev = SimulatedModbusDevice(sim, report.devices[0].device)
    spool_file = tmp_path / "spool" / "samples.db"
    down = FlakyStore(failures=10**6)                       # the database stays down
    hub = Hub(report, store=down, flush_interval_s=0.1, max_buffer=50, batch_size=10,
              spool=DiskQueue(spool_file, 2**20))
    stop = asyncio.Event()
    changer = asyncio.create_task(_changing(simdev, stop))
    await hub.start()
    try:
        await wait_until(lambda: hub.stats.spool_batches >= 5, timeout=20)
    finally:
        stop.set()
        await changer
        hub.store = None                                    # stop() must not flush into it
        await hub.stop()
    assert hub.stats.samples_dropped == 0 and down.written == []
    assert hub.spool is not None
    waiting = hub.spool.count
    assert waiting >= 5
    spooled = {(s.tag, s.timestamp) for _, blob in await hub.spool.peek(waiting)
               for s in decode_samples(blob)}
    hub.spool.close()

    # the hub restarts (database still down at first), then the database comes back
    up = FlakyStore(failures=3)
    restarted = Hub(report, store=up, flush_interval_s=0.1, batch_size=10,
                    spool=DiskQueue(spool_file, 2**20))
    async with restarted:
        assert restarted.stats.spool_batches == waiting       # the backlog was found
        await wait_until(lambda: restarted.spool is not None and restarted.spool.count == 0,
                         timeout=20)
    written = {(s.tag, s.timestamp) for s in up.written}
    assert spooled <= written                                # nothing from the outage lost
    assert restarted.stats.spool_batches == 0
    restarted.spool.close()                                  # type: ignore[union-attr]


async def test_without_a_spool_the_buffer_still_drops(sim: ModbusSimServer,
                                                      tmp_path: Path) -> None:
    report = load_config(write_config(tmp_path, sim.port), env={})
    simdev = SimulatedModbusDevice(sim, report.devices[0].device)
    hub = Hub(report, store=FlakyStore(failures=10**6), flush_interval_s=0.1, max_buffer=10)
    stop = asyncio.Event()
    changer = asyncio.create_task(_changing(simdev, stop))
    await hub.start()
    try:
        await wait_until(lambda: hub.stats.samples_dropped > 0, timeout=20)
    finally:
        stop.set()
        await changer
        hub.store = None
        await hub.stop()


# --- northbound --------------------------------------------------------------------------------


def _services(tmp_path: Path) -> Any:
    from hub.runtime import LiveBus

    return SimpleNamespace(hub=SimpleNamespace(bus=LiveBus(), latest={}, tags={}),
                           settings=SimpleNamespace(node_id="node_a",
                                                    spool_dir=tmp_path / "spool"))


def _sample(i: int) -> dict[str, Any]:
    return {"tag": "kish.gate.meter_02.voltage_l1", "value": float(i),
            "ts": datetime(2026, 10, 24, 8, 0, i % 60, tzinfo=UTC).isoformat(),
            "quality": "good"}


async def test_northbound_keeps_order_through_a_broker_outage(tmp_path: Path) -> None:
    import aiomqtt

    port = free_port()
    target = NorthboundTarget(name="central", broker_ref="central", topic_prefix="site",
                              queue_max=20, heartbeat_s=3600)
    pub = NorthboundPublisher(_services(tmp_path), target,
                              Broker(broker_id="central", host="127.0.0.1", port=port))
    assert pub.spool is not None
    await pub.start()
    try:
        for i in range(60):                     # broker down: 60 changes, memory holds 20
            pub.offer(_sample(i))
            await asyncio.sleep(0)
        await wait_until(lambda: pub.spool.count >= 40, timeout=10)  # type: ignore[union-attr]
        assert pub.stats["dropped"] == 0
        async with EmbeddedBroker(port) as _:
            got: list[float] = []
            async with aiomqtt.Client("127.0.0.1", port) as client:
                await client.subscribe("site/kish/gate/meter_02/voltage_l1", qos=1)

                async def collect() -> None:
                    async for message in client.messages:
                        got.append(json.loads(message.payload)["v"])  # type: ignore[arg-type]
                        if len(got) == 60:
                            return

                await asyncio.wait_for(collect(), 20)
        assert got == [float(i) for i in range(60)]             # all of them, in order
        assert pub.spool.count == 0                              # type: ignore[union-attr]
    finally:
        await pub.stop()
        pub.spool.close()                                        # type: ignore[union-attr]
