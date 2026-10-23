from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg import errors

from hub.drivers.base import DeviceState, DeviceStatus
from hub.models import DEVICE_ADAPTER, Quality, Sample
from hub.storage.postgres import PostgresStore
from tests.hub.builders import modbus_device, mqtt_device

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def devices(**meter_overrides: Any) -> list[Any]:
    meter = modbus_device(**meter_overrides)
    meter["tags"].append({"name": "count", "data_type": "uint32", "register_type": "input",
                          "address": 10})
    meter["tags"].append({"name": "breaker", "data_type": "bool",
                          "register_type": "discrete_input", "address": 0})
    return [DEVICE_ADAPTER.validate_python(meter),
            DEVICE_ADAPTER.validate_python(mqtt_device())]


V = "kish.substation.meter_01.voltage"
COUNT = "kish.substation.meter_01.count"
BREAKER = "kish.substation.meter_01.breaker"


def s(tag: str, value: Any, ts: datetime, quality: Quality = Quality.GOOD) -> Sample:
    return Sample(tag=tag, value=value, timestamp=ts, quality=quality)


# --- config sync ----------------------------------------------------------------------------


async def test_sync_config_creates_and_retires(store: PostgresStore) -> None:
    tags = await store.sync_config(devices())
    assert {V, COUNT, BREAKER, "kish.warehouse.sensor_01.temperature"} <= set(tags)
    assert tags[V].data_type == "float32" and tags[V].site == "kish"

    # remove a tag and the whole MQTT device
    fewer = devices()
    fewer = [fewer[0].model_copy(update={"tags": fewer[0].tags[:1]})]
    tags2 = await store.sync_config(fewer)
    assert set(tags2) == {V}
    assert tags2[V].tag_id == tags[V].tag_id  # same row, history kept

    # bring them back: rows are reused, not duplicated
    tags3 = await store.sync_config(devices())
    assert tags3[COUNT].tag_id == tags[COUNT].tag_id


# --- samples --------------------------------------------------------------------------------


async def test_write_samples_and_latest(store: PostgresStore) -> None:
    await store.sync_config(devices())
    written = await store.write_samples([
        s(V, 230.5, T0), s(V, 231.0, T0 + timedelta(seconds=1)),
        s(COUNT, 42, T0), s(BREAKER, True, T0), s(V, None, T0 - timedelta(seconds=5), Quality.BAD),
    ])
    assert written == 5
    latest = {lv.tag_path: lv for lv in await store.latest()}
    assert latest[V].value == 231.0                       # older bad sample did not win
    assert latest[COUNT].value == 42 and isinstance(latest[COUNT].value, int)
    assert latest[BREAKER].value is True
    assert [lv.tag_path for lv in await store.latest([COUNT])] == [COUNT]


async def test_duplicate_samples_are_ignored(store: PostgresStore) -> None:
    await store.sync_config(devices())
    await store.write_samples([s(V, 1.0, T0)])
    await store.write_samples([s(V, 2.0, T0)])
    history = await store.history(V, T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    assert [p.value for p in history] == [1.0]


async def test_unknown_tag_is_dropped(store: PostgresStore) -> None:
    await store.sync_config(devices())
    assert await store.write_samples([s("kish.x.y.z", 1, T0)]) == 0


async def test_history_raw_and_bucketed(store: PostgresStore) -> None:
    await store.sync_config(devices())
    await store.write_samples([s(V, float(i), T0 + timedelta(seconds=i)) for i in range(100)])
    end = T0 + timedelta(seconds=100)
    raw = await store.history(V, T0, end, max_points=1000)
    assert len(raw) == 100 and raw[0].value == 0.0
    buckets = await store.history(V, T0, end, max_points=10)
    assert len(buckets) == 10
    assert buckets[0].avg == pytest.approx(4.5) and buckets[0].min == 0 and buckets[0].max == 9
    assert sum(b.count for b in buckets) == 100
    assert await store.history("kish.x.y.z", T0, end) == []


# --- device status --------------------------------------------------------------------------


async def test_device_status(store: PostgresStore) -> None:
    await store.sync_config(devices())
    await store.upsert_device_status(DeviceStatus("meter_01", DeviceState.ONLINE, last_seen=T0))
    await store.upsert_device_status(DeviceStatus("meter_01", DeviceState.OFFLINE,
                                                  last_error="timeout", last_error_at=T0))
    status = {r.device_id: r for r in await store.device_statuses()}
    assert status["meter_01"].state == "offline"
    assert status["meter_01"].last_seen == T0          # kept when the update carries none
    assert status["sensor_01"].state == "unknown"


# --- users, roles, sessions -----------------------------------------------------------------


async def test_users_roles_and_sessions(store: PostgresStore) -> None:
    assert await store.count_users() == 0
    user = await store.create_user("op.one", "Operator One", "hash")
    assert user.must_change_password and user.language == "fa"
    with pytest.raises(errors.UniqueViolation):
        await store.create_user("op.one", "Again", "hash")
    updated = await store.update_user(user.user_id, display_name="Op 1", failed_logins=2)
    assert updated is not None and updated.display_name == "Op 1"
    with pytest.raises(ValueError, match="cannot update"):
        await store.update_user(user.user_id, username="hacker")

    grant = await store.grant_role(user.user_id, "operator", "kish", "gate", None)
    assert [g.role_id for g in await store.grants(user.user_id)] == ["operator"]
    perms = await store.role_permissions()
    assert "tag.write" in perms["operator"] and "tag.write" not in perms["viewer"]
    assert await store.revoke_role(grant.user_role_id) is not None
    assert await store.grants(user.user_id) == []

    sid = uuid.uuid4()
    await store.create_session(sid, user.user_id, "h1", T0 + timedelta(hours=8), "10.0.0.1", "ua")
    session = await store.get_session_by_hash("h1")
    assert session is not None and session.revoked_at is None
    await store.revoke_user_sessions(user.user_id)
    assert (await store.get_session(sid)).revoked_at is not None  # type: ignore[union-attr]


# --- audit, pending writes, alarms ----------------------------------------------------------


async def test_write_audit_round_trip(store: PostgresStore) -> None:
    await store.sync_config(devices())
    user = await store.create_user("op.one", "Op", "h")
    rid = uuid.uuid4()
    await store.add_write_audit(request_id=rid, phase="requested", user_id=user.user_id,
                                username="op.one", client_addr="10.0.0.5", tag_path=V,
                                requested_value=231.0, result="requested")
    await store.add_write_audit(request_id=rid, phase="outcome", user_id=user.user_id,
                                username="op.one", client_addr="10.0.0.5", tag_path=V,
                                requested_value=231.0, previous_value=230.0,
                                readback_value=231.0, result="applied")
    rows = await store.list_write_audit(request_id=rid)
    assert [r.phase for r in rows] == ["outcome", "requested"]
    assert rows[0].previous_value == "230.0" and rows[0].client_addr == "10.0.0.5"
    await store.add_auth_audit("login_ok", "op.one", user_id=user.user_id, detail={"x": 1})
    assert (await store.list_auth_audit(event="login_ok"))[0].detail == {"x": 1}


async def test_pending_writes(store: PostgresStore) -> None:
    await store.sync_config(devices())
    a = await store.create_user("op.one", "A", "h")
    b = await store.create_user("eng.two", "B", "h")
    rid = uuid.uuid4()
    await store.add_pending(request_id=rid, tag_path=V, requested_by=a.user_id, value=231.0,
                            reason="test", expires_at=T0 + timedelta(minutes=5))
    assert [p.request_id for p in await store.list_pending()] == [rid]
    decided = await store.decide_pending(rid, "confirmed", b.user_id)
    assert decided is not None and decided.status == "confirmed"
    assert await store.decide_pending(rid, "rejected", b.user_id) is None  # decided once only

    rid2 = uuid.uuid4()
    await store.add_pending(request_id=rid2, tag_path=V, requested_by=a.user_id, value=1,
                            reason=None, expires_at=T0)
    expired = await store.expire_pending(T0 + timedelta(seconds=1))
    assert [p.request_id for p in expired] == [rid2]


async def test_alarm_lifecycle(store: PostgresStore) -> None:
    await store.sync_config(devices())
    user = await store.create_user("op.one", "Op", "h")
    alarm = await store.open_alarm(source=V, tag_path=V, kind="hi", severity="high",
                                   message="Voltage high", value=260.0, raised_at=T0)
    assert alarm.tag_path == V and alarm.is_active
    assert set(await store.open_alarm_sources()) == {V}
    cleared = await store.clear_alarm(V, T0 + timedelta(minutes=1))
    assert cleared is not None and cleared.cleared_at is not None
    assert [a.alarm_id for a in await store.active_alarms()] == [alarm.alarm_id]  # not acked yet
    acked = await store.ack_alarm(alarm.alarm_id, user.user_id, "checked")
    assert acked is not None and acked.ack_comment == "checked"
    assert await store.active_alarms() == []
    assert await store.ack_alarm(alarm.alarm_id, user.user_id, None) is None
    assert len(await store.alarm_history()) == 1

    offline = await store.open_alarm(source="device:meter_01", device_id="meter_01",
                                     kind="offline", severity="high", message="offline",
                                     value=None, raised_at=T0)
    assert offline.device_id == "meter_01" and offline.tag_path is None
