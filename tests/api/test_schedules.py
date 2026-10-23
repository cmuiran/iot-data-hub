from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from hub.drivers.base import utcnow
from tests.api.conftest import GATE_METER, PUMP_DRIVE, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
LIMIT = f"{GATE_METER}.demand_limit"
VOLT = f"{GATE_METER}.voltage_l1"


def sequence(name: str = "evening", **extra: Any) -> dict[str, Any]:
    return {"name": name, "cron": "0 18 * * *", "steps": [
        {"write": {"tag": LIMIT, "value": 250.0}},
        {"delay_s": 0.2},
        {"wait": {"tag": LIMIT, "op": "ge", "value": 249.0, "timeout_s": 5}},
        {"write": {"tag": LIMIT, "value": 300.0}},
    ]} | extra


async def finished_runs(b: Backend, h: dict[str, str], sid: int, n: int = 1
                        ) -> list[dict[str, Any]]:
    for _ in range(200):
        runs = (await b.client.get(f"/api/schedules/{sid}/runs", headers=h)).json()
        done = [r for r in runs if r["status"] != "running"]
        if len(done) >= n:
            return done
        await asyncio.sleep(0.05)
    raise AssertionError("schedule run did not finish")


async def test_sequence_runs_through_the_safety_layer(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    r = await backend.client.post("/api/schedules", headers=h, json=sequence())
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["owner"] == "op.gate" and s["next_run_at"]
    assert (await backend.client.post(f"/api/schedules/{s['schedule_id']}/run",
                                      headers=h)).status_code == 202
    [run] = await finished_runs(backend, h, s["schedule_id"])
    assert run["status"] == "ok", run["detail"]
    assert [d.get("result") for d in run["detail"]] == ["applied", "ok", "ok", "applied"]
    writes = [w for w in backend.sim.unit(1).writes if w[0] == "holding" and w[1] == 100]
    assert [w[2] for w in writes] == [[2500], [3000]]
    audit = await backend.store.list_write_audit(tag_path=LIMIT)
    assert {a.reason for a in audit if a.phase == "requested"} == {"schedule evening"}
    assert {a.username for a in audit} == {"op.gate"}


async def test_failed_step_stops_the_run(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    body = {"name": "never", "cron": "0 3 * * *", "steps": [
        {"wait": {"tag": VOLT, "op": "gt", "value": 9999, "timeout_s": 0.5}},
        {"write": {"tag": LIMIT, "value": 250.0}}]}
    sid = (await backend.client.post("/api/schedules", headers=h, json=body)).json()[
        "schedule_id"]
    await backend.client.post(f"/api/schedules/{sid}/run", headers=h)
    [run] = await finished_runs(backend, h, sid)
    assert run["status"] == "failed" and len(run["detail"]) == 1
    assert backend.sim.unit(1).writes == []


async def test_interlock_rejection_fails_the_run(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    backend.meter.set("breaker_closed", False)
    await backend.wait(lambda: backend.services.hub.latest[
        f"{GATE_METER}.breaker_closed"].value is False)
    sid = (await backend.client.post("/api/schedules", headers=h, json=sequence())).json()[
        "schedule_id"]
    await backend.client.post(f"/api/schedules/{sid}/run", headers=h)
    [run] = await finished_runs(backend, h, sid)
    assert run["status"] == "failed" and run["detail"][0]["result"] == "rejected_interlock"


async def test_timer_triggers_when_due(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    body = {"name": "every_minute", "cron": "* * * * *",
            "steps": [{"write": {"tag": LIMIT, "value": 210.0}}]}
    sid = (await backend.client.post("/api/schedules", headers=h, json=body)).json()[
        "schedule_id"]
    scheduler = backend.services.scheduler
    later = utcnow() + timedelta(minutes=2)
    scheduler.clock = lambda: later
    await scheduler.tick()
    [run] = await finished_runs(backend, h, sid)
    assert run["trigger"] == "timer" and run["status"] == "ok"
    s = (await backend.client.get(f"/api/schedules/{sid}", headers=h)).json()
    assert s["last_run_at"] is not None


async def test_validation_and_permissions(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("op.two", ("operator", "kish", "gate"))
    await backend.user("eng.one", ("engineer", None, None))
    op = await backend.headers("op.gate")
    eng = await backend.headers("eng.one")
    bad_cron = await backend.client.post("/api/schedules", headers=op,
                                         json=sequence(cron="0 25 * * *"))
    assert bad_cron.status_code == 422
    other_area = await backend.client.post("/api/schedules", headers=op, json={
        "name": "x", "cron": "0 1 * * *",
        "steps": [{"write": {"tag": f"{PUMP_DRIVE}.speed_setpoint", "value": 10}}]})
    assert other_area.status_code == 403
    needs_confirm = await backend.client.post("/api/schedules", headers=eng, json={
        "name": "x", "cron": "0 1 * * *",
        "steps": [{"write": {"tag": f"{PUMP_DRIVE}.speed_setpoint", "value": 10}}]})
    assert needs_confirm.status_code == 422 and "second person" in needs_confirm.json()["message"]
    read_only = await backend.client.post("/api/schedules", headers=op, json={
        "name": "x", "cron": "0 1 * * *", "steps": [{"write": {"tag": VOLT, "value": 1.0}}]})
    assert read_only.status_code == 422

    sid = (await backend.client.post("/api/schedules", headers=op, json=sequence())).json()[
        "schedule_id"]
    two = await backend.headers("op.two")
    assert (await backend.client.delete(f"/api/schedules/{sid}", headers=two)).status_code == 403
    assert (await backend.client.delete(f"/api/schedules/{sid}", headers=eng)).status_code == 204
    events = [a.event for a in await backend.store.list_auth_audit()]
    assert "schedule_created" in events and "schedule_deleted" in events


async def test_disabled_owner_makes_runs_fail(backend: Backend) -> None:
    uid = await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    sid = (await backend.client.post("/api/schedules", headers=h, json=sequence())).json()[
        "schedule_id"]
    await backend.store.update_user(uid, is_active=False)
    s = await backend.store.get_schedule(sid)
    result = await backend.services.scheduler.run(s, "manual")
    assert result["status"] == "failed" and "owner" in result["detail"][0]["message"]
    assert backend.sim.unit(1).writes == []
