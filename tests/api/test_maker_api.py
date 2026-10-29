"""The maker panel and the completed consumer app end to end: the maker edits a product and its
picture, checks a range for duplicates, registers and moves a batch, sees its devices by stage;
the buyer's warranty starts at the claim; rooms belong to the buyer's home only; the home
screen's summary, the chart history and the bell; the edge-hub catalog with its protocols."""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.settings import Settings
from tests.api.conftest import Backend, make_settings
from tests.api.test_ingest_api import owned_device

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token="h" * 32)


async def test_panel_is_served(backend: Backend) -> None:
    r = await backend.client.get("/maker/")
    assert r.status_code == 200 and '<div id="root"' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe" not in csp
    for path in ("/maker/js/main.js", "/maker/js/qr.js", "/maker/js/views/product.js",
                 "/maker/css/maker.css", "/maker/i18n/fa.json", "/maker/icon.svg"):
        assert (await backend.client.get(path)).status_code == 200, path


async def test_product_details_picture_stats(backend: Backend) -> None:
    b = backend
    h, sara, _, device_id, _ = await owned_device(b)
    alice, bob, sam = h["alice"], h["bob"], h["sam"]
    pid = (await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)
           ).json()["device"]["product_id"]
    url = f"/api/platform/products/{pid}"

    r = await b.client.patch(url, headers=alice, json={"name": "Relay 8", "category": ""})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Relay 8" and r.json()["category"] is None
    r = await b.client.patch(url, headers=alice, json={"identifier_pattern": "NEW-{000}"})
    assert r.status_code == 409                     # devices exist: printed labels keep theirs
    assert (await b.client.patch(url, headers=sam, json={"name": "x"})).status_code == 403
    assert (await b.client.patch(url, headers=bob, json={"name": "x"})).status_code == 404

    img = f"{url}/image"
    assert (await b.client.put(img, headers=alice | {"content-type": "image/png"},
                               content=b"not a png")).status_code == 422
    assert (await b.client.put(img, headers=alice | {"content-type": "image/png"},
                               content=PNG + b"\0" * 600_000)).status_code == 422
    r = await b.client.put(img, headers=alice | {"content-type": "image/png"}, content=PNG)
    assert r.status_code == 204, r.text
    assert (await b.client.get(url, headers=alice)).json()["has_image"] is True
    r = await b.client.get(img, headers=sara)              # the buyer of a device sees it
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert (await b.client.get(img, headers=bob)).status_code == 404
    assert (await b.client.delete(img, headers=alice)).status_code == 204
    assert (await b.client.get(img, headers=alice)).status_code == 404

    stats = (await b.client.get(f"/api/platform/stats?product={pid}", headers=alice)).json()
    assert stats["total"] == 2 and stats["lifecycle"]["active"] == 1
    assert stats["lifecycle"]["registered"] == 1
    assert (await b.client.get("/api/platform/stats", headers=bob)).json()["total"] == 0

    fields = (await b.client.get("/api/platform/protocols", headers=sara)).json()
    modbus = {f["name"]: f for f in fields["modbus_tcp"]["tag"]}
    assert modbus["register_type"]["type"] == "enum" and modbus["register_type"]["required"]
    assert {f["name"] for f in fields["modbus_tcp"]["connection"]} >= {"host", "unit_id"}
    assert (await b.client.get("/api/platform/catalog", headers=sara)).json() == []
    r = await b.client.patch(url, headers=alice, json={"catalog": True})
    assert r.json()["catalog"] is True
    catalog = (await b.client.get("/api/platform/catalog", headers=sara)).json()
    assert [(p["code"], p["protocols"]) for p in catalog] == [("NAT-8R", ["native"])]


async def test_range_check_batch_stage_and_filters(backend: Backend) -> None:
    b = backend
    h, sara, _, device_id, _ = await owned_device(b)
    alice, olga, sam = h["alice"], h["olga"], h["sam"]
    pid = (await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)
           ).json()["device"]["product_id"]
    check = f"/api/platform/products/{pid}/devices/check"
    r = await b.client.post(check, headers=olga, json={"start": 1, "count": 5})
    assert r.json() == {"first": "NAT-001", "last": "NAT-005", "count": 5,
                        "taken": ["NAT-001", "NAT-002"]}
    r = await b.client.post(check, headers=olga, json={"start": 10, "count": 3})
    assert r.json()["taken"] == []
    assert (await b.client.post(check, headers=sam, json={"start": 1, "count": 1})
            ).status_code == 403
    r = await b.client.post(f"/api/platform/products/{pid}/devices", headers=olga, json={
        "version": 1, "protocol": "native", "start": 10, "count": 3, "batch": "B-7",
        "warranty_months": 12})
    assert r.status_code == 201, r.text

    stage = f"/api/platform/products/{pid}/devices/lifecycle"
    r = await b.client.post(stage, headers=olga, json={"batch": "B-7", "lifecycle": "sold"})
    assert r.json() == {"moved": 3}
    r = await b.client.post(stage, headers=olga, json={"device_ids": [device_id],
                                                       "lifecycle": "disabled"})
    assert r.json() == {"moved": 0}                    # activated by its buyer: left alone
    assert (await b.client.post(stage, headers=olga, json={"lifecycle": "sold"})).status_code == 422
    assert (await b.client.post(stage, headers=sam, json={"batch": "B-7", "lifecycle": "ready"})
            ).status_code == 403

    async def ids(query: str) -> list[str]:
        r = await b.client.get(f"/api/platform/devices?product={pid}&{query}", headers=alice)
        assert r.status_code == 200, r.text
        return [d["identifier"] for d in r.json()["devices"]]

    assert await ids("lifecycle=stock") == ["NAT-002"]
    assert await ids("lifecycle=sold") == ["NAT-010", "NAT-011", "NAT-012"]
    assert await ids("batch=B-7&limit=2") == ["NAT-010", "NAT-011"]

    # warranty from the claim; the maker changes its length
    d = (await b.client.get(f"/api/platform/devices/{device_id}", headers=alice)).json()
    assert d["activated_at"] is not None and d["warranty_until"] is None    # no warranty set
    r = await b.client.patch(f"/api/platform/devices/{device_id}", headers=olga,
                             json={"warranty_months": 18})
    assert r.status_code == 200 and r.json()["warranty_months"] == 18
    until = r.json()["warranty_until"]
    start = datetime.fromisoformat(d["activated_at"])
    end = datetime.fromisoformat(until)
    assert (end.year - start.year) * 12 + end.month - start.month == 18 and end.day == start.day
    view = (await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)).json()
    assert view["device"]["warranty_until"] == until


async def test_rooms_summary_history_notifications(backend: Backend) -> None:
    b = backend
    h, sara, omid, device_id, secret = await owned_device(b)
    room = f"/api/platform/my/devices/{device_id}/room"
    assert (await b.client.put(room, headers=omid, json={"room": "x"})).status_code == 404
    assert (await b.client.put(room, headers=h["alice"], json={"room": "x"})).status_code == 403
    r = await b.client.put(room, headers=sara, json={"room": " پذیرایی "})
    assert r.json() == {"device_id": device_id, "room": "پذیرایی"}
    mine = (await b.client.get("/api/platform/my/devices", headers=sara)).json()
    assert mine[0]["room"] == "پذیرایی" and mine[0]["product_name"] == "Smart 8-relay"
    maker_view = (await b.client.get(f"/api/platform/devices/{device_id}", headers=h["alice"])
                  ).json()
    assert "room" not in maker_view                    # the maker never sees the buyer's rooms
    org = (await b.client.get("/api/account/organizations", headers=sara)).json()[0]["org_id"]
    in_home = (await b.client.get(f"/api/platform/my/devices?org={org}", headers=sara)).json()
    assert len(in_home) == 1
    assert (await b.client.get("/api/platform/my/devices?org=elsewhere", headers=sara)).json() == []

    for value in (2.0, 3.0):
        r = await b.client.post(f"/ingest/v1/devices/{device_id}/telemetry",
                                headers={"Authorization": f"Bearer {secret}"},
                                json={"values": {"power": value, "relay_1": True}})
        assert r.status_code == 200
    summary = (await b.client.get("/api/platform/my/summary", headers=sara)).json()
    assert summary["devices"] == 1 and summary["online"] == 1 and summary["power_kw"] == 3.0
    assert summary["alerts"] == 0
    r = await b.client.get(f"/api/platform/my/devices/{device_id}/history?tag=power",
                           headers=sara)
    assert r.status_code == 200, r.text
    assert (await b.client.get(f"/api/platform/my/devices/{device_id}/history?tag=power",
                               headers=omid)).status_code == 404
    assert (await b.client.get(f"/api/platform/my/devices/{device_id}/history?tag=nope",
                               headers=sara)).status_code == 404

    notes = (await b.client.get("/api/platform/my/notifications", headers=sara)).json()
    assert notes == []                                  # online, nothing failed
    second = b.second                                   # type: ignore[attr-defined]
    r = await b.client.post("/api/platform/claim", headers=sara,
                            json={"identifier": second["identifier"], "code": second["claim_code"]})
    assert r.status_code == 200, r.text
    await b.client.patch(f"/api/platform/my/devices/{second['device_id']}", headers=sara,
                         json={"enabled": True})
    notes = (await b.client.get("/api/platform/my/notifications", headers=sara)).json()
    assert [(n["kind"], n["device_id"]) for n in notes] == [("offline", second["device_id"])]
    summary = (await b.client.get("/api/platform/my/summary", headers=sara)).json()
    assert summary["devices"] == 2 and summary["alerts"] == 1
