"""Modbus RTU: real RTU frames (CRC) through the serial client and through RTU-over-TCP."""

from __future__ import annotations

import math
from typing import Any

import pytest

from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.modbus_tcp import ModbusTcpDriver
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice, crc16
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration

TAGS = [
    {"name": "voltage", "unit": "V", "data_type": "float32", "register_type": "input",
     "address": 0},
    {"name": "setpoint", "data_type": "uint16", "register_type": "holding", "address": 10,
     "access": "read_write", "min": 0, "max": 1000},
    {"name": "relay", "data_type": "bool", "register_type": "coil", "address": 0,
     "access": "read_write"},
]


def rtu_device(port: int, unit_id: int, device_id: str = "meter_rtu") -> Any:
    return device({"device_id": device_id, "name": "RTU meter", "site": "kish", "area": "gate",
                   "protocol": "modbus_rtu", "poll_interval_ms": 100,
                   "connection": {"port": f"socket://127.0.0.1:{port}", "unit_id": unit_id,
                                  "baudrate": 19200, "timeout_ms": 500, "retries": 0},
                   "tags": TAGS})


def test_crc16_known_vector() -> None:
    # read holding registers: unit 1, address 0, count 10 -> CRC bytes C5 CD on the wire
    assert crc16(bytes.fromhex("01030000000A")).to_bytes(2, "little") == bytes.fromhex("C5CD")


@pytest.fixture
async def rtu_sim() -> Any:
    async with ModbusSimServer(framer="rtu") as server:
        yield server


async def test_serial_client_reads_and_writes(rtu_sim: ModbusSimServer) -> None:
    a, b = rtu_device(rtu_sim.port, 1, "meter_a"), rtu_device(rtu_sim.port, 2, "meter_b")
    SimulatedModbusDevice(rtu_sim, a).set("voltage", 229.5)
    SimulatedModbusDevice(rtu_sim, b).set("voltage", 231.5)
    rec = Recorder()
    da = ModbusTcpDriver(a, rec.on_samples, rec.on_status)
    db = ModbusTcpDriver(b, rec.on_samples, rec.on_status)
    await da._connect()
    await db._connect()
    try:
        assert da._conn is db._conn                     # one serial line, one connection
        await da.poll_once()
        await db.poll_once()
        volts = {s.tag: s.value for s in rec.samples if s.tag.endswith("voltage")}
        assert math.isclose(volts["kish.gate.meter_a.voltage"], 229.5, rel_tol=1e-6)
        assert math.isclose(volts["kish.gate.meter_b.voltage"], 231.5, rel_tol=1e-6)
        await da.write_tag("setpoint", WriteGrant("r", a.tag_path("setpoint"), 750))
        await da.write_tag("relay", WriteGrant("r", a.tag_path("relay"), True))
        assert await da.read_back("setpoint") == 750 and await da.read_back("relay") is True
    finally:
        await da._disconnect()
        await db._disconnect()
    assert ("holding", 10, [750]) in rtu_sim.unit(1).writes
    assert rtu_sim.crc_errors == 0


async def test_rtu_over_tcp_framer(rtu_sim: ModbusSimServer) -> None:
    dev = device({"device_id": "gw_meter", "name": "Meter via transparent gateway",
                  "site": "kish", "area": "gate", "protocol": "modbus_tcp",
                  "poll_interval_ms": 100,
                  "connection": {"host": "127.0.0.1", "port": rtu_sim.port, "unit_id": 3,
                                 "framer": "rtu", "timeout_ms": 500, "retries": 0},
                  "tags": TAGS})
    SimulatedModbusDevice(rtu_sim, dev).set("voltage", 220.25)
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    await driver._connect()
    try:
        await driver.poll_once()
    finally:
        await driver._disconnect()
    assert rec.latest()["voltage"].value == pytest.approx(220.25)


async def test_silent_rtu_slave_goes_offline(rtu_sim: ModbusSimServer) -> None:
    dev = rtu_device(rtu_sim.port, 7)
    SimulatedModbusDevice(rtu_sim, dev)
    rec = Recorder()
    driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.1
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        rtu_sim.unit(7).offline = True
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        rtu_sim.unit(7).offline = False
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
    finally:
        await driver.stop()
