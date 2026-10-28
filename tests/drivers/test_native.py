"""Native driver (platform device contract) without a hub: values by key, types, device time,
going offline when silent or when the device says so, commands through a channel, acks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from hub.drivers.base import DeviceState, DriverError, WriteGrant
from hub.drivers.native import NativeDriver
from hub.models import DEVICE_ADAPTER, NativeDevice, Quality

DEVICE = {"device_id": "dev_1", "name": "N", "site": "kish", "area": "a", "protocol": "native",
          "connection": {"stale_after_s": 0.5},
          "tags": [{"name": "relay", "data_type": "bool", "access": "read_write"},
                   {"name": "power", "data_type": "float32", "unit": "kW"}]}


class Rec:
    def __init__(self) -> None:
        self.samples: list[Any] = []
        self.state = DeviceState.UNKNOWN

    async def on_samples(self, samples: list[Any]) -> None:
        self.samples += samples

    async def on_status(self, status: Any) -> None:
        self.state = status.state


class Channel:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, device: NativeDevice, command: dict[str, Any]) -> str:
        self.sent.append(command)
        return "test"


def driver(rec: Rec, channel: Channel | None = None) -> NativeDriver:
    device = DEVICE_ADAPTER.validate_python(DEVICE)
    assert isinstance(device, NativeDevice)
    return NativeDriver(device, rec.on_samples, rec.on_status, channel=channel)


async def test_values_types_and_time() -> None:
    rec = Rec()
    d = driver(rec)
    n = await d.receive({"ts": "2026-10-27T10:00:00Z", "values": {"relay": 1, "power": 2.5,
                                                                  "other": 3}})
    assert n == 2 and rec.state is DeviceState.ONLINE
    by_tag = {s.tag.rsplit(".", 1)[1]: s for s in rec.samples}
    assert by_tag["relay"].value is True
    assert by_tag["power"].timestamp == datetime(2026, 10, 27, 10, tzinfo=UTC)
    assert d.counters["unknown_keys"] == 1
    await d.receive({"ts": "yesterday", "values": {"power": "x"}})
    assert rec.samples[-1].quality is Quality.BAD
    await d.receive({"ts": "yesterday", "values": {"power": 1}})
    assert rec.samples[-1].quality is Quality.UNCERTAIN          # unreadable device time
    for bad in ({"power": 1}, [], {"values": [1]}):
        with pytest.raises(ValueError):
            await d.receive(bad)


async def test_offline_when_silent_or_told() -> None:
    rec = Rec()
    d = driver(rec)
    await d.start()
    try:
        await d.receive({"values": {"power": 1}})
        await asyncio.sleep(2.2)                     # stale_after_s 0.5, checked every second
        assert rec.state is DeviceState.OFFLINE
        assert rec.samples[-1].quality is Quality.BAD
        await d.receive({"values": {"power": 1}})
        assert rec.state is DeviceState.ONLINE
        await d.state({"state": "offline"})
        assert rec.state is DeviceState.OFFLINE
    finally:
        await d.stop()


async def test_commands_and_acks() -> None:
    rec, channel = Rec(), Channel()
    d = driver(rec, channel)
    grant = WriteGrant(request_id="c1", tag_path="kish.a.dev_1.relay", raw_value=True)
    waiting = asyncio.create_task(d.read_back("relay", 2))
    await asyncio.sleep(0)
    await d.write_tag("relay", grant)
    assert channel.sent == [{"command_id": "c1", "tag": "relay", "value": True}]
    d.ack({"command_id": "c1", "status": "done", "value": True})
    assert await waiting is True
    waiting = asyncio.create_task(d.read_back("relay", 2))
    await asyncio.sleep(0)
    await d.write_tag("relay", WriteGrant(request_id="c2", tag_path="kish.a.dev_1.relay",
                                          raw_value=False))
    d.ack({"command_id": "c2", "status": "failed", "error": "jammed"})
    with pytest.raises(DriverError, match="jammed"):
        await waiting
    d.ack({"command_id": "unknown", "status": "failed"})            # ignored
    with pytest.raises(DriverError, match="no device transport"):
        await driver(Rec()).write_tag("relay", grant)


async def test_contact_without_values_keeps_it_online() -> None:
    rec = Rec()
    d = driver(rec)
    await d.start()
    try:
        await d.receive({"values": {"power": 1}})
        for _ in range(10):                         # 2 s, stale_after_s 0.5: only contact
            await asyncio.sleep(0.2)
            await d.touch()
        assert rec.state is DeviceState.ONLINE
        await asyncio.sleep(2.2)                     # silence: offline
        assert rec.state is DeviceState.OFFLINE
        await d.touch()
        assert rec.state is DeviceState.ONLINE
    finally:
        await d.stop()
