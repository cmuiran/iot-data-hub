"""The whole backend over real sockets: uvicorn + WebSocket + Modbus/MQTT/OPC UA simulators."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from websockets.asyncio.client import connect

from backend.app import create_app
from backend.security import hash_password
from backend.services import Services
from backend.settings import Settings
from hub.config_loader import load_config
from hub.runtime import Hub
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from simulators.mqtt_sim import EmbeddedBroker, SimulatedMqttDevice, free_port
from simulators.opcua_sim import SimulatedOpcUaServer

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
PASSWORD = "Correct-Horse-42"


def _config(tmp: Path, modbus_port: int, broker_port: int, opcua_port: int) -> Path:
    config = tmp / "config"
    (config / "devices").mkdir(parents=True)
    devices = [
        {"device_id": "meter_02", "name": "Gate meter", "site": "kish", "area": "gate",
         "protocol": "modbus_tcp", "poll_interval_ms": 200,
         "connection": {"host": "127.0.0.1", "port": modbus_port, "unit_id": 1,
                        "timeout_ms": 500, "retries": 0, "word_order": "little"},
         "tags": [{"name": "voltage_l1", "unit": "V", "data_type": "float32",
                   "register_type": "input", "address": 0},
                  {"name": "energy", "unit": "Wh", "data_type": "int64",
                   "register_type": "input", "address": 3203}]},
        {"device_id": "env_sensor_02", "name": "Gate sensor", "site": "kish", "area": "gate",
         "protocol": "mqtt",
         "connection": {"broker_ref": "local", "telemetry_topic": "kish/gate/th-01/data",
                        "command_topic": "kish/gate/th-01/cmd",
                        "status_topic": "kish/gate/th-01/status",
                        "timestamp_path": "$.ts", "timestamp_format": "unix_s"},
         "tags": [{"name": "temperature", "unit": "degC", "data_type": "float32",
                   "json_path": "$.data.t"},
                  {"name": "fan_on", "data_type": "bool", "json_path": "$.fan",
                   "access": "read_write"}]},
        {"device_id": "plc_02", "name": "Booster PLC", "site": "kish", "area": "pump_station",
         "protocol": "opcua", "poll_interval_ms": 200,
         "connection": {"endpoint_url": f"opc.tcp://127.0.0.1:{opcua_port}/hub-sim/",
                        "security_policy": "None", "security_mode": "None"},
         "tags": [{"name": "motor_speed", "unit": "rpm", "data_type": "float64",
                   "node_id": "ns=2;s=Pump2.Speed"},
                  {"name": "speed_setpoint", "unit": "rpm", "data_type": "float32",
                   "node_id": "ns=2;s=Pump2.Setpoint", "access": "read_write",
                   "min": 0, "max": 3000}]},
    ]
    for d in devices:
        (config / "devices" / f"{d['device_id']}.yaml").write_text(yaml.safe_dump(d), "utf-8")
    (config / "brokers.yaml").write_text(yaml.safe_dump(
        {"brokers": [{"broker_id": "local", "host": "127.0.0.1", "port": broker_port}]}), "utf-8")
    (config / "control.yaml").write_text(yaml.safe_dump({"defaults": {"min_interval_s": 0}}),
                                         "utf-8")
    return config


async def _until(predicate: Any, timeout: float = 15.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_loop(), timeout)


async def test_full_system(tmp_path: Path, store: Any) -> None:
    broker = EmbeddedBroker()
    await broker.start()
    modbus = ModbusSimServer()
    await modbus.start()
    opcua_port = free_port()
    report = load_config(_config(tmp_path, modbus.port, broker.port, opcua_port), env={})
    assert not report.has_errors, report.issues
    devs = {d.device.device_id: d.device for d in report.devices}
    meter = SimulatedModbusDevice(modbus, devs["meter_02"])
    meter.set("voltage_l1", 231.25)
    meter.set("energy", 12_345_678_901)
    opcua = SimulatedOpcUaServer([devs["plc_02"]], port=opcua_port)
    await opcua.start()
    await opcua.set("plc_02", "motor_speed", 1450.0)
    sensor = SimulatedMqttDevice(devs["env_sensor_02"], broker.host, broker.port)

    settings = Settings(jwt_secret="y" * 40, control_enabled=True)
    services = Services.build(settings, report, store, Hub(report, store=store,
                                                           flush_interval_s=0.2))
    await services.start()
    admin = await store.create_user("admin", "Admin", hash_password(PASSWORD),
                                    must_change_password=False)
    await store.grant_role(admin.user_id, "admin", None, None, None)
    pump = await store.create_user("view.pump", "Pump viewer", hash_password(PASSWORD),
                                   must_change_password=False)
    await store.grant_role(pump.user_id, "viewer", "kish", "pump_station", None)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(services), host="127.0.0.1", port=port,
                                           loop="none", lifespan="off", log_level="warning",
                                           ws="websockets-sansio"))
    serve_task = asyncio.create_task(server.serve())
    try:
        await _until(lambda: server.started)
        await sensor.connect()
        sensor.values.update(temperature=24.5, fan_on=False)
        await sensor.publish()
        hub = services.hub
        await _until(lambda: {"kish.gate.meter_02.energy", "kish.gate.env_sensor_02.temperature",
                              "kish.pump_station.plc_02.motor_speed"} <= set(hub.latest))
        assert hub.latest["kish.gate.meter_02.energy"].value == 12_345_678_901

        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base) as http:
            token = (await http.post("/api/auth/login", json={
                "username": "admin", "password": PASSWORD})).json()["access_token"]
            h = {"Authorization": f"Bearer {token}"}
            devices = {d["device_id"]: d["state"] for d in
                       (await http.get("/api/devices", headers=h)).json()}
            assert devices == {"meter_02": "online", "env_sensor_02": "online",
                               "plc_02": "online"}

            # live stream: snapshot, then new samples
            async with connect(f"ws://127.0.0.1:{port}/api/ws") as ws:
                await ws.send(json.dumps({"type": "auth", "token": token}))
                snapshot = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert snapshot["type"] == "snapshot"
                assert {t["tag"] for t in snapshot["tags"]} >= {
                    "kish.gate.meter_02.voltage_l1", "kish.pump_station.plc_02.motor_speed"}
                await ws.send(json.dumps({"type": "subscribe",
                                          "tags": ["kish.gate.env_sensor_02.temperature"]}))
                sensor.values["temperature"] = 26.0
                await sensor.publish()
                got = None
                for _ in range(50):
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if msg["type"] == "samples":
                        got = msg["samples"]
                        break
                assert got and {s["tag"] for s in got} == {"kish.gate.env_sensor_02.temperature"}
                assert got[0]["value"] == pytest.approx(26.0)

            # writes through the control layer: MQTT (read-back from telemetry) and OPC UA
            r = await http.post("/api/control/write", headers=h,
                                json={"tag": "kish.gate.env_sensor_02.fan_on", "value": True})
            assert r.json()["status"] == "applied", r.text
            assert sensor.commands == [{"fan_on": True}]
            r = await http.post("/api/control/write", headers=h, json={
                "tag": "kish.pump_station.plc_02.speed_setpoint", "value": 1200.0})
            assert r.json()["status"] == "applied", r.text
            assert await opcua.get("plc_02", "speed_setpoint") == pytest.approx(1200.0)

            # a scoped viewer sees only its area, also on the WebSocket
            ptoken = (await http.post("/api/auth/login", json={
                "username": "view.pump", "password": PASSWORD})).json()["access_token"]
            async with connect(f"ws://127.0.0.1:{port}/api/ws") as ws:
                await ws.send(json.dumps({"type": "auth", "token": ptoken}))
                snap = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert {t["tag"].split(".")[1] for t in snap["tags"]} == {"pump_station"}
            async with connect(f"ws://127.0.0.1:{port}/api/ws") as ws:
                await ws.send(json.dumps({"type": "auth", "token": "garbage"}))
                with pytest.raises(Exception):  # closed with 4401
                    await asyncio.wait_for(ws.recv(), 5)

            await _until(lambda: hub.stats.samples_written >= 3)
            assert (await http.get("/api/health")).json()["status"] == "ok"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, 10)
        await sensor.disconnect()
        await services.stop()
        await opcua.stop()
        await modbus.stop()
        await broker.stop()

    audit = await store.list_write_audit()
    assert sorted({(r.tag_path, r.result) for r in audit if r.phase == "outcome"}) == [
        ("kish.gate.env_sensor_02.fan_on", "applied"),
        ("kish.pump_station.plc_02.speed_setpoint", "applied")]
