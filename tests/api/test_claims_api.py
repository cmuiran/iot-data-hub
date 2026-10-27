"""Platform phase 3 through the API: label CSV with claim codes and QR, the buyer's claim
(identifier + code), uniform refusals, lockout, transfer, release, the manufacturer's reset,
device credentials, CSV registration, and an edge hub with a catalog device behind it that
polls the Modbus simulator."""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest

from backend.settings import Settings
from tests.api.conftest import Backend, make_settings
from tests.api.test_accounts_api import account
from tests.api.test_platform_api import METER_TEMPLATE, PRODUCT, _product, _setup
from tests.platform.test_template import relay_template

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
HUB_TEMPLATE = {"tags": [{"key": "uptime", "kind": "variable", "display_name": {"en": "Up"},
                          "data_type": "uint32", "unit": "s"}],
                "bindings": {"mqtt": {"connection": {
                    "broker_ref": "main", "telemetry_topic": "d/{device_id}/telemetry"},
                    "tags": {"uptime": {"json_path": "$.uptime"}}}}}


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test")


async def register(b: Backend, h: dict[str, str], pid: str, count: int = 3,
                   protocol: str = "mqtt") -> list[dict[str, Any]]:
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h,
                            json={"version": 1, "protocol": protocol, "start": 1,
                                  "count": count})
    assert r.status_code == 201, r.text
    return r.json()["devices"]


async def labels(b: Backend, h: dict[str, str], pid: str, **body: Any) -> list[dict[str, str]]:
    r = await b.client.post(f"/api/platform/products/{pid}/labels", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(r.text)))


async def claim(b: Backend, h: dict[str, str], identifier: str, code: str, **extra: Any) -> Any:
    return await b.client.post("/api/platform/claim", headers=h,
                               json={"identifier": identifier, "code": code} | extra)


async def mine(b: Backend, h: dict[str, str]) -> list[str]:
    r = await b.client.get("/api/platform/my/devices", headers=h)
    return [d["identifier"] for d in r.json()]


async def test_labels_claim_transfer_release_reset(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    pid = await _product(b, h["alice"], relay_template())
    devices = await register(b, h["alice"], pid)
    rows = await labels(b, h["olga"], pid, credentials=True)
    assert [r["identifier"] for r in rows] == ["CTRL-8R-0001", "CTRL-8R-0002", "CTRL-8R-0003"]
    first = rows[0]
    assert len(first["claim_code"]) == 14 and first["claim_code"].count("-") == 2
    assert first["qr"] == f"https://hub.test/#/claim/CTRL-8R-0001/{first['claim_code']}"
    async with b.store._conn() as conn:       # only keyed hashes are stored
        stored = [r["code_hash"] for r in await (await conn.execute(
            "SELECT code_hash FROM device_claim_codes")).fetchall()]
    assert all(s.startswith("hmac-sha256$") for s in stored) and len(stored) == 3
    assert not any(first["claim_code"].replace("-", "") in s for s in stored)
    assert await b.services.claims.credential_valid(first["device_id"], first["device_secret"])
    assert not await b.services.claims.credential_valid(first["device_id"], "guess")

    sara = await account(b, "sara@example.com", "Sara")
    omid = await account(b, "omid@example.com", "Omid")
    # every refusal looks the same: unknown device, wrong code, (later) used or locked
    refusals = [await claim(b, sara, "CTRL-8R-0001", "AAAA-BBBB-CCCC"),
                await claim(b, sara, "NOPE-0001", first["claim_code"])]
    assert {(r.status_code, r.json()["error"], r.json()["message"]) for r in refusals} == {
        (422, "invalid_claim", "identifier or claim code is not valid")}
    r = await claim(b, sara, "ctrl-8r-0001", first["claim_code"].lower().replace("-", " "),
                    name="Living room relays")       # typed loosely: still the code
    assert r.status_code == 200 and r.json()["transferred"] is False, r.text
    assert await mine(b, sara) == ["CTRL-8R-0001"]
    r = await claim(b, omid, "CTRL-8R-0001", first["claim_code"])
    assert r.json()["error"] == "invalid_claim"                   # used once
    r = await b.client.get("/api/platform/my/devices", headers=sara)
    assert r.json()[0]["name"] == "Living room relays" and r.json()[0]["lifecycle"] == "active"

    # lockout after repeated wrong codes, even for the right code
    second = rows[1]
    for _ in range(5):
        await claim(b, omid, "CTRL-8R-0002", "WRNG-WRNG-WRNG")
    r = await claim(b, omid, "CTRL-8R-0002", second["claim_code"])
    assert r.json()["error"] == "invalid_claim"

    # a claimed device gets no new label; transfer moves it with a one-time code
    assert [r["identifier"] for r in await labels(b, h["alice"], pid)] == [
        "CTRL-8R-0002", "CTRL-8R-0003"]
    r = await b.client.post(f"/api/platform/my/devices/{devices[0]['device_id']}/transfer",
                            headers=omid)
    assert r.status_code == 404                                    # not omid's device
    r = await b.client.post(f"/api/platform/my/devices/{devices[0]['device_id']}/transfer",
                            headers=sara)
    assert r.status_code == 200, r.text
    r = await claim(b, omid, "CTRL-8R-0001", r.json()["claim_code"])
    assert r.status_code == 200 and r.json()["transferred"] is True, r.text
    assert await mine(b, sara) == [] and await mine(b, omid) == ["CTRL-8R-0001"]

    r = await b.client.delete(f"/api/platform/my/devices/{devices[0]['device_id']}",
                              headers=omid)
    assert r.status_code == 204 and await mine(b, omid) == []

    # the maker's reset: every access ends, a fresh activation code works (lock cleared)
    r = await b.client.post(f"/api/platform/devices/{devices[1]['device_id']}/reset",
                            headers=h["bob"])
    assert r.status_code == 404                                    # another manufacturer
    r = await b.client.post(f"/api/platform/devices/{devices[1]['device_id']}/reset",
                            headers=h["alice"])
    assert r.status_code == 200, r.text
    r = await claim(b, omid, "CTRL-8R-0002", r.json()["claim_code"])
    assert r.status_code == 200, r.text

    events = {a.event for a in await b.store.list_auth_audit()}
    assert {"claim_codes_issued", "claim_failed", "device_claimed", "device_transfer_started",
            "device_released", "device_reset"} <= events
    async with b.store._conn() as conn:       # no code or secret ever reaches the audit log
        details = " ".join(str(r["detail"]) for r in await (await conn.execute(
            "SELECT detail FROM auth_audit")).fetchall())
    assert first["claim_code"] not in details and first["device_secret"] not in details


async def test_csv_registration(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    pid = await _product(b, h["alice"], relay_template())
    text = "identifier,serial_number\nctrl-8r-0101,SN-101\nCTRL-8R-0102,SN-102\n\nCTRL-8R-0103,\n"
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["alice"],
                            json={"version": 1, "protocol": "mqtt", "csv": text})
    assert r.status_code == 201 and r.json()["registered"] == 3, r.text
    serials = {d["identifier"]: d["serial_number"] for d in r.json()["devices"]}
    assert serials == {"CTRL-8R-0101": "SN-101", "CTRL-8R-0102": "SN-102",
                       "CTRL-8R-0103": None}
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["alice"],
                            json={"version": 1, "protocol": "mqtt", "csv": "x,1\nbad id,2"})
    assert r.status_code == 422


async def test_edge_hub_with_catalog_device(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    hub_pid = await _product(b, h["alice"], HUB_TEMPLATE, body=PRODUCT | {
        "code": "EDGE-1", "identifier_pattern": "EDGE-{000}", "kind": "edge_hub"})
    meter_pid = await _product(b, h["bob"], METER_TEMPLATE, "maker_b", PRODUCT | {
        "code": "METER-9", "identifier_pattern": "M9-{000}", "catalog": True})
    private_pid = await _product(b, h["bob"], METER_TEMPLATE, "maker_b", PRODUCT | {
        "code": "METER-P", "identifier_pattern": "MP-{000}"})
    relay_pid = await _product(b, h["alice"], relay_template(), body=PRODUCT | {
        "code": "R-2", "identifier_pattern": "R2-{000}"})
    (hub,) = await register(b, h["alice"], hub_pid, 1)
    (relay,) = await register(b, h["alice"], relay_pid, 1)
    codes = {r["identifier"]: r["claim_code"] for r in await labels(b, h["alice"], hub_pid)}
    relay_code = (await labels(b, h["alice"], relay_pid))[0]["claim_code"]
    sara = await account(b, "sara@example.com", "Sara")
    omid = await account(b, "omid@example.com", "Omid")
    assert (await claim(b, sara, "EDGE-001", codes["EDGE-001"])).status_code == 200
    assert (await claim(b, sara, "R2-001", relay_code)).status_code == 200

    r = await b.client.get("/api/platform/catalog", headers=sara)
    assert [p["code"] for p in r.json()] == ["METER-9"]
    child = {"product_id": meter_pid, "version": 1, "protocol": "modbus_tcp",
             "name": "Main meter", "enabled": True,
             "connection": {"host": "127.0.0.1", "port": b.sim.port, "unit_id": 1}}
    base = f"/api/platform/my/devices/{hub['device_id']}/children"
    r = await b.client.post(f"/api/platform/my/devices/{relay['device_id']}/children",
                            headers=sara, json=child)
    assert r.status_code == 422 and r.json()["error"] == "not_edge_hub"
    r = await b.client.post(base, headers=sara, json=child | {"product_id": private_pid})
    assert r.status_code == 404                              # not in the catalog
    r = await b.client.post(base, headers=omid, json=child)
    assert r.status_code == 404                              # not omid's hub
    r = await b.client.post(base, headers=sara, json=child)
    assert r.status_code == 201, r.text
    meter = r.json()
    assert meter["identifier"] == "EDGE-001-001" and meter["parent_device_id"] == hub["device_id"]
    path = f"platform.devices.{meter['device_id']}.voltage"
    live = b.services.hub
    await b.wait(lambda: path in live.latest and live.latest[path].value is not None)
    assert live.latest[path].value == pytest.approx(230.0)
    assert set(await mine(b, sara)) == {"EDGE-001", "EDGE-001-001", "R2-001"}
    r = await b.client.post(f"/api/platform/my/devices/{meter['device_id']}/transfer",
                            headers=sara)
    assert r.status_code == 422 and r.json()["error"] == "child_device"

    # the hub moves with what is behind it
    r = await b.client.post(f"/api/platform/my/devices/{hub['device_id']}/transfer",
                            headers=sara)
    assert (await claim(b, omid, "EDGE-001", r.json()["claim_code"])).status_code == 200
    assert await mine(b, sara) == ["R2-001"]
    assert set(await mine(b, omid)) == {"EDGE-001", "EDGE-001-001"}

    r = await b.client.delete(f"{base}/{meter['device_id']}", headers=omid)
    assert r.status_code == 204
    assert await mine(b, omid) == ["EDGE-001"]
    assert meter["device_id"] not in live.drivers and path not in live.latest
