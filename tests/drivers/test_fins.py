"""Omron FINS driver against the FINS simulator (TCP with node handshake, and UDP)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from hub.codec import to_raw
from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.fins import FinsDriver
from hub.drivers.word_plc import plan_spans
from hub.models import FinsDevice, Quality, parse_fins_address
from simulators.fins_sim import FinsSimServer, SimulatedFinsDevice
from simulators.mqtt_sim import free_port
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def plc(port: int, transport: str = "tcp", *extra: dict[str, Any]) -> FinsDevice:
    conn: dict[str, Any] = {"host": "127.0.0.1", "port": port, "transport": transport,
                            "timeout_ms": 1000}
    if transport == "udp":
        conn |= {"dest_node": 1, "src_node": 50}
    return device({
        "device_id": "plc_omron", "name": "Omron test", "site": "kish", "area": "water",
        "protocol": "fins", "poll_interval_ms": 100, "connection": conn,
        "tags": [
            {"name": "level", "data_type": "float32", "address": "D100", "min": 0, "max": 10},
            {"name": "count", "data_type": "uint32", "address": "D102"},
            {"name": "temp", "data_type": "int16", "address": "D110", "scale": 0.1},
            {"name": "running", "data_type": "bool", "address": "CIO0.00"},
            {"name": "fault", "data_type": "bool", "address": "CIO0.07"},
            {"name": "pump_cmd", "data_type": "bool", "address": "CIO0.03",
             "access": "read_write"},
            {"name": "setpoint", "data_type": "float32", "address": "D200",
             "access": "read_write", "min": 0, "max": 50},
            {"name": "hours", "data_type": "uint64", "address": "H10"},
            *extra,
        ],
    })


def grant(dev: FinsDevice, name: str, value: Any) -> WriteGrant:
    tag = next(t for t in dev.tags if t.name == name)
    return WriteGrant("t", dev.tag_path(name), to_raw(tag, value))


@pytest.fixture(params=["tcp", "udp"])
async def setup(request: pytest.FixtureRequest) -> AsyncIterator[tuple[FinsSimServer,
                                                                       SimulatedFinsDevice,
                                                                       FinsDevice]]:
    transport = request.param
    async with FinsSimServer(free_port(), transport=transport) as srv:
        dev = plc(srv.port, transport)
        sim = SimulatedFinsDevice(srv, dev)
        sim.set("level", 4.25)
        sim.set("count", 70000)
        sim.set("temp", -12.3)
        sim.set("running", True)
        sim.set("fault", True)
        sim.set("hours", 123456789012)
        yield srv, sim, dev


def test_addresses_and_spans() -> None:
    assert parse_fins_address("DM100").area == "D"
    assert parse_fins_address("cio10.05").bit == 5
    with pytest.raises(ValueError):
        parse_fins_address("X10")
    dev = plc(9600)
    spans = plan_spans(((t.parsed.area, t.parsed.word, t.word_count, t) for t in dev.tags),
                       500, 16)
    by_area = {(s.area, s.start): s for s in spans}
    assert by_area[("D", 100)].count == 11            # D100..D110 in one read
    assert ("D", 200) in by_area                      # D200 too far: its own read
    assert len(by_area[("CIO", 0)].tags) == 3         # three bits of one word: one read


async def test_reads(setup: Any) -> None:
    _, _, dev = setup
    rec = Recorder()
    driver = FinsDriver(dev, rec.on_samples, rec.on_status)
    await driver.start()
    try:
        await rec.wait_for(lambda: len(rec.latest()) == 8 and rec.state is DeviceState.ONLINE)
        v = {name: s.value for name, s in rec.latest().items()}
        assert v["level"] == pytest.approx(4.25) and v["count"] == 70000
        assert v["temp"] == pytest.approx(-12.3)
        assert v["running"] is True and v["fault"] is True and v["pump_cmd"] is False
        assert v["hours"] == 123456789012
        assert all(s.quality is Quality.GOOD for s in rec.latest().values())
    finally:
        await driver.stop()


async def test_writes_words_and_single_bits(setup: Any) -> None:
    srv, _, dev = setup
    driver = FinsDriver(dev, Recorder().on_samples, Recorder().on_status)
    await driver.start()
    try:
        await asyncio.sleep(0.3)
        await driver.write_tag("setpoint", grant(dev, "setpoint", 12.5))
        assert await driver.read_tag("setpoint") == pytest.approx(12.5)
        # float32 12.5 = 0x41480000, lowest word first (Omron)
        assert srv.memory.read("D", 200, 2) == [0x0000, 0x4148]
        await driver.write_tag("pump_cmd", grant(dev, "pump_cmd", True))
        assert srv.memory.read("CIO", 0, 1) == [0b1000_1001]       # bits 0 and 7 untouched
        await driver.write_tag("pump_cmd", grant(dev, "pump_cmd", False))
        assert srv.memory.read("CIO", 0, 1) == [0b1000_0001]
    finally:
        await driver.stop()


async def test_unreadable_area_marks_only_its_tags() -> None:
    async with FinsSimServer(free_port()) as srv:
        dev = plc(srv.port)
        SimulatedFinsDevice(srv, dev)
        srv.unreadable.add("H")
        rec = Recorder()
        driver = FinsDriver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: {"hours", "level"} <= rec.latest().keys()
                               and rec.state is DeviceState.ERROR)
            assert rec.latest()["hours"].quality is Quality.BAD
            assert rec.latest()["level"].quality is Quality.GOOD
            assert "1103" in (rec.statuses[-1].last_error or "")
        finally:
            await driver.stop()


async def test_protected_area_refuses_writes() -> None:
    async with FinsSimServer(free_port()) as srv:
        dev = plc(srv.port)
        SimulatedFinsDevice(srv, dev)
        srv.protected.add("D")
        driver = FinsDriver(dev, Recorder().on_samples, Recorder().on_status)
        await driver.start()
        try:
            await asyncio.sleep(0.3)
            with pytest.raises(Exception, match="2002"):
                await driver.write_tag("setpoint", grant(dev, "setpoint", 1.0))
        finally:
            await driver.stop()


async def test_offline_then_reconnect() -> None:
    port = free_port()
    dev = plc(port)
    rec = Recorder()
    driver = FinsDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = 0.1   # noqa: SLF001 - keep the test short
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        async with FinsSimServer(port) as srv:
            SimulatedFinsDevice(srv, dev).set("level", 9.0)
            await rec.wait_for(lambda: rec.state is DeviceState.ONLINE, timeout=15)
            await rec.wait_for(lambda: rec.latest().get("level") is not None
                               and rec.latest()["level"].value == pytest.approx(9.0))
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE, timeout=15)
        assert rec.latest()["level"].quality is Quality.BAD
    finally:
        await driver.stop()
