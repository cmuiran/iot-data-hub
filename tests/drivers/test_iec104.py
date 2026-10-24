"""IEC 60870-5-104 driver against the outstation simulator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from hub.codec import to_raw
from hub.drivers.base import DeviceState, DriverError, WriteGrant
from hub.drivers.iec104 import Iec104Driver
from hub.drivers.iec104_proto import (
    IV,
    NT,
    Asdu,
    InfoObject,
    decode_asdu,
    decode_cp56,
    encode_asdu,
    encode_cp56,
)
from hub.models import Iec104Device, Quality
from simulators.iec104_sim import Iec104SimServer, SimulatedIec104Device
from simulators.mqtt_sim import free_port
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def rtu(port: int, *extra: dict[str, Any], **conn: Any) -> Iec104Device:
    return device({
        "device_id": "rtu_01", "name": "Substation RTU", "site": "kish", "area": "substation",
        "protocol": "iec104",
        "connection": {"host": "127.0.0.1", "port": port, "common_address": 7, "t1_s": 3,
                       "t2_s": 1, "t3_s": 5, "interrogation_interval_s": 0} | conn,
        "tags": [
            {"name": "breaker", "data_type": "bool", "ioa": 1001, "type": "double",
             "access": "read_write", "command_ioa": 2001, "select_before_operate": True},
            {"name": "alarm", "data_type": "bool", "ioa": 1002, "type": "single"},
            {"name": "power", "data_type": "float32", "ioa": 3001, "type": "float",
             "unit": "MW", "min": 0, "max": 50},
            {"name": "voltage", "data_type": "float32", "ioa": 3002, "type": "normalized",
             "scale": 40, "unit": "kV", "min": -40, "max": 40},
            {"name": "tap", "data_type": "int16", "ioa": 3003, "type": "step"},
            {"name": "current", "data_type": "int16", "ioa": 3004, "type": "scaled",
             "scale": 0.1},
            {"name": "energy", "data_type": "int64", "ioa": 4001, "type": "integrated"},
            {"name": "setpoint", "data_type": "float32", "ioa": 5001, "type": "float",
             "access": "read_write", "min": 0, "max": 100},
            {"name": "trip", "data_type": "bool", "ioa": 5002, "type": "single",
             "access": "read_write"},
            *extra,
        ],
    })


def grant(dev: Iec104Device, name: str, value: Any) -> WriteGrant:
    tag = next(t for t in dev.tags if t.name == name)
    return WriteGrant("t", dev.tag_path(name), to_raw(tag, value))


@pytest.fixture
async def rig() -> AsyncIterator[tuple[Iec104SimServer, SimulatedIec104Device, Iec104Device,
                                       Recorder, Iec104Driver]]:
    async with Iec104SimServer(free_port()) as srv:
        dev = rtu(srv.port)
        sim = SimulatedIec104Device(srv, dev)
        sim.set("breaker", True)
        sim.set("alarm", False)
        sim.set("power", 12.5)
        sim.set("voltage", 20.0)                   # 20 kV = normalized 0.5 x scale 40
        sim.set("tap", -3)
        sim.set("current", 123.4)
        sim.set("energy", 2_000_000_000)             # integrated totals are 32-bit
        sim.set("setpoint", 10.0)
        rec = Recorder()
        driver = Iec104Driver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            yield srv, sim, dev, rec, driver
        finally:
            await driver.stop()


def test_codec_round_trip() -> None:
    t = datetime(2026, 10, 24, 8, 30, 15, 250000, tzinfo=UTC)
    assert decode_cp56(encode_cp56(t)) == t
    asdu = Asdu(36, 3, 7, [InfoObject(3001, 1.5, NT, t), InfoObject(3002, -2.0, 0, t)])
    back = decode_asdu(encode_asdu(asdu))
    assert (back.type_id, back.cot, back.ca) == (36, 3, 7)
    assert [(o.ioa, o.value, o.quality, o.time) for o in back.objects] == [
        (3001, 1.5, NT, t), (3002, -2.0, 0, t)]
    seq = Asdu(1, 20, 7, [InfoObject(10, True), InfoObject(11, False)], sq=True)
    assert [o.ioa for o in decode_asdu(encode_asdu(seq)).objects] == [10, 11]
    with pytest.raises(ValidationError, match="cannot be commanded"):
        rtu(1, {"name": "x", "data_type": "int64", "ioa": 9, "type": "integrated",
                "access": "read_write", "min": 0, "max": 10})


async def test_interrogation_delivers_every_type(rig: Any) -> None:
    _, _, _, rec, _ = rig
    await rec.wait_for(lambda: len(rec.latest()) == 9 and rec.state is DeviceState.ONLINE)
    v = {name: s.value for name, s in rec.latest().items()}
    assert v["breaker"] is True and v["alarm"] is False and v["tap"] == -3
    assert v["power"] == pytest.approx(12.5) and v["voltage"] == pytest.approx(20.0, abs=0.01)
    assert v["current"] == pytest.approx(123.4) and v["energy"] == 2_000_000_000
    assert all(s.quality is Quality.GOOD for s in rec.latest().values())


async def test_spontaneous_changes_and_quality(rig: Any) -> None:
    _, sim, _, rec, _ = rig
    await rec.wait_for(lambda: "power" in rec.latest())
    await sim.change("power", 33.0)
    await rec.wait_for(lambda: rec.latest()["power"].value == pytest.approx(33.0))
    stamp = rec.latest()["power"].timestamp
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 5   # the device's time tag
    await sim.change("alarm", True, quality=NT)
    await rec.wait_for(lambda: rec.latest()["alarm"].quality is Quality.UNCERTAIN)
    await sim.change("power", 1.0, quality=IV)
    await rec.wait_for(lambda: rec.latest()["power"].quality is Quality.BAD)
    assert rec.latest()["power"].value is None


async def test_commands(rig: Any) -> None:
    srv, sim, dev, rec, driver = rig
    await rec.wait_for(lambda: rec.state is DeviceState.ONLINE and "breaker" in rec.latest())
    # select-before-operate on a separate command IOA; the RTU reports the new position
    await driver.write_tag("breaker", grant(dev, "breaker", False))
    assert srv.commands[-2:] == [(2001, 1, True), (2001, 1, False)]
    assert await driver.read_back("breaker", 3) is False
    assert sim.get("breaker") == 1                                # DPI 1 = OFF
    await driver.write_tag("setpoint", grant(dev, "setpoint", 42.5))
    assert await driver.read_back("setpoint", 3) == pytest.approx(42.5)
    await driver.write_tag("trip", grant(dev, "trip", True))
    assert srv.commands[-1] == (5002, True, False)
    srv.refuse_commands.add(5001)
    with pytest.raises(DriverError, match="negative"):
        await driver.write_tag("setpoint", grant(dev, "setpoint", 1.0))


async def test_link_acknowledges_and_recovers(rig: Any) -> None:
    srv, sim, _, rec, _ = rig
    await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
    for i in range(30):                         # more than k frames: the hub must acknowledge
        await sim.change("power", float(i))
    await rec.wait_for(lambda: rec.latest()["power"].value == pytest.approx(29.0))
    ep = next(iter(srv._endpoints))             # noqa: SLF001 - check the link state
    assert (ep._vs - ep._ack) % 32768 < ep.k    # noqa: SLF001
    for e in list(srv._endpoints):              # noqa: SLF001 - the RTU drops the link
        await e.close()
    await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE, timeout=10)
    await rec.wait_for(lambda: rec.state is DeviceState.ONLINE, timeout=20)


async def test_unknown_common_address_is_offline_free_but_logged() -> None:
    async with Iec104SimServer(free_port()) as srv:
        dev = rtu(srv.port)
        SimulatedIec104Device(srv, dev)
        srv.ca = 99                             # the RTU is configured for another address
        rec = Recorder()
        driver = Iec104Driver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
            await asyncio.sleep(0.5)
            assert rec.latest() == {}           # nothing was interrogated
        finally:
            await driver.stop()
