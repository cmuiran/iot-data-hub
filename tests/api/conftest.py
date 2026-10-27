"""A complete backend (store + hub + Modbus simulator + API) for API tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from backend.app import create_app
from backend.security import hash_password
from backend.services import Services
from backend.settings import Settings
from hub.config_loader import load_config
from hub.runtime import Hub
from hub.storage.postgres import PostgresStore
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice

PASSWORD = "Correct-Horse-42"
GATE_METER = "kish.gate.meter_02"
PUMP_DRIVE = "kish.pump_station.drive_01"


def _devices(port: int) -> list[dict[str, Any]]:
    conn = {"host": "127.0.0.1", "port": port, "timeout_ms": 500, "retries": 0}
    return [
        {"device_id": "meter_02", "name": "Gate meter", "site": "kish", "area": "gate",
         "protocol": "modbus_tcp", "poll_interval_ms": 100,
         "connection": conn | {"unit_id": 1},
         "tags": [
             {"name": "voltage_l1", "unit": "V", "data_type": "float32",
              "register_type": "input", "address": 0},
             {"name": "breaker_closed", "data_type": "bool", "register_type": "discrete_input",
              "address": 0},
             {"name": "demand_limit", "unit": "kW", "data_type": "uint16",
              "register_type": "holding", "address": 100, "scale": 0.1,
              "access": "read_write", "min": 0, "max": 500},
             {"name": "relay", "data_type": "bool", "register_type": "coil", "address": 0,
              "access": "read_write"},
         ]},
        {"device_id": "drive_01", "name": "Pump drive", "site": "kish", "area": "pump_station",
         "protocol": "modbus_tcp", "poll_interval_ms": 100,
         "connection": conn | {"unit_id": 5},
         "tags": [
             {"name": "speed_setpoint", "unit": "rpm", "data_type": "uint16",
              "register_type": "holding", "address": 10, "access": "read_write",
              "min": 0, "max": 3000},
             {"name": "frequency", "unit": "Hz", "data_type": "uint16",
              "register_type": "holding", "address": 101, "scale": 0.01},
         ]},
    ]


CONTROL = {
    "defaults": {"min_interval_s": 0},
    "tags": {
        f"{GATE_METER}.demand_limit": {
            "max_step": 100,
            "interlocks": [{"tag": f"{GATE_METER}.breaker_closed", "op": "eq", "value": True,
                            "max_age_s": 10, "message": "breaker must be closed"}],
        },
        f"{PUMP_DRIVE}.speed_setpoint": {"confirm": True, "min_interval_s": 0},
        f"{GATE_METER}.relay": {"min_interval_s": 30},
    },
}
ALARMS = {"offline_delay_s": 1, "rules": [
    {"tag": f"{GATE_METER}.voltage_l1", "hi": 250, "severity": "high"}]}


@dataclass
class Backend:
    client: httpx.AsyncClient
    services: Services
    store: PostgresStore
    sim: ModbusSimServer
    meter: SimulatedModbusDevice
    drive: SimulatedModbusDevice

    async def user(self, username: str, *grants: tuple[str, str | None, str | None],
                   must_change: bool = False, password: str = PASSWORD) -> int:
        user = await self.store.create_user(username, username.title(), hash_password(password),
                                            must_change_password=must_change)
        for role, site, area in grants:
            await self.store.grant_role(user.user_id, role, site, area, None)
        return user.user_id

    async def login(self, username: str, password: str = PASSWORD) -> dict[str, Any]:
        r = await self.client.post("/api/auth/login",
                                   json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return r.json()

    async def headers(self, username: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {(await self.login(username))['access_token']}"}

    async def wait(self, predicate: Any, timeout: float = 10.0) -> None:
        async def _loop() -> None:
            while not predicate():
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_loop(), timeout)


def make_settings(**overrides: Any) -> Settings:
    base = {"jwt_secret": "x" * 40, "control_enabled": True, "login_max_failures": 3,
            "lockout_minutes": 15, "password_min_length": 10,
            # the developer's disk is not what these tests check (tests/api/test_health_watch
            # sets its own thresholds where the disk check itself is tested)
            "disk_warn_percent": 0.5, "disk_critical_percent": 0.1}
    return Settings.model_validate(base | overrides)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def backend(tmp_path: Path, store: PostgresStore, settings: Settings
                  ) -> AsyncIterator[Backend]:
    async with ModbusSimServer() as sim:
        config = tmp_path / "config"
        (config / "devices").mkdir(parents=True)
        for d in _devices(sim.port):
            (config / "devices" / f"{d['device_id']}.yaml").write_text(yaml.safe_dump(d), "utf-8")
        (config / "control.yaml").write_text(yaml.safe_dump(CONTROL), "utf-8")
        (config / "alarms.yaml").write_text(yaml.safe_dump(ALARMS), "utf-8")
        report = load_config(config, env={})
        assert not report.has_errors, report.issues
        by_id = {d.device.device_id: d.device for d in report.devices}
        meter = SimulatedModbusDevice(sim, by_id["meter_02"])
        drive = SimulatedModbusDevice(sim, by_id["drive_01"])
        meter.set("voltage_l1", 230.0)
        meter.set("breaker_closed", True)
        meter.set("demand_limit", 200.0)
        hub = Hub(report, store=store, flush_interval_s=0.1)
        settings = settings.model_copy(update={"config_dir": config})
        services = Services.build(settings, report, store, hub)
        await services.start()
        app = create_app(services)
        transport = httpx.ASGITransport(app=app, client=("10.1.2.3", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as client:
            b = Backend(client, services, store, sim, meter, drive)
            await b.wait(lambda: f"{GATE_METER}.demand_limit" in hub.latest
                         and f"{PUMP_DRIVE}.speed_setpoint" in hub.latest)
            try:
                yield b
            finally:
                await services.stop()
