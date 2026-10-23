from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from hub.drivers.base import utcnow
from tests.api.conftest import GATE_METER, PUMP_DRIVE, Backend, make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.integration]

LIMIT = f"{GATE_METER}.demand_limit"
RELAY = f"{GATE_METER}.relay"
VOLT = f"{GATE_METER}.voltage_l1"
SPEED = f"{PUMP_DRIVE}.speed_setpoint"


async def write(b: Backend, headers: dict[str, str], tag: str, value: Any) -> dict[str, Any]:
    r = await b.client.post("/api/control/write", headers=headers,
                            json={"tag": tag, "value": value, "reason": "test"})
    assert r.status_code == 200, r.text
    return r.json()


async def outcomes(b: Backend, request_id: str) -> list[tuple[str, str]]:
    rows = await b.store.list_write_audit(request_id=request_id)  # type: ignore[arg-type]
    return sorted((r.phase, r.result) for r in rows)


# --- read access and scopes -----------------------------------------------------------------


async def test_device_and_tag_visibility_follows_scope(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("boss", ("viewer", None, None))
    gate = await backend.headers("op.gate")
    boss = await backend.headers("boss")

    devices = (await backend.client.get("/api/devices", headers=gate)).json()
    assert [d["device_id"] for d in devices] == ["meter_02"]
    assert devices[0]["state"] == "online"
    all_devices = (await backend.client.get("/api/devices", headers=boss)).json()
    assert {d["device_id"] for d in all_devices} == {"meter_02", "drive_01"}

    assert (await backend.client.get("/api/devices/drive_01", headers=gate)).status_code == 404
    detail = (await backend.client.get("/api/devices/meter_02", headers=gate)).json()
    tags = {t["name"]: t for t in detail["tags"]}
    assert tags["voltage_l1"]["value"] == pytest.approx(230.0)
    assert tags["demand_limit"]["access"] == "read_write"

    paths = [t["tag"] for t in (await backend.client.get("/api/tags", headers=gate)).json()]
    assert paths and all(p.startswith("kish.gate.") for p in paths)
    r = await backend.client.get(f"/api/tags/{SPEED}/latest", headers=gate)
    assert r.status_code == 404
    latest = (await backend.client.get(f"/api/tags/{VOLT}/latest", headers=gate)).json()
    assert latest["quality"] == "good"


async def test_history_and_csv(backend: Backend) -> None:
    await backend.user("viewer", ("viewer", None, None))
    h = await backend.headers("viewer")
    await backend.wait(lambda: backend.services.hub.stats.samples_written >= 2)
    start = (utcnow() - timedelta(minutes=5)).isoformat()
    r = await backend.client.get(f"/api/tags/{VOLT}/history", headers=h,
                                 params={"start": start, "max_points": 10})
    assert r.status_code == 200 and r.json()
    csv = await backend.client.get(f"/api/tags/{VOLT}/history.csv", headers=h,
                                   params={"start": start})
    assert csv.status_code == 200 and csv.text.startswith("timestamp_utc,value")
    assert "attachment" in csv.headers["content-disposition"]
    bad = await backend.client.get(f"/api/tags/{VOLT}/history", headers=h,
                                   params={"start": "2026-01-01T00:00:00"})
    assert bad.status_code == 422 and bad.json()["error"] == "naive_datetime"


async def test_alarm_list_and_ack(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("view.pump", ("viewer", "kish", "pump_station"))
    backend.meter.set("voltage_l1", 260.0)
    await backend.wait(lambda: backend.services.hub.latest[VOLT].value > 250)
    await asyncio.sleep(0.3)
    op = await backend.headers("op.gate")
    alarms = (await backend.client.get("/api/alarms", headers=op)).json()
    assert [(a["source"], a["kind"]) for a in alarms] == [(VOLT, "hi")]
    other = (await backend.client.get("/api/alarms",
                                      headers=await backend.headers("view.pump"))).json()
    assert other == []
    r = await backend.client.post(f"/api/alarms/{alarms[0]['alarm_id']}/ack", headers=op,
                                  json={"comment": "seen"})
    assert r.status_code == 200 and r.json()["ack_comment"] == "seen"


# --- control: the safety gates --------------------------------------------------------------


async def test_write_applied_with_readback_and_audit(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    result = await write(backend, await backend.headers("op.gate"), LIMIT, 250.0)
    assert result["status"] == "applied", result
    assert result["previous_value"] == pytest.approx(200.0)
    assert result["readback_value"] == pytest.approx(250.0)
    assert ("holding", 100, [2500]) in backend.sim.unit(1).writes
    assert await outcomes(backend, result["request_id"]) == [("outcome", "applied"),
                                                             ("requested", "requested")]


async def test_bool_coil_write(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    result = await write(backend, await backend.headers("op.gate"), RELAY, True)
    assert result["status"] == "applied" and backend.meter.get_raw_bit("relay") is True
    again = await write(backend, await backend.headers("op.gate"), RELAY, False)
    assert again["result"] == "rejected_rate"          # relay: min_interval_s 30


@pytest.mark.parametrize(
    "tag,value,result",
    [(LIMIT, 600, "rejected_range"),            # above max
     (LIMIT, -1, "rejected_range"),             # below min
     (LIMIT, "250", "invalid"),                 # not a number (request validation)
     (LIMIT, True, "rejected_range"),           # bool for a numeric tag
     (LIMIT, 350, "rejected_range"),            # max_step 100 from current 200
     (RELAY, 1, "rejected_range"),              # number for a bool tag
     (VOLT, 1, "rejected_disabled")],           # read-only tag
)
async def test_rejected_writes_are_audited(backend: Backend, tag: str, value: Any,
                                           result: str) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    r = await backend.client.post("/api/control/write", headers=await backend.headers("op.gate"),
                                  json={"tag": tag, "value": value})
    if result == "invalid":
        assert r.status_code == 422
        return
    body = r.json()
    assert body["status"] == "rejected" and body["result"] == result, body
    assert await outcomes(backend, body["request_id"]) == [("outcome", result),
                                                           ("requested", "requested")]
    assert backend.sim.unit(1).writes == []


async def test_rbac_scope_for_writes(backend: Backend) -> None:
    await backend.user("op.pump", ("operator", "kish", "pump_station"))
    await backend.user("viewer", ("viewer", None, None))
    body = await write(backend, await backend.headers("op.pump"), LIMIT, 250.0)
    assert body["result"] == "rejected_rbac"
    r = await backend.client.post("/api/control/write", headers=await backend.headers("viewer"),
                                  json={"tag": LIMIT, "value": 250.0})
    assert r.status_code == 403                       # no tag.write anywhere: not even audited


async def test_interlock_blocks_until_condition_holds(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    backend.meter.set("breaker_closed", False)
    breaker = f"{GATE_METER}.breaker_closed"
    await backend.wait(lambda: backend.services.hub.latest[breaker].value is False)
    body = await write(backend, h, LIMIT, 250.0)
    assert body["result"] == "rejected_interlock" and "breaker" in body["message"]
    backend.meter.set("breaker_closed", True)
    await backend.wait(lambda: backend.services.hub.latest[breaker].value is True)
    assert (await write(backend, h, LIMIT, 250.0))["status"] == "applied"


async def test_interlock_on_stale_value(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    later = utcnow() + timedelta(seconds=60)            # interlock max_age_s is 10
    backend.services.control.clock = lambda: later
    body = await write(backend, await backend.headers("op.gate"), LIMIT, 250.0)
    assert body["result"] == "rejected_interlock" and "stale" in body["message"]


async def test_offline_device_blocks_writes(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    backend.sim.unit(1).offline = True
    breaker = f"{GATE_METER}.breaker_closed"
    await backend.wait(lambda: backend.services.hub.latest[breaker].quality != "good",
                       timeout=15)
    body = await write(backend, await backend.headers("op.gate"), LIMIT, 250.0)
    assert body["status"] == "rejected"   # max_step / interlock: no good current value
    assert backend.sim.unit(1).writes == []


async def test_device_failure_during_write(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    backend.sim.unit(1).exception_addresses.add(("holding", 100))
    body = await write(backend, h, LIMIT, 250.0)
    assert body["status"] in ("failed", "rejected"), body
    assert body["result"] in ("failed", "rejected_range")   # rejected if the poll saw it first
    assert await outcomes(backend, body["request_id"]) == [("outcome", body["result"]),
                                                           ("requested", "requested")]


async def test_control_disabled_globally(backend: Backend) -> None:
    backend.services.settings = backend.services.control.settings = make_settings(
        control_enabled=False)
    await backend.user("op.gate", ("operator", "kish", "gate"))
    body = await write(backend, await backend.headers("op.gate"), LIMIT, 250.0)
    assert body["result"] == "rejected_disabled" and "HUB_CONTROL_ENABLED" in body["message"]


# --- control: two-person confirmation -------------------------------------------------------


async def test_confirmation_flow(backend: Backend) -> None:
    await backend.user("op.pump", ("operator", "kish", "pump_station"))
    await backend.user("eng.one", ("engineer", None, None))
    op = await backend.headers("op.pump")
    eng = await backend.headers("eng.one")

    pending = await write(backend, op, SPEED, 1500)
    assert pending["status"] == "pending_confirmation" and backend.sim.unit(5).writes == []
    rid = pending["request_id"]

    listed = (await backend.client.get("/api/control/pending", headers=eng)).json()
    assert [p["request_id"] for p in listed] == [rid]
    self_confirm = await backend.client.post(f"/api/control/pending/{rid}/confirm", headers=op)
    assert self_confirm.status_code == 403 and self_confirm.json()["error"] == "self_confirmation"

    r = await backend.client.post(f"/api/control/pending/{rid}/confirm", headers=eng)
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    assert ("holding", 10, [1500]) in backend.sim.unit(5).writes
    rows = await backend.store.list_write_audit(request_id=rid)  # type: ignore[arg-type]
    outcome = next(row for row in rows if row.phase == "outcome")
    assert outcome.result == "applied" and outcome.username == "op.pump"
    assert outcome.confirmed_by == (await backend.store.get_user_by_username("eng.one")).user_id  # type: ignore[union-attr]

    again = await backend.client.post(f"/api/control/pending/{rid}/confirm", headers=eng)
    assert again.status_code == 409


async def test_reject_and_expiry(backend: Backend) -> None:
    await backend.user("op.pump", ("operator", "kish", "pump_station"))
    await backend.user("eng.one", ("engineer", None, None))
    op = await backend.headers("op.pump")
    eng = await backend.headers("eng.one")

    rid = (await write(backend, op, SPEED, 1200))["request_id"]
    r = await backend.client.post(f"/api/control/pending/{rid}/reject", headers=eng,
                                  json={"reason": "not now"})
    assert r.status_code == 200 and r.json()["result"] == "rejected_unconfirmed"
    assert await outcomes(backend, rid) == [("outcome", "rejected_unconfirmed"),
                                            ("requested", "requested")]

    rid2 = (await write(backend, op, SPEED, 1300))["request_id"]
    later = utcnow() + timedelta(minutes=10)
    backend.services.control.clock = lambda: later
    r = await backend.client.post(f"/api/control/pending/{rid2}/confirm", headers=eng)
    assert r.status_code == 409 and r.json()["error"] == "expired"
    assert await outcomes(backend, rid2) == [("outcome", "rejected_unconfirmed"),
                                             ("requested", "requested")]
    assert backend.sim.unit(5).writes == []


# --- audit / system -------------------------------------------------------------------------


async def test_audit_and_system_endpoints(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("eng.one", ("engineer", None, None))
    await write(backend, await backend.headers("op.gate"), LIMIT, 250.0)
    eng = await backend.headers("eng.one")
    rows = (await backend.client.get("/api/audit/writes", headers=eng,
                                     params={"tag": LIMIT})).json()
    assert {r["result"] for r in rows} == {"requested", "applied"}
    assert (await backend.client.get("/api/audit/writes",
                                     headers=await backend.headers("op.gate"))).status_code == 403
    stats = (await backend.client.get("/api/system/stats", headers=eng)).json()
    assert stats["devices"] == {"drive_01": "online", "meter_02": "online"}
    health = (await backend.client.get("/api/health")).json()
    assert health["status"] == "ok" and health["devices"]["online"] == 2
    r = await backend.client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_unknown_routes_and_bad_bodies(backend: Backend) -> None:
    r = await backend.client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await backend.client.post("/api/auth/login", json={"username": ""})
    assert r.status_code == 422 and r.json()["error"] == "invalid"

