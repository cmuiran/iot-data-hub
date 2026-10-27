"""BMS phase 1 through the API: building model, maintenance, calculated tags (preview, publish,
versions), setpoint/command permissions, organization scope, config history and restore,
quality reason / receive time, collection errors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from hub.drivers.base import DeviceState, DeviceStatus
from tests.api.conftest import GATE_METER, PUMP_DRIVE, Backend

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

LIMIT = f"{GATE_METER}.demand_limit"
RELAY = f"{GATE_METER}.relay"
VOLT = f"{GATE_METER}.voltage_l1"

BUILDING = {
    "organizations": [{"id": "gate_kish", "name": "GATE Kish", "sites": ["kish"]},
                      {"id": "other_org", "name": "Other", "sites": ["tehran"]}],
    "locations": [
        {"id": "kish_campus", "kind": "campus", "name": "Kish", "site": "kish"},
        {"id": "gate_house", "kind": "building", "name": "Gate house", "parent": "kish_campus"},
        {"id": "gate_ground", "kind": "floor", "name": "Ground", "parent": "gate_house",
         "level": 0},
        {"id": "gate_room", "kind": "room", "name": "Electrical room", "parent": "gate_ground"},
        {"id": "pump_house", "kind": "building", "name": "Pump house", "parent": "kish_campus"},
        {"id": "pump_room", "kind": "room", "name": "Pump room", "parent": "pump_ground"},
        {"id": "pump_ground", "kind": "floor", "name": "Ground", "parent": "pump_house"},
        {"id": "tehran_campus", "kind": "campus", "name": "Tehran", "site": "tehran"},
    ],
    "systems": [{"id": "gate_power", "kind": "electrical", "name": "Power",
                 "building": "gate_house"}],
    "equipment": [
        {"id": "gate_meter", "name": "Gate meter", "type": "meter", "location": "gate_room",
         "system": "gate_power", "device": "meter_02", "serial_number": "SN-42",
         "brick_class": "Electrical_Meter",
         "points": [{"role": "voltage", "tag": VOLT}, {"role": "demand_limit", "tag": LIMIT}]},
        {"id": "pump_drive", "name": "Pump drive", "type": "vfd", "location": "pump_room",
         "device": "drive_01",
         "points": [{"role": "speed_setpoint", "tag": f"{PUMP_DRIVE}.speed_setpoint"}]},
    ],
}


async def admin(b: Backend) -> dict[str, str]:
    if await b.store.get_user_by_username("boss") is None:
        await b.user("boss", ("admin", None, None))
    return await b.headers("boss")


async def put_building(b: Backend, body: dict[str, Any] = BUILDING) -> dict[str, Any]:
    r = await b.client.put("/api/config/building", headers=await admin(b), json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def set_write_class(b: Backend, tag_name: str, write_class: str) -> None:
    h = await admin(b)
    r = await b.client.get("/api/config/devices/meter_02", headers=h)
    device = r.json()
    for tag in device["tags"]:
        if tag["name"] == tag_name:
            tag["write_class"] = write_class
    r = await b.client.put("/api/config/devices/meter_02", headers=h, json=device)
    assert r.status_code == 200, r.text


async def token(b: Backend, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {(await b.login(user, 'Correct-Horse-42x'))['access_token']}"}


async def write(b: Backend, user: str, tag: str, value: Any) -> Any:
    return await b.client.post("/api/control/write", headers=await b.headers(user),
                               json={"tag": tag, "value": value})


# --- setpoint vs command ----------------------------------------------------------------------


async def test_setpoint_operator_adjusts_setpoints_but_cannot_command(backend: Backend) -> None:
    await set_write_class(backend, "demand_limit", "setpoint")
    await backend.user("sp.gate", ("setpoint_operator", "kish", "gate"))
    await backend.user("op.gate", ("operator", "kish", "gate"))

    r = await write(backend, "sp.gate", LIMIT, 250.0)
    assert r.status_code == 200 and r.json()["result"] == "applied", r.text
    r = await write(backend, "sp.gate", RELAY, True)        # write_class unset = command
    assert r.json()["result"] == "rejected_rbac"
    assert "control.command" in r.json()["message"]
    r = await write(backend, "op.gate", RELAY, True)        # tag.write: both
    assert r.json()["result"] == "applied", r.text

    me = (await backend.client.get("/api/auth/me", headers=await backend.headers("sp.gate"))
          ).json()
    perms = me.get("permissions") or me.get("effective") or me
    assert "control.setpoint" in str(perms) and "control.command" not in str(perms)


async def test_tags_show_write_class_reason_and_receive_time(backend: Backend) -> None:
    await set_write_class(backend, "demand_limit", "setpoint")
    await backend.user("viewer", ("viewer", None, None))
    r = await backend.client.get("/api/tags", headers=await backend.headers("viewer"),
                                 params={"device_id": "meter_02"})
    by_tag = {t["tag"]: t for t in r.json()}
    assert by_tag[LIMIT]["write_class"] == "setpoint"
    assert by_tag[VOLT]["received_at"] is not None and by_tag[VOLT]["quality"] == "good"
    assert by_tag[VOLT]["reason"] is None
    r = await backend.client.get(f"/api/tags/{VOLT}/latest",
                                 headers=await backend.headers("viewer"))
    assert r.json()["received_at"] is not None


# --- building model ----------------------------------------------------------------------------


async def test_building_model_visibility(backend: Backend) -> None:
    await put_building(backend)
    await backend.user("v.gate", ("viewer", "kish", "gate"))
    await backend.user("v.all", ("viewer", None, None))
    h_gate = await backend.headers("v.gate")
    h_all = await backend.headers("v.all")

    locs = (await backend.client.get("/api/model/locations", headers=h_all)).json()
    assert [loc["id"] for loc in locs][:2] == ["kish_campus", "gate_house"]
    assert {loc["id"] for loc in locs} >= {"pump_room", "tehran_campus"}
    gate_locs = (await backend.client.get("/api/model/locations", headers=h_gate)).json()
    # an area-limited user sees only the path to its equipment
    assert {loc["id"] for loc in gate_locs} == {"kish_campus", "gate_house", "gate_ground",
                                                "gate_room"}
    assert next(x for x in gate_locs if x["id"] == "kish_campus")["equipment"] == 1

    r = await backend.client.get("/api/model/equipment/gate_meter", headers=h_gate)
    eq = r.json()
    assert eq["location_path"] == ["Kish", "Gate house", "Ground", "Electrical room"]
    points = {p["role"]: p for p in eq["points"]}
    assert points["voltage"]["value"] == pytest.approx(230.0)
    assert points["voltage"]["unit"] == "V" and points["demand_limit"]["access"] == "read_write"
    r = await backend.client.get("/api/model/equipment/pump_drive", headers=h_gate)
    assert r.status_code == 404
    r = await backend.client.get("/api/model/locations/gate_house", headers=h_gate)
    body = r.json()
    assert [c["id"] for c in body["children"]] == ["gate_ground"]
    assert [p["id"] for p in body["path"]] == ["kish_campus", "gate_house"]
    r = await backend.client.get(f"/api/model/points/{VOLT}", headers=h_gate)
    assert r.json()["role"] == "voltage" and r.json()["equipment"]["id"] == "gate_meter"
    systems = (await backend.client.get("/api/model/systems", headers=h_gate)).json()
    assert [s["id"] for s in systems] == ["gate_power"]


async def test_building_model_validation_and_permission(backend: Backend) -> None:
    await backend.user("eng", ("engineer", None, None))
    await backend.user("op.all", ("operator", None, None))
    bad = {**BUILDING, "equipment": [{**BUILDING["equipment"][0],
                                      "points": [{"role": "x", "tag": "kish.gate.meter_02.nope"}]}]}
    r = await backend.client.put("/api/config/building", headers=await backend.headers("eng"),
                                 json=bad)
    assert r.status_code == 422 and "unknown tag" in r.text
    r = await backend.client.put("/api/config/building", headers=await backend.headers("op.all"),
                                 json=BUILDING)
    assert r.status_code == 403                               # no model.edit


async def test_maintenance_records(backend: Backend) -> None:
    await put_building(backend)
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("v.gate", ("viewer", "kish", "gate"))
    entry = {"performed_at": "2026-09-01T08:00:00Z", "kind": "calibration",
             "description": "CT ratio checked", "performed_by": "Contractor", "work_order": "W1"}
    r = await backend.client.post("/api/model/equipment/gate_meter/maintenance",
                                  headers=await backend.headers("v.gate"), json=entry)
    assert r.status_code == 403
    r = await backend.client.post("/api/model/equipment/gate_meter/maintenance",
                                  headers=await backend.headers("op.gate"), json=entry)
    assert r.status_code == 201, r.text
    r = await backend.client.post("/api/model/equipment/pump_drive/maintenance",
                                  headers=await backend.headers("op.gate"), json=entry)
    assert r.status_code == 404                               # not visible to a gate operator
    rows = (await backend.client.get("/api/model/equipment/gate_meter/maintenance",
                                     headers=await backend.headers("v.gate"))).json()
    assert [(x["kind"], x["created_by_name"]) for x in rows] == [("calibration", "op.gate")]
    audit = await backend.store.list_auth_audit(event="maintenance_recorded")
    assert audit[0].detail["equipment"] == "gate_meter"


# --- organization scope ---------------------------------------------------------------------------


async def test_roles_granted_for_an_organization(backend: Backend) -> None:
    await put_building(backend)
    h = await admin(backend)
    for name, org in (("v.org", "gate_kish"), ("v.other", "other_org")):
        r = await backend.client.post("/api/users", headers=h, json={
            "username": name, "display_name": name, "password": "Correct-Horse-42x",
            "roles": [{"role": "viewer", "organization": org}]})
        assert r.status_code == 201, r.text
        user_id = r.json()["user_id"]
        await backend.store.update_user(user_id, must_change_password=False)
    r = await backend.client.get("/api/devices", headers=await token(backend, "v.org"))
    assert {d["device_id"] for d in r.json()} == {"meter_02", "drive_01"}
    r = await backend.client.get("/api/devices", headers=await token(backend, "v.other"))
    assert r.json() == []
    users = (await backend.client.get("/api/users", headers=h)).json()
    grant = next(u for u in users if u["username"] == "v.org")["roles"][0]
    assert grant["organization"] == "gate_kish" and grant["site"] is None

    r = await backend.client.post("/api/users", headers=h, json={
        "username": "bad.scope", "display_name": "x", "password": "Correct-Horse-42x",
        "roles": [{"role": "viewer", "organization": "gate_kish", "site": "kish"}]})
    assert r.status_code == 422
    r = await backend.client.post("/api/users", headers=h, json={
        "username": "bad.org", "display_name": "x", "password": "Correct-Horse-42x",
        "roles": [{"role": "viewer", "organization": "nope"}]})
    assert r.status_code == 422 and "unknown organization" in r.text


# --- calculated tags ----------------------------------------------------------------------------

CALC_DEVICE = {
    "device_id": "calc_gate", "name": "Gate calculations", "site": "kish", "area": "gate",
    "protocol": "calc",
    "tags": [{"name": "limit_margin", "unit": "kW", "data_type": "float64",
              "formula": "limit - 50[kW]", "inputs": {"limit": LIMIT}, "min_interval_s": 0.1}],
}


async def test_preview_then_publish_a_calculated_tag(backend: Backend) -> None:
    await backend.user("eng", ("engineer", None, None))
    await backend.user("v.gate", ("viewer", "kish", "gate"))
    h = await backend.headers("eng")
    r = await backend.client.post("/api/calc/preview", headers=h, json={
        "formula": "v * 2", "inputs": {"v": VOLT}, "unit": "V"})
    body = r.json()
    assert body["valid"] and body["value"] == pytest.approx(460.0)
    assert body["quality"] == "good" and body["inputs"][0]["tag"] == VOLT
    r = await backend.client.post("/api/calc/preview", headers=h, json={
        "formula": "v + 1[kWh]", "inputs": {"v": VOLT}, "unit": "V"})
    assert not r.json()["valid"] and "units do not match" in r.json()["error"]
    assert r.json()["error_position"] == 2
    # a gate viewer may not peek at the pump area through a formula
    r = await backend.client.post("/api/calc/preview", headers=await backend.headers("v.gate"),
                                  json={"formula": "f", "inputs": {"f": f"{PUMP_DRIVE}.frequency"},
                                        "unit": "Hz"})
    assert r.status_code == 403

    r = await backend.client.post("/api/config/devices", headers=h, json=CALC_DEVICE)
    assert r.status_code == 201, r.text
    path = "kish.gate.calc_gate.limit_margin"
    hub = backend.services.hub
    await backend.wait(lambda: path in hub.latest and hub.latest[path].value is not None)
    assert hub.latest[path].value == pytest.approx(150.0)        # 200 kW - 50 kW
    tags = (await backend.client.get("/api/calc/tags", headers=h)).json()
    assert [t["tag"] for t in tags] == [path] and tags[0]["value"] == pytest.approx(150.0)
    versions = (await backend.client.get(f"/api/calc/tags/{path}/versions", headers=h)).json()
    assert len(versions) == 1 and versions[0]["definition"]["formula"] == "limit - 50[kW]"

    changed = {**CALC_DEVICE, "tags": [{**CALC_DEVICE["tags"][0], "formula": "limit - 20[kW]"}]}
    r = await backend.client.put("/api/config/devices/calc_gate", headers=h, json=changed)
    assert r.status_code == 200, r.text
    await backend.wait(lambda: abs((hub.latest[path].value or 0) - 180.0) < 1e-6)
    versions = (await backend.client.get(f"/api/calc/tags/{path}/versions", headers=h)).json()
    assert [v["definition"]["formula"] for v in versions] == ["limit - 20[kW]",
                                                              "limit - 50[kW]"]


async def test_publishing_needs_calc_publish(backend: Backend) -> None:
    async with backend.store._conn() as conn:
        await conn.execute("INSERT INTO roles (role_id, description) VALUES ('cfg', 'test')")
        await conn.execute("INSERT INTO role_permissions VALUES ('cfg', 'config.edit'), "
                           "('cfg', 'data.read')")
    await backend.user("cfg.only", ("cfg", None, None))
    r = await backend.client.post("/api/config/devices", headers=await backend.headers("cfg.only"),
                                  json=CALC_DEVICE)
    assert r.status_code == 403 and "calc.publish" in r.text
    bad = {**CALC_DEVICE, "tags": [{**CALC_DEVICE["tags"][0], "formula": "limit + 1[kWh]"}]}
    r = await backend.client.post("/api/config/devices", headers=await admin(backend), json=bad)
    assert r.status_code == 422 and "units do not match" in r.text


# --- configuration history ------------------------------------------------------------------------


async def test_config_history_and_restore(backend: Backend) -> None:
    h = await admin(backend)
    await put_building(backend)
    assert backend.services.hub.report.building.equipment
    history = (await backend.client.get("/api/config/history", headers=h)).json()
    before_building = history[0]
    assert before_building["action"] == "building_updated" and before_building["user"] == "boss"
    assert "building.yaml" not in before_building["files"]

    r = await backend.client.post(f"/api/config/history/{before_building['id']}/restore",
                                  headers=h)
    assert r.status_code == 200, r.text
    assert not backend.services.hub.report.building.equipment      # back to no building model
    audit = await backend.store.list_auth_audit(event="config_changed")
    assert audit[0].detail["action"] == "config_restored"
    # the restore itself can be undone: the state before it is the newest snapshot
    history = (await backend.client.get("/api/config/history", headers=h)).json()
    assert history[0]["action"] == "config_restored" and "building.yaml" in history[0]["files"]
    r = await backend.client.post("/api/config/history/20260101-000000-000000-nope/restore",
                                  headers=h)
    assert r.status_code == 404
    r = await backend.client.post("/api/config/history/..%2F..%2Fetc/restore", headers=h)
    assert r.status_code in (404, 405)                       # never reaches the file system


# --- collection errors --------------------------------------------------------------------------


async def test_collection_errors_are_kept(backend: Backend) -> None:
    hub = backend.services.hub
    await hub._on_status(DeviceStatus("meter_02", DeviceState.OFFLINE,
                                      last_error="TimeoutError: no answer",
                                      last_error_at=datetime.now(UTC)))
    await hub.flush()
    await backend.user("v.gate", ("viewer", "kish", "gate"))
    r = await backend.client.get("/api/devices/meter_02/errors",
                                 headers=await backend.headers("v.gate"))
    assert [e["message"] for e in r.json()][:1] == ["TimeoutError: no answer"]
    r = await backend.client.get("/api/devices/drive_01/errors",
                                 headers=await backend.headers("v.gate"))
    assert r.status_code == 404


async def test_a_change_that_fails_to_apply_leaves_no_trace(backend: Backend,
                                                           monkeypatch: pytest.MonkeyPatch
                                                           ) -> None:
    services = backend.services
    real_reload = services.reload
    calls = {"n": 0}

    async def flaky(report: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("driver failed to start")
        return await real_reload(report)

    monkeypatch.setattr(services, "reload", flaky)
    config_dir = services.settings.config_dir
    before = sorted(p.name for p in (config_dir / "devices").glob("*.yaml"))
    headers = await admin(backend)
    with pytest.raises(RuntimeError):     # the test transport re-raises what would be a 500
        await backend.client.post("/api/config/devices", headers=headers, json=CALC_DEVICE)
    assert sorted(p.name for p in (config_dir / "devices").glob("*.yaml")) == before
    assert "calc_gate" not in services.hub.devices
