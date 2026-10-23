"""S7 driver against the S7 simulator (python-snap7 server with real bit writes)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from hub.codec import to_raw
from hub.drivers.base import DeviceState, WriteGrant, WriteNotPermitted
from hub.drivers.s7 import S7Driver, decode_s7, encode_s7, plan_s7_blocks
from hub.models import DataType, Quality, S7Area, S7Device
from simulators.mqtt_sim import free_port
from simulators.s7_sim import S7SimServer, SimulatedS7Device
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def plc(port: int, *extra: dict[str, Any], **conn: Any) -> S7Device:
    return device({
        "device_id": "plc_s7", "name": "S7 test", "site": "kish", "area": "water",
        "protocol": "s7", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "timeout_ms": 1000} | conn,
        "tags": [
            {"name": "flow", "data_type": "float32", "address": "DB1.DBD0", "min": 0,
             "max": 200},
            {"name": "level", "data_type": "int16", "address": "DB1.DBW4", "scale": 0.1},
            {"name": "running", "data_type": "bool", "address": "DB1.DBX6.0"},
            {"name": "pump_cmd", "data_type": "bool", "address": "DB1.DBX6.3",
             "access": "read_write"},
            {"name": "mode", "data_type": "uint16", "address": "DB1.DBB7",
             "states": {0: "off", 1: "auto"}},
            {"name": "setpoint", "data_type": "float32", "address": "DB2.DBD0",
             "access": "read_write", "min": 0, "max": 50},
            {"name": "hours", "data_type": "uint32", "address": "MD20"},
            {"name": "label", "data_type": "string", "address": "DB3.DBB0", "string_length": 8},
            {"name": "total", "data_type": "float64", "address": "DB3.DBB20"},
            {"name": "di", "data_type": "bool", "address": "I0.1"},
            {"name": "aw", "data_type": "int16", "address": "QW2", "access": "read_write",
             "min": -1000, "max": 1000},
            *extra,
        ],
    })


@pytest.fixture
def port() -> int:
    return free_port()


@pytest.fixture
def server(port: int) -> Iterator[tuple[S7SimServer, SimulatedS7Device]]:
    srv = S7SimServer(port)
    sim = SimulatedS7Device(srv, plc(port))
    sim.set("flow", 42.5)
    sim.set("level", 63.4)
    sim.set("running", True)
    sim.set("mode", 1)
    sim.set("hours", 123456)
    sim.set("label", "PUMP-A")
    sim.set("total", 1.25e9)
    sim.set("di", True)
    srv.start()
    try:
        yield srv, sim
    finally:
        srv.stop()


def grant(dev: S7Device, name: str, value: Any) -> WriteGrant:
    tag = next(t for t in dev.tags if t.name == name)
    return WriteGrant("t", dev.tag_path(name), to_raw(tag, value))


def test_codec_round_trip() -> None:
    dev = plc(102)
    for t in dev.tags:
        value: Any = {DataType.BOOL: True, DataType.STRING: "ABC"}.get(t.data_type, 7)
        if t.data_type is DataType.BOOL:
            continue
        assert decode_s7(t, encode_s7(t, value)) == value


def test_blocks_group_neighbours() -> None:
    dev = plc(102)
    blocks = plan_s7_blocks(list(dev.tags), max_bytes=200, max_gap=16)
    by_area = {(b.area, b.db): b for b in blocks}
    db1 = by_area[(S7Area.DB, 1)]
    assert (db1.start, db1.size, len(db1.tags)) == (0, 8, 5)       # DBD0 .. DBB7 in one read
    assert by_area[(S7Area.DB, 3)].size == 28                      # string 0..9, LREAL 20..27
    small = plan_s7_blocks(list(dev.tags), max_bytes=4, max_gap=0)
    # a tag larger than the limit (the 10-byte STRING) is still read in one piece
    assert max(b.size for b in small) == 10 and len(small) > len(blocks)


async def test_reads_every_type(server: tuple[S7SimServer, SimulatedS7Device],
                                port: int) -> None:
    rec = Recorder()
    driver = S7Driver(plc(port), rec.on_samples, rec.on_status)
    await driver.start()
    try:
        await rec.wait_for(lambda: len(rec.latest()) == 11 and rec.state is DeviceState.ONLINE)
        v = {name: s.value for name, s in rec.latest().items()}
        assert v["flow"] == pytest.approx(42.5) and v["level"] == pytest.approx(63.4)
        assert v["running"] is True and v["pump_cmd"] is False and v["mode"] == 1
        assert v["hours"] == 123456 and v["label"] == "PUMP-A" and v["total"] == 1.25e9
        assert v["di"] is True
        assert all(s.quality is Quality.GOOD for s in rec.latest().values())
    finally:
        await driver.stop()


async def test_writes_numbers_and_single_bits(server: tuple[S7SimServer, SimulatedS7Device],
                                              port: int) -> None:
    srv, sim = server
    dev = plc(port)
    driver = S7Driver(dev, Recorder().on_samples, Recorder().on_status)
    await driver.start()
    try:
        await asyncio.sleep(0.3)
        await driver.write_tag("setpoint", grant(dev, "setpoint", 12.5))
        await driver.write_tag("aw", grant(dev, "aw", -321))
        assert await driver.read_tag("setpoint") == pytest.approx(12.5)
        assert await driver.read_tag("aw") == -321
        # the bit write must not touch DBX6.0 (running), which shares the byte
        await driver.write_tag("pump_cmd", grant(dev, "pump_cmd", True))
        assert srv.memory[(S7Area.DB, 1)][6] == 0b1001
        await driver.write_tag("pump_cmd", grant(dev, "pump_cmd", False))
        assert srv.memory[(S7Area.DB, 1)][6] == 0b0001
        assert sim.get_raw("running") is True
        with pytest.raises(WriteNotPermitted):
            await driver.write_tag("flow", grant(dev, "flow", 1.0))
    finally:
        await driver.stop()


async def test_unknown_area_marks_only_its_tags(port: int) -> None:
    srv = S7SimServer(port)
    good = plc(port)
    SimulatedS7Device(srv, good)
    srv.start()
    try:
        dev = plc(port, {"name": "ghost", "data_type": "int16", "address": "DB9.DBW0"},
                  {"name": "ghost2", "data_type": "int16", "address": "DB9.DBW2"})
        rec = Recorder()
        driver = S7Driver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: {"ghost", "flow"} <= rec.latest().keys()
                               and rec.state is DeviceState.ERROR)
            assert rec.latest()["ghost"].quality is Quality.BAD
            assert rec.latest()["flow"].quality is Quality.GOOD
            assert "DB9" in (rec.statuses[-1].last_error or "")
        finally:
            await driver.stop()
    finally:
        srv.stop()


async def test_offline_then_reconnect(port: int) -> None:
    dev = plc(port)
    rec = Recorder()
    driver = S7Driver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = 0.1   # noqa: SLF001 - keep the test short
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        srv = S7SimServer(port)
        SimulatedS7Device(srv, dev).set("flow", 9.0)
        srv.start()
        try:
            await rec.wait_for(lambda: rec.state is DeviceState.ONLINE, timeout=15)
            await rec.wait_for(lambda: rec.latest().get("flow") is not None
                               and rec.latest()["flow"].value == pytest.approx(9.0))
        finally:
            srv.stop()
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE, timeout=15)
        assert rec.latest()["flow"].quality is Quality.BAD
    finally:
        await driver.stop()
