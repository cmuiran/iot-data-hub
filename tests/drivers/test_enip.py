"""EtherNet/IP (Logix) driver against the controller simulator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError

from hub.codec import to_raw
from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.enip import EnipDriver, parse_path, tag_path
from hub.models import EnipDevice, Quality
from simulators.enip_sim import EnipSimServer, SimulatedEnipDevice
from simulators.mqtt_sim import free_port
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def plc(port: int, slot: int | None = 0, *extra: dict[str, Any], **conn: Any) -> EnipDevice:
    return device({
        "device_id": "plc_logix", "name": "Logix test", "site": "kish", "area": "water",
        "protocol": "enip", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "slot": slot, "timeout_ms": 1000,
                       "max_tags_per_request": 4} | conn,
        "tags": [
            {"name": "speed", "data_type": "float32", "tag": "Speed", "min": 0, "max": 1500},
            {"name": "level", "data_type": "float64", "tag": "Program:Main.Level"},
            {"name": "current", "data_type": "int32", "tag": "Motors[3].Current"},
            {"name": "running", "data_type": "bool", "tag": "Running"},
            {"name": "alarm_bit", "data_type": "bool", "tag": "AlarmWord", "bit": 4},
            {"name": "count", "data_type": "uint32", "tag": "Count"},
            {"name": "label", "data_type": "string", "tag": "Label"},
            {"name": "setpoint", "data_type": "float32", "tag": "Setpoint",
             "access": "read_write", "min": 0, "max": 100},
            {"name": "start", "data_type": "bool", "tag": "StartCmd", "access": "read_write"},
            {"name": "mode", "data_type": "int16", "tag": "Mode", "access": "read_write",
             "min": 0, "max": 3},
            *extra,
        ],
    })


def grant(dev: EnipDevice, name: str, value: Any) -> WriteGrant:
    tag = next(t for t in dev.tags if t.name == name)
    return WriteGrant("t", dev.tag_path(name), to_raw(tag, value))


@pytest.fixture(params=[0, None], ids=["routed", "direct"])
async def setup(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    async with EnipSimServer(free_port()) as srv:
        dev = plc(srv.port, request.param)
        sim = SimulatedEnipDevice(srv, dev)
        sim.set("speed", 1234.5)
        sim.set("level", 3.0625)
        sim.set("current", -17)
        sim.set("running", True)
        sim.set("alarm_bit", True)
        sim.set("count", 3_000_000_000)
        yield srv, sim, dev


def test_paths() -> None:
    for name in ("Speed", "Program:Main.Level", "Motors[3].Current", "Big[300]", "A[1,2].B"):
        assert parse_path(tag_path(name)) == name.replace(",", "][") if "," in name \
            else parse_path(tag_path(name)) == name
    assert len(tag_path("Abc")) % 2 == 0                     # odd names are padded
    with pytest.raises(ValidationError):
        plc(1, 0, {"name": "x", "data_type": "int32", "tag": "1bad"})
    with pytest.raises(ValidationError, match="read-only"):
        plc(1, 0, {"name": "x", "data_type": "bool", "tag": "W", "bit": 1,
                   "access": "read_write"})


async def test_reads_in_batches(setup: Any) -> None:
    srv, _, dev = setup
    rec = Recorder()
    driver = EnipDriver(dev, rec.on_samples, rec.on_status)
    await driver.start()
    try:
        await rec.wait_for(lambda: len(rec.latest()) == 10 and rec.state is DeviceState.ONLINE)
        v = {name: s.value for name, s in rec.latest().items()}
        assert v["speed"] == pytest.approx(1234.5) and v["level"] == 3.0625
        assert v["current"] == -17 and v["running"] is True and v["alarm_bit"] is True
        assert v["count"] == 3_000_000_000 and v["label"] == "LABEL"
        assert all(s.quality is Quality.GOOD for s in rec.latest().values())
        before = srv.requests
        await driver.poll_once()
        assert srv.requests - before == 3                    # 10 tags, 4 per request
    finally:
        await driver.stop()


async def test_writes_use_the_controller_type(setup: Any) -> None:
    srv, sim, dev = setup
    srv.tags["Mode"][0] = 0xC2                               # Mode is a SINT in the PLC
    driver = EnipDriver(dev, Recorder().on_samples, Recorder().on_status)
    await driver.start()
    try:
        await asyncio.sleep(0.3)
        await driver.write_tag("setpoint", grant(dev, "setpoint", 61.5))
        await driver.write_tag("start", grant(dev, "start", True))
        await driver.write_tag("mode", grant(dev, "mode", 2))
        assert sim.get("setpoint") == pytest.approx(61.5) and sim.get("start") is True
        assert srv.tags["Mode"] == [0xC2, 2]
        assert await driver.read_tag("setpoint") == pytest.approx(61.5)
    finally:
        await driver.stop()


async def test_unknown_tag_and_type_mismatch_mark_only_those_tags() -> None:
    async with EnipSimServer(free_port()) as srv:
        dev = plc(srv.port, 0,
                  {"name": "ghost", "data_type": "int32", "tag": "DoesNotExist"},
                  {"name": "wrong", "data_type": "float32", "tag": "Count2"})
        SimulatedEnipDevice(srv, dev)
        del srv.tags["DoesNotExist"]                            # not in the controller
        srv.tags["Count2"] = [0xC4, 5]                          # a DINT, configured as float32
        rec = Recorder()
        driver = EnipDriver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: {"ghost", "wrong", "speed"} <= rec.latest().keys()
                               and rec.state is DeviceState.ERROR)
            assert rec.latest()["ghost"].quality is Quality.BAD
            assert rec.latest()["wrong"].quality is Quality.BAD
            assert rec.latest()["speed"].quality is Quality.GOOD
            error = rec.statuses[-1].last_error or ""
            assert "unknown tag" in error and "DINT" in error
        finally:
            await driver.stop()


async def test_offline_then_reconnect() -> None:
    port = free_port()
    dev = plc(port)
    rec = Recorder()
    driver = EnipDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = 0.1   # noqa: SLF001
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        async with EnipSimServer(port) as srv:
            SimulatedEnipDevice(srv, dev).set("speed", 7.0)
            await rec.wait_for(lambda: rec.state is DeviceState.ONLINE, timeout=15)
            await rec.wait_for(lambda: rec.latest().get("speed") is not None
                               and rec.latest()["speed"].value == pytest.approx(7.0))
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE, timeout=15)
    finally:
        await driver.stop()
