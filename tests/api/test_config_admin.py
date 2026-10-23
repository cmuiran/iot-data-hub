from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.drivers.base import DeviceState
from simulators.modbus_sim import SimulatedModbusDevice
from tests.api.conftest import GATE_METER, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def new_meter(port: int, unit_id: int = 9, device_id: str = "meter_09") -> dict[str, Any]:
    return {"device_id": device_id, "name": "New meter", "site": "kish", "area": "gate",
            "protocol": "modbus_tcp", "poll_interval_ms": 100,
            "connection": {"host": "127.0.0.1", "port": port, "unit_id": unit_id,
                           "timeout_ms": 500, "retries": 0},
            "tags": [{"name": "voltage_l1", "unit": "V", "data_type": "float32",
                      "register_type": "input", "address": 0}]}


async def eng(b: Backend) -> dict[str, str]:
    await b.user("eng.one", ("engineer", None, None))
    return await b.headers("eng.one")


def config_dir(b: Backend) -> Path:
    return Path(b.services.settings.config_dir)


async def test_validate_device_dry_run(backend: Backend) -> None:
    h = await eng(backend)
    bad = await backend.client.post("/api/config/devices/validate", headers=h,
                                    json={"device_id": "Bad-ID", "protocol": "modbus_tcp"})
    assert bad.json()["valid"] is False and bad.json()["errors"]
    clash = new_meter(backend.sim.port, unit_id=1)          # unit 1 = meter_02 already
    r = (await backend.client.post("/api/config/devices/validate", headers=h,
                                   json=clash)).json()
    assert r["valid"] is False and any("unit_id 1 is also used" in i["message"]
                                       for i in r["issues"])
    assert not (config_dir(backend) / "devices" / "meter_09.yaml").exists()


async def test_create_update_disable_delete_device_live(backend: Backend) -> None:
    h = await eng(backend)
    body = new_meter(backend.sim.port)
    from hub.models import DEVICE_ADAPTER

    sim_dev = SimulatedModbusDevice(backend.sim, DEVICE_ADAPTER.validate_python(body))
    sim_dev.set("voltage_l1", 222.0)

    r = await backend.client.post("/api/config/devices", headers=h, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["changes"]["added"] == ["meter_09"]
    assert set(r.json()["changes"]["unchanged"]) == {"meter_02", "drive_01"}
    hub = backend.services.hub
    await backend.wait(lambda: "kish.gate.meter_09.voltage_l1" in hub.latest)
    assert hub.latest["kish.gate.meter_09.voltage_l1"].value == pytest.approx(222.0)
    assert yaml.safe_load((config_dir(backend) / "devices" / "meter_09.yaml").read_text())[
        "device_id"] == "meter_09"
    dup = await backend.client.post("/api/config/devices", headers=h, json=body)
    assert dup.status_code == 409 and dup.json()["error"] == "device_exists"

    body["poll_interval_ms"] = 200
    r = await backend.client.put("/api/config/devices/meter_09", headers=h, json=body)
    assert r.status_code == 200 and r.json()["changes"]["restarted"] == ["meter_09"]

    r = await backend.client.patch("/api/config/devices/meter_09", headers=h,
                                   json={"enabled": False})
    assert r.status_code == 200
    await backend.wait(lambda: hub.statuses["meter_09"].state is DeviceState.DISABLED)

    r = await backend.client.delete("/api/config/devices/meter_09", headers=h)
    assert r.status_code == 200 and r.json()["changes"]["removed"] == ["meter_09"]
    assert "meter_09" not in hub.devices
    assert not any(p.startswith("kish.gate.meter_09.") for p in hub.latest)
    assert list((config_dir(backend) / ".trash").glob("meter_09-*.yaml"))
    actions = [a.detail["action"] for a in await backend.store.list_auth_audit(
        event="config_changed")]
    assert actions == ["device_deleted", "device_updated", "device_updated", "device_created"]


async def test_invalid_change_is_refused_and_nothing_written(backend: Backend) -> None:
    h = await eng(backend)
    clash = new_meter(backend.sim.port, unit_id=1)
    r = await backend.client.post("/api/config/devices", headers=h, json=clash)
    assert r.status_code == 422 and r.json()["error"] == "config_invalid"
    assert r.json()["issues"]
    assert not (config_dir(backend) / "devices" / "meter_09.yaml").exists()
    assert "meter_09" not in backend.services.hub.devices
    schema = await backend.client.post("/api/config/devices", headers=h,
                                       json={"device_id": "x", "protocol": "nope"})
    assert schema.status_code == 422 and schema.json()["error"] == "device_invalid"


async def test_alarm_rules_and_control_policy(backend: Backend) -> None:
    h = await eng(backend)
    rule = {"tag": f"{GATE_METER}.voltage_l1", "hi": 240, "severity": "critical"}
    r = await backend.client.put("/api/config/alarms/rules", headers=h, json=rule)
    assert r.status_code == 200, r.text
    rules = (await backend.client.get("/api/config/alarms", headers=h)).json()["rules"]
    assert {(x["tag"], x["hi"], x["severity"]) for x in rules} == {
        (f"{GATE_METER}.voltage_l1", 240.0, "critical")}
    backend.meter.set("voltage_l1", 245.0)        # the new limit is live
    await asyncio.sleep(0.6)
    active = await backend.store.active_alarms()
    assert any(a.source == f"{GATE_METER}.voltage_l1" and a.severity == "critical"
               for a in active)

    unknown = await backend.client.put("/api/config/alarms/rules", headers=h,
                                       json={"tag": "kish.gate.nope.x", "hi": 1})
    assert unknown.status_code == 422
    r = await backend.client.delete(f"/api/config/alarms/rules/{GATE_METER}.voltage_l1",
                                    headers=h)
    assert r.status_code == 200
    assert backend.services.report.alarms.rules == []

    limit = f"{GATE_METER}.demand_limit"
    r = await backend.client.put(f"/api/config/control/tags/{limit}", headers=h,
                                 json={"confirm": True, "max_step": 20})
    assert r.status_code == 200
    policy = backend.services.control.control.policy_for(limit)
    assert policy.confirm is True and policy.max_step == 20 and policy.min_interval_s == 0
    read_only = await backend.client.put(f"/api/config/control/tags/{GATE_METER}.voltage_l1",
                                         headers=h, json={"confirm": True})
    assert read_only.status_code == 422


async def test_reload_after_manual_edit(backend: Backend) -> None:
    h = await eng(backend)
    path = config_dir(backend) / "devices" / "drive_01.yaml"
    data = yaml.safe_load(path.read_text())
    data["name"] = "Pump drive (renamed)"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    r = await backend.client.post("/api/config/reload", headers=h)
    assert r.status_code == 200 and r.json()["changes"]["restarted"] == ["drive_01"]
    assert backend.services.hub.devices["drive_01"].name == "Pump drive (renamed)"
    path.write_text("device_id: Broken\n", encoding="utf-8")
    bad = await backend.client.post("/api/config/reload", headers=h)
    assert bad.status_code == 422
    assert backend.services.hub.devices["drive_01"].name == "Pump drive (renamed)"


async def test_config_needs_config_edit(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    r = await backend.client.post("/api/config/devices", headers=h,
                                  json=new_meter(backend.sim.port))
    assert r.status_code == 403


# --- shelving -----------------------------------------------------------------------------------


async def test_shelving(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("view.all", ("viewer", None, None))
    op = await backend.headers("op.gate")
    volt = f"{GATE_METER}.voltage_l1"
    backend.meter.set("voltage_l1", 260.0)
    await backend.wait(lambda: backend.services.hub.latest[volt].value > 250)
    await asyncio.sleep(0.3)
    r = await backend.client.post("/api/alarms/shelve", headers=op,
                                  json={"source": volt, "minutes": 30, "reason": "sensor work"})
    assert r.status_code == 204
    alarms = (await backend.client.get("/api/alarms", headers=op)).json()
    assert alarms[0]["shelved"]["reason"] == "sensor work"
    assert alarms[0]["shelved"]["shelved_by"] == "op.gate"
    shelves = (await backend.client.get("/api/alarms/shelves", headers=op)).json()
    assert [s["source"] for s in shelves] == [volt]

    too_long = await backend.client.post("/api/alarms/shelve", headers=op, json={
        "source": volt, "minutes": 5000, "reason": "forever"})
    assert too_long.status_code == 422
    viewer = await backend.client.post("/api/alarms/shelve",
                                       headers=await backend.headers("view.all"),
                                       json={"source": volt, "minutes": 5, "reason": "x x"})
    assert viewer.status_code == 403
    assert (await backend.client.delete(f"/api/alarms/shelve/{volt}", headers=op)
            ).status_code == 204
    assert (await backend.client.delete(f"/api/alarms/shelve/{volt}", headers=op)
            ).status_code == 404
    events = [a.event for a in await backend.store.list_auth_audit()]
    assert "alarm_shelved" in events and "alarm_unshelved" in events
