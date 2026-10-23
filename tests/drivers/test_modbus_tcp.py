from __future__ import annotations

import math
from typing import Any

import pytest

from hub.drivers.base import DeviceState, WriteGrant, WriteNotPermitted
from hub.drivers.modbus_tcp import ModbusTcpDriver, plan_blocks
from hub.models import ModbusDevice, ModbusTag, Quality
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def meter(port: int, unit_id: int = 1, device_id: str = "meter_02", **conn: Any) -> ModbusDevice:
    return device({
        "device_id": device_id, "name": "Gate meter", "site": "kish", "area": "gate",
        "protocol": "modbus_tcp", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "unit_id": unit_id,
                       "timeout_ms": 300, "retries": 0, "word_order": "little"} | conn,
        "tags": [
            {"name": "voltage_l1", "unit": "V", "data_type": "float32",
             "register_type": "input", "address": 0, "min": 200, "max": 260},
            {"name": "current_l1", "unit": "A", "data_type": "float32",
             "register_type": "input", "address": 6},
            {"name": "energy", "unit": "Wh", "data_type": "int64",
             "register_type": "input", "address": 3203},
            {"name": "status_word", "data_type": "uint16", "register_type": "holding",
             "address": 50},
            {"name": "alarm", "data_type": "bool", "register_type": "holding", "address": 50,
             "bit": 2},
            {"name": "demand_limit", "unit": "kW", "data_type": "uint16",
             "register_type": "holding", "address": 100, "scale": 0.1,
             "access": "read_write", "min": 0, "max": 500},
            {"name": "relay", "data_type": "bool", "register_type": "coil", "address": 0,
             "access": "read_write"},
            {"name": "model", "data_type": "string", "register_type": "holding",
             "address": 200, "count": 4},
        ],
    })


@pytest.fixture
async def sim() -> Any:
    async with ModbusSimServer() as server:
        yield server


def grant(dev: ModbusDevice, tag: str, raw: Any) -> WriteGrant:
    return WriteGrant(request_id="t", tag_path=dev.tag_path(tag), raw_value=raw)


# --- read plan ------------------------------------------------------------------------------


def _tag(name: str, address: int, data_type: str = "uint16", rtype: str = "holding") -> ModbusTag:
    return ModbusTag.model_validate({"name": name, "data_type": data_type,
                                     "register_type": rtype, "address": address})


def test_plan_merges_neighbours_and_respects_limit() -> None:
    tags = [_tag("a", 0, "float32"), _tag("b", 2), _tag("c", 9), _tag("d", 40),
            _tag("e", 0, "uint16", "input")]
    blocks = plan_blocks(tags, max_registers=125)
    summary = sorted((b.register_type, b.start, b.count, [t.name for t in b.tags]) for b in blocks)
    assert summary == [("holding", 0, 10, ["a", "b", "c"]), ("holding", 40, 1, ["d"]),
                       ("input", 0, 1, ["e"])]
    small = plan_blocks(tags, max_registers=4)
    assert [(b.start, b.count) for b in small if b.register_type == "holding"] == [
        (0, 3), (9, 1), (40, 1)]


def test_plan_keeps_bits_with_their_word() -> None:
    word = _tag("w", 10)
    bit = ModbusTag.model_validate({"name": "b", "data_type": "bool", "register_type": "holding",
                                    "address": 10, "bit": 3})
    [block] = plan_blocks([word, bit], 125)
    assert (block.start, block.count, len(block.tags)) == (10, 1, 2)


# --- reading --------------------------------------------------------------------------------


async def test_reads_all_types_with_word_order(sim: ModbusSimServer) -> None:
    dev = meter(sim.port)
    simdev = SimulatedModbusDevice(sim, dev)
    simdev.set("voltage_l1", 231.5)
    simdev.set("current_l1", 12.25)
    simdev.set("energy", 9_876_543_210)
    simdev.set("status_word", 0b0100)
    simdev.set("demand_limit", 123.4)
    simdev.set("relay", True)
    simdev.set("model", "PM55")

    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver._connect()
    try:
        await driver.poll_once()
    finally:
        await driver._disconnect()

    got = {k: s.value for k, s in rec.latest().items()}
    assert math.isclose(got["voltage_l1"], 231.5, rel_tol=1e-6)
    assert math.isclose(got["current_l1"], 12.25, rel_tol=1e-6)
    assert got["energy"] == 9_876_543_210
    assert got["status_word"] == 4 and got["alarm"] is True
    assert math.isclose(got["demand_limit"], 123.4, abs_tol=0.05)
    assert got["relay"] is True
    assert got["model"] == "PM55"
    assert rec.latest()["voltage_l1"].tag == "kish.gate.meter_02.voltage_l1"
    assert rec.state is DeviceState.ONLINE


async def test_out_of_range_value_is_uncertain(sim: ModbusSimServer) -> None:
    dev = meter(sim.port)
    SimulatedModbusDevice(sim, dev).set("voltage_l1", 300.0)
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver._connect()
    await driver.poll_once()
    await driver._disconnect()
    assert rec.latest()["voltage_l1"].quality is Quality.UNCERTAIN


async def test_block_with_undefined_register_is_split(sim: ModbusSimServer) -> None:
    # voltage (0..1) and current (6..7) are merged into one block 0..7, but 2..5 do not exist
    dev = meter(sim.port)
    SimulatedModbusDevice(sim, dev)
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver._connect()
    await driver.poll_once()
    await driver._disconnect()
    assert all(s.quality is not Quality.BAD for s in rec.samples), rec.samples
    assert any(b.split for b in driver._blocks)


async def test_device_exception_marks_tags_bad(sim: ModbusSimServer) -> None:
    dev = meter(sim.port)
    SimulatedModbusDevice(sim, dev)
    sim.unit(1).exception_addresses.add(("holding", 100))
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver._connect()
    await driver.poll_once()
    await driver._disconnect()
    latest = rec.latest()
    assert latest["demand_limit"].quality is Quality.BAD
    assert latest["voltage_l1"].quality is Quality.GOOD
    assert rec.state is DeviceState.ERROR


# --- lifecycle ------------------------------------------------------------------------------


async def test_offline_then_recovers(sim: ModbusSimServer) -> None:
    dev = meter(sim.port)
    SimulatedModbusDevice(sim, dev)
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.1
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        sim.unit(1).offline = True
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert rec.latest()["voltage_l1"].quality is Quality.BAD
        sim.unit(1).offline = False
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        assert rec.latest()["voltage_l1"].quality is Quality.GOOD
    finally:
        await driver.stop()


async def test_connection_refused_is_offline() -> None:
    dev = meter(1)  # nothing listens on port 1
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.1
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert "cannot connect" in (rec.statuses[-1].last_error or "")
    finally:
        await driver.stop()


async def test_disabled_device_is_not_polled(sim: ModbusSimServer) -> None:
    dev = meter(sim.port).model_copy(update={"enabled": False})
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver.start()
    assert rec.state is DeviceState.DISABLED and rec.samples == []
    await driver.stop()


async def test_gateway_units_share_one_connection(sim: ModbusSimServer) -> None:
    a = meter(sim.port, unit_id=1, device_id="meter_a")
    b = meter(sim.port, unit_id=2, device_id="meter_b")
    SimulatedModbusDevice(sim, a).set("voltage_l1", 221.0)
    SimulatedModbusDevice(sim, b).set("voltage_l1", 239.0)
    rec = Recorder()
    da = ModbusTcpDriver(a, rec.on_samples, rec.on_status)
    db = ModbusTcpDriver(b, rec.on_samples, rec.on_status)
    await da._connect()
    await db._connect()
    try:
        assert da._conn is db._conn
        await da.poll_once()
        await db.poll_once()
    finally:
        await da._disconnect()
        await db._disconnect()
    volts = {s.tag: round(s.value, 1) for s in rec.samples if s.tag.endswith("voltage_l1")}
    assert volts == {"kish.gate.meter_a.voltage_l1": 221.0, "kish.gate.meter_b.voltage_l1": 239.0}


# --- writing --------------------------------------------------------------------------------


async def test_write_holding_and_coil_with_grant(sim: ModbusSimServer) -> None:
    dev = meter(sim.port)
    simdev = SimulatedModbusDevice(sim, dev)
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver._connect()
    try:
        await driver.write_tag("demand_limit", grant(dev, "demand_limit", 2500))  # raw = 250.0 kW
        await driver.write_tag("relay", grant(dev, "relay", True))
        assert await driver.read_tag("demand_limit") == pytest.approx(250.0)
        assert await driver.read_tag("relay") is True
    finally:
        await driver._disconnect()
    assert ("holding", 100, [2500]) in sim.unit(1).writes
    assert simdev.get_raw_bit("relay") is True


async def test_write_needs_grant_and_read_write_tag(sim: ModbusSimServer) -> None:
    dev = meter(sim.port)
    SimulatedModbusDevice(sim, dev)
    driver = ModbusTcpDriver(dev, Recorder().on_samples, Recorder().on_status)
    await driver._connect()
    try:
        with pytest.raises(WriteNotPermitted, match="WriteGrant"):
            await driver.write_tag("demand_limit", 2500)  # type: ignore[arg-type]
        with pytest.raises(WriteNotPermitted, match="not read_write"):
            await driver.write_tag("status_word", grant(dev, "status_word", 1))
        with pytest.raises(WriteNotPermitted, match="grant is for"):
            await driver.write_tag("relay", grant(dev, "demand_limit", True))
    finally:
        await driver._disconnect()
    assert sim.unit(1).writes == []
