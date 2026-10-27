"""Platform phase 4 end to end: a product with the native binding, manufactured, labelled,
claimed and switched on by its owner; the simulated device (simulators.native_device) connects
over HTTP, TCP and MQTT with its credential; commands from the owner go through the control-
safety layer and their life is followed: sent, delivered, executed, confirmed, or failed /
expired. Also: who may operate a platform device, device authentication, broker hooks, and
the command life of a polled (Modbus) write."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from backend.mqtt_ingest import MqttIngest
from backend.settings import Settings
from hub.models import Broker
from simulators.mqtt_sim import EmbeddedBroker
from simulators.native_device import NativeDeviceSim
from tests.api.conftest import GATE_METER, Backend, make_settings
from tests.api.test_accounts_api import account
from tests.api.test_claims_api import claim, labels, register
from tests.api.test_platform_api import PRODUCT, _product, _setup
from tests.platform.test_template import relay_template

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
HOOK = "h" * 32


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token=HOOK)


def native_template(**connection: Any) -> dict[str, Any]:
    t = relay_template()
    t["bindings"] = {"native": {"connection": {"command_timeout_s": 3} | connection,
                                "tags": {tag["key"]: {} for tag in t["tags"]}}}
    return t


async def owned_device(b: Backend, transports: list[str] | None = None
                       ) -> tuple[dict[str, str], dict[str, str], dict[str, str], str, str]:
    """(maker headers…, sara, omid, device_id, secret): claimed by sara and switched on."""
    h = await _setup(b)
    pid = await _product(b, h["alice"], native_template(), body=PRODUCT | {
        "code": "NAT-8R", "identifier_pattern": "NAT-{000}"})
    await register(b, h["alice"], pid, 2, protocol="native")
    rows = await labels(b, h["alice"], pid, credentials=True)
    sara = await account(b, "sara@example.com", "Sara")
    omid = await account(b, "omid@example.com", "Omid")
    r = await claim(b, sara, rows[0]["identifier"], rows[0]["claim_code"])
    assert r.status_code == 200, r.text
    device_id = rows[0]["device_id"]
    patch: dict[str, Any] = {"enabled": True}
    if transports:
        patch["connection"] = {"transports": transports}
    r = await b.client.patch(f"/api/platform/my/devices/{device_id}", headers=sara, json=patch)
    assert r.status_code == 200, r.text
    b.second = rows[1]                                     # type: ignore[attr-defined]
    return h, sara, omid, device_id, rows[0]["device_secret"]


async def write(b: Backend, h: dict[str, str], tag: str, value: Any) -> dict[str, Any]:
    r = await b.client.post("/api/control/write", headers=h,
                            json={"tag": tag, "value": value, "reason": "test"})
    assert r.status_code == 200, r.text
    return r.json()


async def commands(b: Backend, h: dict[str, str], device_id: str) -> list[dict[str, Any]]:
    r = await b.client.get(f"/api/platform/my/devices/{device_id}/commands", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


@contextlib.asynccontextmanager
async def running(coro: Any) -> Any:
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_http_device_and_command_life(backend: Backend) -> None:
    b = backend
    h, sara, omid, device_id, secret = await owned_device(b)
    hub = b.services.hub
    relay = f"platform.devices.{device_id}.relay_1"
    sim = NativeDeviceSim(device_id, secret, {"relay_1": False, "power": 1.5})
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        await b.wait(lambda: relay in hub.latest)
        assert hub.latest[relay].value is False
        assert hub.latest[f"platform.devices.{device_id}.power"].value == pytest.approx(1.5)

        # who may operate it: the owner; not another customer, not the platform admin
        r = await b.client.post("/api/control/write", headers=omid,
                                json={"tag": relay, "value": True, "reason": "test"})
        assert r.status_code == 404              # not even its existence is told
        assert (await write(b, h["boss"], relay, True))["result"] == "rejected_rbac"
        result = await write(b, sara, relay, True)
        assert result["result"] == "applied", result
        assert sim.values["relay_1"] is True and hub.latest[relay].value is True
        (cmd,) = await commands(b, sara, device_id)
        assert cmd["status"] == "confirmed" and cmd["transport"] == "http"
        assert cmd["command_id"] == str(result["request_id"])
        assert cmd["sent_at"] <= cmd["delivered_at"] <= cmd["executed_at"] <= cmd["finished_at"]

        sim.refuse = True                                   # the device refuses
        result = await write(b, sara, relay, False)
        assert result["result"] == "readback_mismatch" and "refused" in result["message"]
        assert (await commands(b, sara, device_id))[0]["status"] == "failed"

        sim.refuse, sim.silent = False, True                 # the device never answers
        result = await write(b, sara, relay, False)
        assert result["result"] == "timeout", result
        assert (await commands(b, sara, device_id))[0]["status"] == "expired"
        stop.set()

    # a viewer share cannot operate it; an operator share can
    r = await b.client.post(f"/api/platform/devices/{device_id}/shares", headers=sara,
                            json={"email": "omid@example.com", "level": "viewer"})
    assert r.status_code == 201
    from tests.api.test_accounts_api import last_link
    r = await b.client.post("/api/account/invitations/accept", headers=omid,
                            json={"token": last_link(b, "omid@example.com", "invitation")})
    assert r.status_code == 200
    assert (await write(b, omid, relay, True))["result"] == "rejected_rbac"
    assert len(await commands(b, omid, device_id)) == 3        # viewers see the history


async def test_device_authentication(backend: Backend) -> None:
    b = backend
    _, sara, _, device_id, secret = await owned_device(b)
    base = f"/ingest/v1/devices/{device_id}"
    body = {"values": {"relay_1": True}}
    for headers in ({}, {"Authorization": "Bearer wrong"},
                    {"Authorization": f"Basic {secret}"}):
        r = await b.client.post(f"{base}/telemetry", headers=headers, json=body)
        assert r.status_code == 401, r.text
    other = b.second["device_id"]                            # type: ignore[attr-defined]
    r = await b.client.post(f"/ingest/v1/devices/{other}/telemetry",
                            headers={"Authorization": f"Bearer {secret}"}, json=body)
    assert r.status_code == 401                                # someone else's credential
    auth = {"Authorization": f"Bearer {secret}"}
    r = await b.client.post(f"{base}/telemetry", headers=auth, json={"relay_1": True})
    assert r.status_code == 422 and r.json()["error"] == "invalid_message"
    r = await b.client.post(f"{base}/telemetry", headers=auth,
                            json={"ts": 1793000000000, "values": {"relay_1": "yes",
                                                                  "unknown": 1}})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert b.services.hub.latest[f"platform.devices.{device_id}.relay_1"].quality == "bad"
    events = [a for a in await b.store.list_auth_audit() if a.event == "device_auth_failed"]
    assert len(events) == 2          # wrong secret, other device (no header: not audited)

    # switched off by the owner: the credential no longer gets in
    await b.client.patch(f"/api/platform/my/devices/{device_id}", headers=sara,
                         json={"enabled": False})
    r = await b.client.post(f"{base}/telemetry", headers=auth, json=body)
    assert r.status_code == 401


async def test_tcp_device(backend: Backend) -> None:
    b = backend
    _, sara, _, device_id, secret = await owned_device(b)
    port = b.services.tcp_ingest.port
    bad = await NativeDeviceSim(device_id, "wrong").run_tcp("127.0.0.1", port)
    assert bad["ok"] is False
    sim = NativeDeviceSim(device_id, secret, {"relay_2": False})
    ready = asyncio.Event()
    relay = f"platform.devices.{device_id}.relay_2"
    async with running(sim.run_tcp("127.0.0.1", port, ready=ready)):
        await asyncio.wait_for(ready.wait(), 5)
        await b.wait(lambda: relay in b.services.hub.latest)
        result = await write(b, sara, relay, True)
        assert result["result"] == "applied", result
    cmd = (await commands(b, sara, device_id))[0]
    assert cmd["status"] == "confirmed" and cmd["transport"] == "tcp"


async def test_mqtt_device(backend: Backend) -> None:
    b = backend
    _, sara, _, device_id, secret = await owned_device(b, transports=["mqtt"])
    async with EmbeddedBroker() as broker:
        mqtt = MqttIngest(b.services.ingest, Broker(broker_id="platform", host=broker.host,
                                                    port=broker.port))
        b.services.ingest.mqtt = mqtt
        await mqtt.start()
        try:
            await b.wait(lambda: mqtt.connected)
            await asyncio.sleep(0.2)
            sim = NativeDeviceSim(device_id, secret, {"relay_3": False})
            ready = asyncio.Event()
            relay = f"platform.devices.{device_id}.relay_3"
            async with running(sim.run_mqtt(broker.host, broker.port, ready=ready)):
                await asyncio.wait_for(ready.wait(), 5)
                await b.wait(lambda: relay in b.services.hub.latest)
                await asyncio.sleep(0.2)
                result = await write(b, sara, relay, True)
                assert result["result"] == "applied", result
        finally:
            await mqtt.stop()
            b.services.ingest.mqtt = None
    cmd = (await commands(b, sara, device_id))[0]
    assert cmd["status"] == "confirmed" and cmd["transport"] == "mqtt"


async def test_broker_hooks(backend: Backend) -> None:
    b = backend
    _, _, _, device_id, secret = await owned_device(b, transports=["mqtt", "http"])
    hook = {"X-Hub-Hook-Token": HOOK}
    r = await b.client.post("/ingest/v1/broker/auth", json={"username": device_id,
                                                            "password": secret})
    assert r.status_code == 403                                 # no hook token
    for password, expected in ((secret, "allow"), ("wrong", "deny")):
        r = await b.client.post("/ingest/v1/broker/auth", headers=hook,
                                json={"username": device_id, "password": password})
        assert r.json() == {"result": expected}
    other = b.second["device_id"]                            # type: ignore[attr-defined]
    for topic, action, expected in (
            (f"d/{device_id}/telemetry", "publish", "allow"),
            (f"d/{device_id}/ack", "publish", "allow"),
            (f"d/{device_id}/cmd", "subscribe", "allow"),
            (f"d/{device_id}/cmd", "publish", "deny"),           # nobody else's commands
            (f"d/{other}/telemetry", "publish", "deny"),
            ("d/+/telemetry", "subscribe", "deny"),
            ("#", "subscribe", "deny")):
        r = await b.client.post("/ingest/v1/broker/acl", headers=hook, json={
            "username": device_id, "topic": topic, "action": action})
        assert r.json() == {"result": expected}, (topic, action)


async def test_polled_write_has_a_command_life(backend: Backend) -> None:
    b = backend
    await b.user("eng.x", ("engineer", None, None))
    h = await b.headers("eng.x")
    result = await write(b, h, f"{GATE_METER}.demand_limit", 250)
    assert result["result"] == "applied", result
    record = await b.store.commands.get(result["request_id"])
    assert record is not None and record.status == "confirmed"
    assert record.transport == "modbus_tcp" and record.delivered_at is not None
