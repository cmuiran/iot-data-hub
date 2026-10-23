"""Modbus multi-bit fields and BCD values against the Modbus simulator."""

from __future__ import annotations

import pytest

from hub.codec import to_raw
from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.modbus_tcp import ModbusTcpDriver
from hub.models import ModbusDevice, Quality, RegisterType
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def drive(port: int, word_order: str = "big") -> ModbusDevice:
    return device({
        "device_id": "drive_02", "name": "Old drive", "site": "kish", "area": "pump_station",
        "protocol": "modbus_tcp", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "unit_id": 1, "timeout_ms": 500,
                       "retries": 0, "word_order": word_order},
        "tags": [
            {"name": "status_word", "data_type": "uint16", "register_type": "holding",
             "address": 0},
            {"name": "ready", "data_type": "bool", "register_type": "holding", "address": 0,
             "bit": 0},
            {"name": "mode", "data_type": "uint16", "register_type": "holding", "address": 0,
             "bit": 4, "bit_count": 3, "states": {0: "off", 1: "local", 2: "remote"}},
            {"name": "hours", "data_type": "uint32", "register_type": "input", "address": 10,
             "encoding": "bcd"},
            {"name": "preset", "data_type": "uint16", "register_type": "holding", "address": 20,
             "encoding": "bcd", "access": "read_write", "min": 0, "max": 9999},
        ],
    })


@pytest.mark.parametrize("word_order", ["big", "little"])
async def test_bit_field_and_bcd(word_order: str) -> None:
    async with ModbusSimServer() as server:
        dev = drive(server.port, word_order)
        sim = SimulatedModbusDevice(server, dev)
        sim.set("status_word", 0b0000_0000_0010_0001)  # ready, mode = 2
        sim.set("hours", 12345678)
        sim.set("preset", 1500)
        unit = server.unit(1)
        hours_regs = [unit.areas[RegisterType.INPUT][10], unit.areas[RegisterType.INPUT][11]]
        assert hours_regs == ([0x1234, 0x5678] if word_order == "big" else [0x5678, 0x1234])
        rec = Recorder()
        driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: len(rec.latest()) == 5)
            v = {k: s.value for k, s in rec.latest().items()}
            assert v == {"status_word": 0x21, "ready": True, "mode": 2, "hours": 12345678,
                         "preset": 1500}
            tag = next(t for t in dev.tags if t.name == "preset")
            await driver.write_tag("preset", WriteGrant("t", dev.tag_path("preset"),
                                                        to_raw(tag, 2468)))
            assert unit.areas[RegisterType.HOLDING][20] == 0x2468
            assert await driver.read_tag("preset") == 2468
        finally:
            await driver.stop()


async def test_invalid_bcd_is_bad() -> None:
    async with ModbusSimServer() as server:
        dev = drive(server.port)
        SimulatedModbusDevice(server, dev)
        server.unit(1).areas[RegisterType.HOLDING][20] = 0x12AF   # not decimal digits
        rec = Recorder()
        driver = ModbusTcpDriver(dev, rec.on_samples, rec.on_status)
        await driver.start()
        try:
            await rec.wait_for(lambda: len(rec.latest()) == 5)
            assert rec.latest()["preset"].quality is Quality.BAD       # only this value
            assert rec.latest()["status_word"].quality is Quality.GOOD
            assert rec.state is DeviceState.ONLINE                      # the device stays up
        finally:
            await driver.stop()
