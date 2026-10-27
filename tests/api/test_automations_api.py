"""Platform phase 6 end to end: scenes and automations of a home, with the simulated native
device. Time triggers on the Jalali calendar run once per occurrence (also across two engine
instances), missed ones are skipped or caught up, value triggers fire on the edge, a person's
recent change is respected, every write goes through the control layer as the creator;
validation of devices, tags and values; isolation between homes; holidays and the calendar."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from backend.automations import AutomationEngine, AutomationIn, next_runs
from backend.settings import Settings
from simulators.native_device import NativeDeviceSim
from tests.api.conftest import Backend, make_settings
from tests.api.test_accounts_api import personal_org
from tests.api.test_ingest_api import owned_device, running

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token="h" * 32)


def write(device_id: str, tag: str, value: Any) -> dict[str, Any]:
    return {"type": "write", "device_id": device_id, "tag": tag, "value": value}


async def setup(b: Backend) -> tuple[dict[str, str], dict[str, str], str, str, NativeDeviceSim]:
    _, sara, omid, device_id, secret = await owned_device(b)
    org = await personal_org(b, sara)
    sim = NativeDeviceSim(device_id, secret, {f"relay_{i}": False for i in range(1, 9)}
                          | {"power": 1.0})
    return sara, omid, org, device_id, sim


async def create(b: Backend, h: dict[str, str], org: str, body: dict[str, Any]) -> Any:
    return await b.client.post(f"/api/platform/organizations/{org}/automations", headers=h,
                               json=body)


async def test_time_trigger_once_per_occurrence(backend: Backend) -> None:
    b = backend
    sara, _, org, device_id, sim = await setup(b)
    r = await create(b, sara, org, {"name": "Office hours", "trigger": {
        "type": "time", "at": "08:00", "weekdays": [0, 1, 2, 3, 4]},
        "actions": [write(device_id, "relay_1", True)], "respect_manual_min": 0})
    assert r.status_code == 201, r.text
    auto = r.json()
    assert len(auto["next_runs"]) == 3                    # the next Sat-Wed 08:00 (Tehran)
    record = await b.store.automations.automation(None, auto["automation_id"])
    spec = AutomationIn.from_record(record)
    eng = b.services.automations
    due = next_runs(spec.trigger, record.updated_at, eng.tz, set(), 1)[0]
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        await b.wait(lambda: f"platform.devices.{device_id}.relay_1" in b.services.hub.latest)
        one = AutomationEngine(b.services, clock=lambda: due + timedelta(seconds=10))
        two = AutomationEngine(b.services, clock=lambda: due + timedelta(seconds=12))
        await asyncio.gather(one.tick(), two.tick())      # two hub nodes, same minute
        await asyncio.gather(*one._running.values(), *two._running.values())
        stop.set()
    runs = (await b.client.get(f"/api/platform/organizations/{org}/automations/"
                               f"{auto['automation_id']}/runs", headers=sara)).json()
    assert [r["status"] for r in runs] == ["ok"], runs
    assert runs[0]["trigger_key"] == f"time:{due.isoformat()}"
    assert runs[0]["detail"][0]["result"] == "applied"
    assert sim.values["relay_1"] is True
    audit = await b.store.list_write_audit(tag_path=f"platform.devices.{device_id}.relay_1")
    assert any((a.reason or "").startswith("automation:") for a in audit)


async def test_missed_runs_skipped_or_caught_up(backend: Backend) -> None:
    b = backend
    sara, _, org, device_id, _ = await setup(b)
    base = {"trigger": {"type": "time", "at": "07:00"},
            "actions": [{"type": "notify", "text": "good morning"}]}
    skip = (await create(b, sara, org, base | {"name": "skip"})).json()
    catch = (await create(b, sara, org, base | {"name": "catch", "missed": "catch_up",
                                                 "catch_up_min": 180})).json()
    rec = await b.store.automations.automation(None, skip["automation_id"])
    eng = b.services.automations
    due = next_runs(AutomationIn.from_record(rec).trigger, rec.updated_at, eng.tz, set(), 1)[0]
    late = AutomationEngine(b.services, clock=lambda: due + timedelta(hours=2))
    await late.tick()
    await asyncio.gather(*late._running.values())
    for auto, status in ((skip, "skipped"), (catch, "ok")):
        runs = await b.store.automations.runs(None, auto["automation_id"])
        assert [r.status for r in runs] == [status], (auto["name"], runs)
    await late.tick()                                     # handled once
    assert len(await b.store.automations.runs(None, skip["automation_id"])) == 1


async def test_value_trigger_conditions_and_manual_override(backend: Backend) -> None:
    b = backend
    sara, _, org, device_id, sim = await setup(b)
    r = await create(b, sara, org, {
        "name": "Peak power", "trigger": {"type": "value", "device_id": device_id,
                                          "tag": "power", "op": "gt", "value": 5},
        "actions": [write(device_id, "relay_2", False), write(device_id, "relay_3", True)]})
    auto = r.json()
    eng = AutomationEngine(b.services)
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        path = f"platform.devices.{device_id}.power"
        await b.wait(lambda: path in b.services.hub.latest)
        await eng.tick()                                   # first look: not above 5
        # a person switches relay_2 on: the automation leaves it alone for an hour
        r = await b.client.post("/api/control/write", headers=sara, json={
            "tag": f"platform.devices.{device_id}.relay_2", "value": True, "reason": "me"})
        assert r.json()["result"] == "applied", r.text
        sim.values["power"] = 7.5
        await b.client.post(f"/ingest/v1/devices/{device_id}/telemetry", json=sim.telemetry(),
                            headers={"Authorization": f"Bearer {sim.secret}"})
        await b.wait(lambda: b.services.hub.latest[path].value == 7.5)
        await eng.tick()
        await asyncio.gather(*eng._running.values())
        await eng.tick()                                   # still above: no second run
        stop.set()
    runs = await b.store.automations.runs(None, auto["automation_id"])
    assert len(runs) == 1 and runs[0].status == "partial", runs
    results = {d["tag"].rsplit(".", 1)[1]: d["result"] for d in runs[0].detail}
    assert results == {"relay_2": "skipped_manual", "relay_3": "applied"}
    assert sim.values["relay_2"] is True and sim.values["relay_3"] is True


async def test_scenes_dry_run_and_validation(backend: Backend) -> None:
    b = backend
    sara, omid, org, device_id, sim = await setup(b)
    base = f"/api/platform/organizations/{org}"
    r = await b.client.post(f"{base}/scenes", headers=sara, json={
        "name": "Night", "icon": "moon", "actions": [
            write(device_id, "relay_1", False), {"type": "delay", "seconds": 0.1},
            write(device_id, "relay_4", True)]})
    assert r.status_code == 201, r.text
    scene = r.json()
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        await b.wait(lambda: f"platform.devices.{device_id}.relay_1" in b.services.hub.latest)
        r = await b.client.post(f"{base}/scenes/{scene['scene_id']}/run", headers=sara)
        assert r.status_code == 200, r.text
        stop.set()
    assert [d.get("result") for d in r.json()["detail"]] == ["applied", "ok", "applied"]
    assert sim.values["relay_4"] is True

    r = await create(b, sara, org, {"name": "Evening", "trigger": {"type": "time",
                                                                   "at": "19:00"},
                                    "conditions": [{"device_id": device_id, "tag": "power",
                                                    "op": "lt", "value": 100}],
                                    "actions": [{"type": "scene",
                                                 "scene_id": scene["scene_id"]}]})
    auto = r.json()
    r = await b.client.post(f"{base}/automations/{auto['automation_id']}/test", headers=sara)
    assert r.status_code == 200, r.text
    assert [d["result"] for d in r.json()["detail"]] == ["would_write", "ok", "would_write"]
    assert await b.store.automations.runs(None, auto["automation_id"]) == []   # nothing stored

    for bad, code in (
            ({"actions": [write(device_id, "power", 1)]}, "read_only_tag"),
            ({"actions": [write(device_id, "relay_1", 1)]}, "invalid_value"),
            ({"actions": [write(device_id, "nope", True)]}, "unknown_tag"),
            ({"actions": [write("dxxxxxxxxxxxxxxxxxxxx", "relay_1", True)]}, "unknown_device"),
            ({"actions": [{"type": "scene", "scene_id": 99999}]}, "unknown_scene")):
        r = await create(b, sara, org, {"name": "bad", "trigger": {"type": "time",
                                                                   "at": "10:00"}} | bad)
        assert r.status_code == 422 and r.json()["error"] == code, (code, r.text)
    # another home sees nothing and cannot change anything here
    for method, url in (("GET", f"{base}/automations"), ("GET", f"{base}/scenes"),
                        ("POST", f"{base}/scenes/{scene['scene_id']}/run"),
                        ("DELETE", f"{base}/automations/{auto['automation_id']}")):
        r = await b.client.request(method, url, headers=omid)
        assert r.status_code in (403, 404), (method, url, r.status_code)
    own = await personal_org(b, omid)
    r = await create(b, omid, own, {"name": "steal", "trigger": {"type": "time",
                                                                 "at": "10:00"},
                                    "actions": [write(device_id, "relay_1", True)]})
    assert r.status_code == 422 and r.json()["error"] == "unknown_device"


async def test_holidays_and_calendar(backend: Backend) -> None:
    b = backend
    sara, _, org, _, _ = await setup(b)
    base = f"/api/platform/organizations/{org}"
    r = await b.client.post(f"{base}/holidays", headers=sara,
                            json={"date": "1405/02/10", "name": "Family day"})
    assert r.status_code == 201, r.text
    r = await b.client.post(f"{base}/holidays", headers=sara,
                            json={"date": "1404/12/30", "name": "no such day"})
    assert r.status_code == 422
    days = (await b.client.get(f"{base}/holidays", headers=sara,
                               params={"year": 1405})).json()
    by_date = {d["date"]: d for d in days}
    assert by_date["1405/01/01"]["source"] == "fixed"
    assert by_date["1405/02/10"] == by_date["1405/02/10"] | {"source": "organization",
                                                            "name": "Family day"}
    r = await b.client.get("/api/platform/calendar", headers=sara,
                           params={"day": "1405/01/13"})
    assert r.json() | {} == r.json() and r.json()["holiday"] == "روز طبیعت"
    assert r.json()["gregorian"] == "2026-04-02" and r.json()["month_name"] == "فروردین"
