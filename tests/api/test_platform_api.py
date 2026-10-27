"""Platform phase 1 through the API: organizations, products, versions, device registration,
organization permissions, isolation between manufacturers, audit, and a device built from a
product template collecting real values from the Modbus simulator (no code per product)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.conftest import Backend
from tests.platform.test_template import relay_template

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

PRODUCT = {"code": "CTRL-8R", "name": "Smart 8-relay", "category": "relay controller",
           "identifier_pattern": "CTRL-8R-{0000}"}
METER_TEMPLATE = {
    "tags": [
        {"key": "voltage", "kind": "analog_input", "display_name": {"fa": "ولتاژ", "en": "Volt"},
         "data_type": "float32", "unit": "V"},
        {"key": "breaker", "kind": "digital_input", "display_name": {"en": "Breaker"},
         "data_type": "bool"},
        {"key": "relay", "kind": "digital_output", "display_name": {"en": "Relay"},
         "data_type": "bool", "access": "read_write"},
    ],
    "bindings": {"modbus_tcp": {
        "connection": {"timeout_ms": 500, "retries": 0},
        "device": {"poll_interval_ms": 100},
        "tags": {"voltage": {"register_type": "input", "address": 0},
                 "breaker": {"register_type": "discrete_input", "address": 0},
                 "relay": {"register_type": "coil", "address": 0}}}},
}


async def _setup(b: Backend) -> dict[str, dict[str, str]]:
    """Platform admin, two manufacturers' admins, a production operator and support."""
    await b.user("boss", ("admin", None, None))
    boss = await b.headers("boss")
    for org, kind in (("maker_a", "manufacturer"), ("maker_b", "manufacturer")):
        r = await b.client.post("/api/platform/organizations", headers=boss,
                                json={"org_id": org, "name": org.title(), "kind": kind})
        assert r.status_code == 201, r.text
    users = {"alice": ("manufacturer_admin", "maker_a"), "bob": ("manufacturer_admin", "maker_b"),
             "olga": ("production_operator", "maker_a"),
             "sam": ("manufacturer_support", "maker_a")}
    for name, (role, org) in users.items():
        user_id = await b.user(f"{name}.x")
        await b.store.grant_role(user_id, role, None, None, None, org_id=org)
    return {"boss": boss} | {n: await b.headers(f"{n}.x") for n in users}


async def _product(b: Backend, h: dict[str, str], template: dict[str, Any],
                   org: str = "maker_a", body: dict[str, Any] = PRODUCT) -> str:
    r = await b.client.post("/api/platform/products", headers=h, json=body | {"org_id": org})
    assert r.status_code == 201, r.text
    pid = r.json()["product_id"]
    r = await b.client.put(f"/api/platform/products/{pid}/draft", headers=h,
                           json={"definition": template, "notes": "first"})
    assert r.status_code == 200, r.text
    r = await b.client.post(f"/api/platform/products/{pid}/versions/1/publish", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "published", r.text
    return pid


async def test_maker_flow_and_permissions(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    pid = await _product(b, h["alice"], relay_template())

    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["olga"],
                            json={"version": 1, "protocol": "mqtt", "start": 1, "count": 25,
                                  "batch": "1405-08", "warranty_months": 18})
    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["registered"], out["first"], out["last"]) == (25, "CTRL-8R-0001", "CTRL-8R-0025")
    device = out["devices"][0]
    assert device["lifecycle"] == "registered" and device["enabled"] is False

    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["alice"],
                            json={"version": 1, "protocol": "mqtt", "start": 20, "count": 10})
    assert r.status_code == 409 and r.json()["error"] == "duplicate", r.text
    assert "CTRL-8R-0020" in r.json()["values"]

    r = await b.client.get("/api/platform/devices", headers=h["sam"],
                           params={"product": pid, "q": "0007"})
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["devices"][0]["identifier"] == "CTRL-8R-0007"

    # what each role may do
    r = await b.client.post("/api/platform/products", headers=h["olga"],
                            json=PRODUCT | {"org_id": "maker_a", "code": "X-1"})
    assert r.status_code == 403
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["sam"],
                            json={"version": 1, "protocol": "mqtt", "start": 900, "count": 1})
    assert r.status_code == 403
    r = await b.client.patch(f"/api/platform/devices/{device['device_id']}", headers=h["olga"],
                             json={"lifecycle": "ready", "name": "Shelf 3"})
    assert r.status_code == 200 and r.json()["lifecycle"] == "ready", r.text

    # invalid templates are refused with the reason, like invalid device YAML
    bad = relay_template()
    bad["bindings"]["modbus_tcp"]["tags"]["relay_1"] = {"register_type": "input", "address": 0}
    r = await b.client.put(f"/api/platform/products/{pid}/draft", headers=h["alice"],
                           json={"definition": bad})
    assert r.status_code == 422 and r.json()["error"] == "invalid_template"
    assert "relay_1" in str(r.json()["details"])

    events = {a.event for a in await b.store.list_auth_audit()}
    assert {"organization_created", "product_saved", "product_version_saved",
            "product_version_published", "devices_registered",
            "platform_device_updated"} <= events


async def test_manufacturers_are_isolated(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    pid = await _product(b, h["alice"], relay_template())
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["alice"],
                            json={"version": 1, "protocol": "mqtt", "start": 1, "count": 3})
    did = r.json()["devices"][0]["device_id"]
    bob = h["bob"]

    r = await b.client.get("/api/platform/products", headers=bob)
    assert r.status_code == 200 and r.json() == []
    r = await b.client.get("/api/platform/devices", headers=bob)
    assert r.json() == {"total": 0, "devices": []}
    for method, url, body in (
            ("GET", f"/api/platform/products/{pid}", None),
            ("GET", f"/api/platform/products/{pid}/versions", None),
            ("GET", f"/api/platform/products/{pid}/versions/1", None),
            ("PUT", f"/api/platform/products/{pid}/draft", {"definition": relay_template()}),
            ("POST", f"/api/platform/products/{pid}/versions/1/publish", None),
            ("POST", f"/api/platform/products/{pid}/devices",
             {"version": 1, "protocol": "mqtt", "start": 500, "count": 1}),
            ("GET", f"/api/platform/devices/{did}", None),
            ("PATCH", f"/api/platform/devices/{did}", {"name": "mine now"})):
        r = await b.client.request(method, url, headers=bob, json=body)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
    r = await b.client.post("/api/platform/products", headers=bob,
                            json=PRODUCT | {"org_id": "maker_a", "code": "EVIL-1"})
    assert r.status_code == 403
    r = await b.client.get("/api/platform/organizations", headers=bob)
    assert [o["org_id"] for o in r.json()] == ["maker_b"]

    # a product of B is B's alone; the platform admin sees both
    await _product(b, bob, relay_template(), "maker_b", PRODUCT | {"code": "B-RELAY",
                                                                   "identifier_pattern":
                                                                   "B-{000}"})
    r = await b.client.get("/api/platform/products", headers=h["alice"])
    assert [p["code"] for p in r.json()] == ["CTRL-8R"]
    r = await b.client.get("/api/platform/products", headers=h["boss"])
    assert sorted(p["code"] for p in r.json()) == ["B-RELAY", "CTRL-8R"]

    # organization grants come from the platform too; a site grant on "platform" is refused
    r = await b.client.get("/api/auth/me", headers=h["alice"])
    perms = r.json().get("permissions", {})
    assert perms.get("product.manage") == [{"org": "maker_a"}], perms


async def test_platform_site_grant_refused(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    user_id = await b.user("eve.x")
    r = await b.client.post(f"/api/users/{user_id}/roles", headers=h["boss"],
                            json={"role": "viewer", "site": "platform"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_scope", r.text
    r = await b.client.post(f"/api/users/{user_id}/roles", headers=h["boss"],
                            json={"role": "manufacturer_support", "organization": "maker_b"})
    assert r.status_code in (200, 201), r.text


async def test_template_device_collects_from_simulator(backend: Backend) -> None:
    """A product defined only as data, registered and enabled, polls the Modbus simulator."""
    b = backend
    h = await _setup(b)
    pid = await _product(b, h["alice"], METER_TEMPLATE, body=PRODUCT | {
        "code": "METER-1", "identifier_pattern": "MTR-{00000}"})
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=h["alice"], json={
        "version": 1, "protocol": "modbus_tcp", "devices": [
            {"identifier": "mtr-00001", "serial_number": "SN-1"}],
        "connection": {"host": "127.0.0.1", "port": b.sim.port, "unit_id": 1},
        "enabled": True})
    assert r.status_code == 201, r.text
    device = r.json()["devices"][0]
    assert device["identifier"] == "MTR-00001" and device["enabled"] is True
    path = f"platform.devices.{device['device_id']}.voltage"
    hub = b.services.hub
    await b.wait(lambda: path in hub.latest and hub.latest[path].value is not None)
    assert hub.latest[path].value == pytest.approx(230.0)
    assert hub.latest[f"platform.devices.{device['device_id']}.breaker"].value is True

    # read-only first: no command reaches a platform device before phase 4, not even an admin's
    r = await b.client.post("/api/control/write", headers=h["boss"], json={
        "tag": f"platform.devices.{device['device_id']}.relay", "value": True, "reason": "t"})
    assert r.status_code == 200 and r.json()["result"] == "rejected_rbac", r.text

    # stays across a configuration reload (YAML side) ...
    await b.services.reload(b.services.hub.report)
    assert device["device_id"] in hub.drivers
    # ... and stops when disabled
    r = await b.client.patch(f"/api/platform/devices/{device['device_id']}",
                             headers=h["alice"], json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert device["device_id"] not in hub.drivers and path not in hub.latest

    # a broken connection is refused before anything runs
    r = await b.client.patch(f"/api/platform/devices/{device['device_id']}",
                             headers=h["alice"], json={"enabled": True,
                                                       "connection": {"port": 99999}})
    assert r.status_code == 422 and r.json()["error"] == "invalid_device", r.text
