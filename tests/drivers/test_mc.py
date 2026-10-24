"""MELSEC MC protocol (3E binary) driver against the MC simulator, TCP and UDP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError

from hub.codec import to_raw
from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.mc import McDriver, pack_bits, unpack_bits
from hub.models import McDevice, Quality, parse_mc_address
from simulators.mc_sim import McSimServer, SimulatedMcDevice
from simulators.mqtt_sim import free_port
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def plc(port: int, transport: str = "tcp", *extra: dict[str, Any]) -> McDevice:
    return device({
        "device_id": "plc_melsec", "name": "MELSEC test", "site": "kish", "area": "water",
        "protocol": "mc", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "transport": transport,
                       "timeout_ms": 1000},
        "tags": [
            {"name": "flow", "data_type": "float32", "address": "D100", "min": 0, "max": 200},
            {"name": "total", "data_type": "uint32", "address": "D102"},
            {"name": "status_bit", "data_type": "bool", "address": "D104.3"},
            {"name": "wvalue", "data_type": "int16", "address": "W1A"},
            {"name": "running", "data_type": "bool", "address": "M10"},
            {"name": "input", "data_type": "bool", "address": "X1F"},
            {"name": "start_cmd", "data_type": "bool", "address": "Y20",
             "access": "read_write"},
            {"name": "setpoint", "data_type": "float32", "address": "D200",
             "access": "read_write", "min": 0, "max": 100},
            {"name": "energy", "data_type": "float64", "address": "R1000"},
            *extra,
        ],
    })


def grant(dev: McDevice, name: str, value: Any) -> WriteGrant:
    tag = next(t for t in dev.tags if t.name == name)
    return WriteGrant("t", dev.tag_path(name), to_raw(tag, value))


@pytest.fixture(params=["tcp", "udp"])
async def setup(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    async with McSimServer(free_port(), transport=request.param) as srv:
        dev = plc(srv.port, request.param)
        sim = SimulatedMcDevice(srv, dev)
        sim.set("flow", 55.5)
        sim.set("total", 4_000_000)
        sim.set("status_bit", True)
        sim.set("wvalue", -7)
        sim.set("running", True)
        sim.set("input", True)
        sim.set("energy", 12345.678)
        yield srv, sim, dev


def test_addresses_and_bit_packing() -> None:
    assert parse_mc_address("W1A").word == 0x1A and parse_mc_address("X1F").word == 31
    assert parse_mc_address("M10").word == 10                 # M is decimal
    with pytest.raises(ValueError):
        parse_mc_address("MA")                                # hex digits on a decimal device
    with pytest.raises(ValidationError, match="read-modify-write"):
        plc(5000, "tcp", {"name": "b", "data_type": "bool", "address": "D5.1",
                          "access": "read_write"})
    assert unpack_bits(pack_bits([1, 0, 1]), 3) == [1, 0, 1]
    assert pack_bits([1, 1, 0, 1]) == bytes([0x11, 0x01])


async def test_reads(setup: Any) -> None:
    _, _, dev = setup
    rec = Recorder()
    driver = McDriver(dev, rec.on_samples, rec.on_status)
    await driver.start()
    try:
        await rec.wait_for(lambda: len(rec.latest()) == 9 and rec.state is DeviceState.ONLINE)
        v = {name: s.value for name, s in rec.latest().items()}
        assert v["flow"] == pytest.approx(55.5) and v["total"] == 4_000_000
        assert v["status_bit"] is True and v["wvalue"] == -7
        assert v["running"] is True and v["input"] is True and v["start_cmd"] is False
        assert v["energy"] == pytest.approx(12345.678)
        assert all(s.quality is Quality.GOOD for s in rec.latest().values())
    finally:
        await driver.stop()


async def test_writes(setup: Any) -> None:
    srv, _, dev = setup
    driver = McDriver(dev, Recorder().on_samples, Recorder().on_status)
    await driver.start()
    try:
        await asyncio.sleep(0.3)
        await driver.write_tag("setpoint", grant(dev, "setpoint", 42.0))
        assert await driver.read_tag("setpoint") == pytest.approx(42.0)
        assert srv.memory.read("D", 200, 2) == [0x0000, 0x4228]      # lowest word first
        await driver.write_tag("start_cmd", grant(dev, "start_cmd", True))
        assert srv.memory.read("Y", 0x20, 1) == [1]
        assert await driver.read_tag("start_cmd") is True
    finally:
        await driver.stop()


async def test_bad_device_marks_only_its_tags() -> None:
    async with McSimServer(free_port()) as srv:
        dev = plc(srv.port)
        SimulatedMcDevice(srv, dev)
        srv.unreadable.add("R")
        rec = Recorder()
        driver = McDriver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: {"energy", "flow"} <= rec.latest().keys()
                               and rec.state is DeviceState.ERROR)
            assert rec.latest()["energy"].quality is Quality.BAD
            assert rec.latest()["flow"].quality is Quality.GOOD
            assert "C056" in (rec.statuses[-1].last_error or "")
        finally:
            await driver.stop()
