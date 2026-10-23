"""The hub's Modbus TCP server (output for SCADA): reads from live values, writes only through
the control-safety layer, as the configured user and from allowed addresses."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pymodbus.client import AsyncModbusTcpClient

from backend.modbus_server import HubModbusServer
from hub.models import ModbusServerFile
from simulators.mqtt_sim import free_port
from tests.api.conftest import GATE_METER, PUMP_DRIVE, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def config(port: int, write_clients: list[str] | None = None, **kw: Any) -> ModbusServerFile:
    return ModbusServerFile.model_validate({
        "host": "127.0.0.1", "port": port, "unit_id": 1,
        "points": [
            {"tag": f"{GATE_METER}.voltage_l1", "register_type": "input", "address": 0,
             "data_type": "float32"},
            {"tag": f"{GATE_METER}.voltage_l1", "register_type": "input", "address": 2,
             "source": "quality"},
            {"tag": f"{GATE_METER}.voltage_l1", "register_type": "input", "address": 3,
             "data_type": "uint16", "scale": 0.1},                     # 230.0 V -> 2300
            {"tag": f"{GATE_METER}.breaker_closed", "register_type": "discrete_input",
             "address": 0},
            {"tag": f"{GATE_METER}.demand_limit", "register_type": "holding", "address": 100,
             "data_type": "uint16", "writable": True},
            {"tag": f"{PUMP_DRIVE}.speed_setpoint", "register_type": "holding", "address": 110,
             "data_type": "uint16", "writable": True},
            {"tag": f"{GATE_METER}.relay", "register_type": "coil", "address": 0,
             "writable": True},
        ],
        "writes": {"as_user": "scada_link",
                   "allowed_clients": write_clients or ["127.0.0.1"]},
    } | kw)


@pytest.fixture
async def server(backend: Backend) -> AsyncIterator[tuple[HubModbusServer, AsyncModbusTcpClient]]:
    await backend.user("scada_link", ("operator", "kish", None))
    port = free_port()
    backend.services.report.modbus_server = config(port)
    srv = HubModbusServer(backend.services)
    await srv.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=10, retries=0)
    await client.connect()
    try:
        yield srv, client
    finally:
        client.close()
        await srv.stop()


async def test_reads_live_values(server: tuple[HubModbusServer, AsyncModbusTcpClient]) -> None:
    _, client = server
    r = await client.read_input_registers(0, count=4, device_id=1)
    assert not r.isError()
    volts = struct.unpack(">f", struct.pack(">HH", *r.registers[:2]))[0]
    assert volts == pytest.approx(230.0) and r.registers[2] == 0 and r.registers[3] == 2300
    bits = await client.read_discrete_inputs(0, count=1, device_id=1)
    assert bits.bits[0] is True
    other_unit = await client.read_input_registers(0, count=2, device_id=0)   # 0 = any
    assert not other_unit.isError()
    unknown = await client.read_input_registers(50, count=1, device_id=1)
    assert unknown.isError() and unknown.exception_code == 2


async def test_write_goes_through_the_safety_layer(
        backend: Backend, server: tuple[HubModbusServer, AsyncModbusTcpClient]) -> None:
    srv, client = server
    r = await client.write_register(100, 300, device_id=1)                 # 200 -> 300 kW
    assert not r.isError()
    await backend.wait(lambda: backend.services.hub.latest[f"{GATE_METER}.demand_limit"].value
                       == 300)
    audit = await backend.store.list_write_audit(tag_path=f"{GATE_METER}.demand_limit")
    assert {a.result for a in audit} >= {"requested", "applied"}
    assert all(a.username == "scada_link" for a in audit)
    assert any("Modbus server write from 127.0.0.1" in (a.reason or "") for a in audit)
    # out of range for the policy (max_step 100): refused by the control layer -> exception 04
    refused = await client.write_register(100, 450, device_id=1)
    assert refused.isError() and refused.exception_code == 4
    # two-person confirmation: accepted for confirmation -> exception 05 (acknowledge)
    pending = await client.write_register(110, 1500, device_id=1)
    assert pending.isError() and pending.exception_code == 5
    assert srv.counters["applied"] == 1 and srv.counters["pending"] == 1


async def test_writes_need_a_writable_point_and_an_allowed_client(
        backend: Backend, server: tuple[HubModbusServer, AsyncModbusTcpClient]) -> None:
    srv, client = server
    not_writable = await client.write_register(3, 1, device_id=1)         # input area address
    assert not_writable.isError() and not_writable.exception_code == 2
    partial = await client.write_registers(100, [1, 2], device_id=1)      # wider than the point
    assert partial.isError() and partial.exception_code in (2, 3)
    # a client outside writes.allowed_clients may read but not write
    srv.config = config(srv.port or 0, ["10.9.9.9/32"])
    backend.services.report.modbus_server = srv.config
    srv.build()
    denied = await client.write_register(100, 250, device_id=1)
    assert denied.isError() and denied.exception_code == 1
    readable = await client.read_input_registers(0, count=2, device_id=1)
    assert not readable.isError()
    assert not await backend.store.list_write_audit(tag_path=f"{GATE_METER}.demand_limit")


async def test_readers_can_be_limited(backend: Backend) -> None:
    port = free_port()
    backend.services.report.modbus_server = config(port, allowed_clients=["10.9.9.0/24"])
    srv = HubModbusServer(backend.services)
    await srv.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=2, retries=0)
    try:
        await client.connect()
        r = None
        try:
            r = await client.read_input_registers(0, count=2, device_id=1)
        except Exception:  # noqa: BLE001 - the server closed the connection
            r = None
        assert r is None or r.isError()
        assert srv.counters["connections_refused"] >= 1
    finally:
        client.close()
        await srv.stop()
