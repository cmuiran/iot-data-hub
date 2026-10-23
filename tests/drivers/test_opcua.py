from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.opcua import OpcUaDriver
from hub.models import OpcUaDevice, Quality
from simulators.opcua_sim import SimulatedOpcUaServer
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def plc(endpoint: str = "opc.tcp://127.0.0.1:4840/", **conn: Any) -> OpcUaDevice:
    return device({
        "device_id": "plc_02", "name": "Booster PLC", "site": "kish", "area": "pump_station",
        "protocol": "opcua", "poll_interval_ms": 100,
        "connection": {"endpoint_url": endpoint, "security_policy": "None",
                       "security_mode": "None", "namespace": "urn:kish:pump:plc_02",
                       "timeout_ms": 2000} | conn,
        "tags": [
            {"name": "motor_speed", "unit": "rpm", "data_type": "float64",
             "node_id": "ns=2;s=Pump2.Speed", "min": 0, "max": 3000},
            {"name": "motor_running", "data_type": "bool", "node_id": "ns=2;i=1042"},
            {"name": "run_hours", "unit": "h", "data_type": "uint32", "node_id": "s=Pump2.Hours"},
            {"name": "speed_setpoint", "unit": "rpm", "data_type": "float32",
             "node_id": "ns=2;s=Pump2.Setpoint", "access": "read_write", "min": 0, "max": 3000},
        ],
    })


@pytest.fixture
async def server() -> Any:
    sim = SimulatedOpcUaServer([plc()])
    async with sim:
        yield sim


async def _started(server: SimulatedOpcUaServer) -> tuple[OpcUaDriver, Recorder, OpcUaDevice]:
    dev = plc(server.endpoint)
    rec = Recorder()
    driver = OpcUaDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.2
    await driver.start()
    return driver, rec, dev


async def test_subscription_delivers_values(server: SimulatedOpcUaServer) -> None:
    await server.set("plc_02", "motor_speed", 1480.5)
    await server.set("plc_02", "motor_running", True)
    await server.set("plc_02", "run_hours", 1234)
    driver, rec, _ = await _started(server)
    try:
        await rec.wait_for(lambda: {"motor_speed", "motor_running", "run_hours"}
                           <= set(rec.latest()))
        latest = rec.latest()
        assert latest["motor_speed"].value == pytest.approx(1480.5)
        assert latest["motor_running"].value is True
        assert latest["run_hours"].value == 1234   # bare s= resolved through namespace URI
        assert rec.state is DeviceState.ONLINE

        await server.set("plc_02", "motor_speed", 900.0)
        await rec.wait_for(lambda: rec.latest()["motor_speed"].value == pytest.approx(900.0))
    finally:
        await driver.stop()


async def test_bad_status_gives_bad_sample(server: SimulatedOpcUaServer) -> None:
    driver, rec, _ = await _started(server)
    try:
        await rec.wait_for(lambda: "motor_speed" in rec.latest())
        await server.set_bad("plc_02", "motor_speed")
        await rec.wait_for(lambda: rec.latest()["motor_speed"].quality is Quality.BAD)
    finally:
        await driver.stop()


async def test_write_and_read_back(server: SimulatedOpcUaServer) -> None:
    driver, rec, dev = await _started(server)
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        await driver.write_tag("speed_setpoint",
                               WriteGrant("r1", dev.tag_path("speed_setpoint"), 1500.0))
        assert await driver.read_back("speed_setpoint") == pytest.approx(1500.0)
        assert await server.get("plc_02", "speed_setpoint") == pytest.approx(1500.0)
    finally:
        await driver.stop()


async def test_server_restart_reconnects() -> None:
    sim = SimulatedOpcUaServer([plc()])
    await sim.start()
    driver, rec, _ = await _started(sim)
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        await sim.stop()
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE, timeout=20)
        sim2 = SimulatedOpcUaServer([plc()], port=sim.port)
        await sim2.start()
        try:
            await sim2.set("plc_02", "motor_speed", 42.0)
            await rec.wait_for(lambda: rec.state is DeviceState.ONLINE, timeout=20)
            await rec.wait_for(
                lambda: rec.latest()["motor_speed"].value == pytest.approx(42.0), timeout=20)
        finally:
            await driver.stop()
            await sim2.stop()
    finally:
        await driver.stop()


async def test_unreachable_endpoint_is_offline() -> None:
    rec = Recorder()
    driver = OpcUaDriver(plc("opc.tcp://127.0.0.1:1/"), rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.2
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
    finally:
        await driver.stop()
    await asyncio.sleep(0)
