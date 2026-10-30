"""The second round of the app and the maker panel end to end: homes and account deletion,
home settings and the tariff, groups, countdown timers, the SMS fallback, rules the device
runs by itself, sunrise / presence automations, calculated product tags, commands with
several parameters, tickets and warranty claims, branding, trends, the CSV export, plans."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.api.home import sms_signature
from backend.settings import Settings
from simulators.native_device import NativeDeviceSim
from tests.api.conftest import Backend, make_settings
from tests.api.test_accounts_api import account
from tests.api.test_claims_api import claim, labels, register
from tests.api.test_ingest_api import native_template, owned_device, running
from tests.api.test_platform_api import PRODUCT, _product, _setup

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token="h" * 32)


async def _home(b: Backend, h: dict[str, str]) -> str:
    rows = (await b.client.get("/api/account/organizations", headers=h)).json()
    return next(m["org_id"] for m in rows if m["personal"])


async def _fetch(b: Backend, device_id: str, secret: str) -> list[dict[str, Any]]:
    r = await b.client.get(f"/ingest/v1/devices/{device_id}/commands",
                           headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200, r.text
    return r.json()


async def test_homes_settings_groups_timers(backend: Backend) -> None:
    b = backend
    h, sara, omid, device_id, _ = await owned_device(b)
    r = await b.client.post("/api/account/homes", headers=sara, json={"name": "ویلا"})
    assert r.status_code == 201, r.text
    villa = r.json()["org_id"]
    r = await b.client.patch(f"/api/account/homes/{villa}", headers=sara,
                             json={"name": "ویلای شمال"})
    assert r.json()["name"] == "ویلای شمال"
    assert (await b.client.patch(f"/api/account/homes/{villa}", headers=omid,
                                 json={"name": "x"})).status_code == 404
    homes = (await b.client.get("/api/account/organizations", headers=sara)).json()
    assert {m["name"] for m in homes} >= {"ویلای شمال"}

    home = await _home(b, sara)
    url = f"/api/platform/organizations/{home}/settings"
    tariff = {"tiers": [{"up_to_kwh": 100, "price": 1000}, {"price": 3000}],
              "peak_from": "19:00", "peak_to": "23:00", "peak_factor": 1.5, "fixed": 5000,
              "tax_percent": 10}
    r = await b.client.put(url, headers=sara, json={"latitude": 26.53, "longitude": 53.98,
                                                     "tariff": tariff})
    assert r.status_code == 200, r.text
    got = (await b.client.get(url, headers=sara)).json()
    assert got["latitude"] == 26.53 and got["tariff"]["tiers"][1]["price"] == 3000
    assert (await b.client.get(url, headers=omid)).status_code == 404
    energy = (await b.client.get(f"/api/platform/my/energy?org={home}&days=7", headers=sara)).json()
    assert len(energy["days"]) == 7 and energy["month"]["cost"] == round(5000 * 1.1)

    groups = f"/api/platform/organizations/{home}/groups"
    r = await b.client.post(groups, headers=sara,
                            json={"name": "پذیرایی", "device_ids": [device_id]})
    assert r.status_code == 201, r.text
    gid = r.json()["group_id"]
    assert (await b.client.post(groups, headers=sara, json={
        "name": "x", "device_ids": ["dzzzzzzzzzzzzzzzzzzzz"]})).status_code == 422
    r = await b.client.put(f"{groups}/{gid}", headers=sara,
                           json={"name": "همه", "device_ids": [device_id, device_id]})
    assert r.json()["device_ids"] == [device_id]
    assert [g["name"] for g in (await b.client.get(groups, headers=sara)).json()] == ["همه"]
    assert (await b.client.get(groups, headers=omid)).status_code == 404
    # named parts inside a group, and the home's own names for outputs
    chandelier = {"name": "لوستر", "icon": "bulb", "outputs": [
        {"device_id": device_id, "key": "relay_1"}, {"device_id": device_id, "key": "relay_2"}]}
    r = await b.client.put(f"{groups}/{gid}", headers=sara, json={
        "name": "آشپزخانه", "device_ids": [device_id], "sections": [chandelier]})
    assert r.status_code == 200 and r.json()["sections"] == [chandelier], r.text
    stray = chandelier | {"outputs": [{"device_id": "dzzzzzzzzzzzzzzzzzzzz", "key": "relay_1"}]}
    assert (await b.client.put(f"{groups}/{gid}", headers=sara, json={
        "name": "x", "device_ids": [device_id], "sections": [stray]})).status_code == 422
    label = f"/api/platform/my/devices/{device_id}/labels"
    r = await b.client.put(f"{label}/relay_1", headers=sara,
                           json={"name": "لوستر بزرگ", "icon": "bulb", "favorite": True})
    assert r.status_code == 200, r.text
    assert (await b.client.put(f"{label}/nope", headers=sara, json={})).status_code == 404
    assert (await b.client.put(f"{label}/relay_1", headers=omid, json={})).status_code == 404
    view = (await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)).json()
    relay = next(t for t in view["tags"] if t["key"] == "relay_1")
    assert relay["display_name"]["fa"] == "لوستر بزرگ" and relay["favorite"] is True
    assert relay["icon"] == "bulb" and relay["default_name"]["fa"]
    await b.client.put(f"{label}/relay_1", headers=sara, json={})          # back to the maker's
    view = (await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)).json()
    relay = next(t for t in view["tags"] if t["key"] == "relay_1")
    assert relay["default_name"] is None and relay["favorite"] is False
    assert (await b.client.delete(f"{groups}/{gid}", headers=sara)).status_code == 204

    timers = f"/api/platform/my/devices/{device_id}/timers"
    r = await b.client.post(timers, headers=sara, json={"tag": "relay_1", "value": False,
                                                        "minutes": 30})
    assert r.status_code == 201, r.text
    listed = (await b.client.get(timers, headers=sara)).json()
    assert [(t["tag"], t["value"]) for t in listed] == [("relay_1", False)]
    assert (await b.client.post(timers, headers=omid, json={"tag": "relay_1", "value": True,
                                                            "minutes": 5})).status_code == 404
    assert (await b.client.post(timers, headers=sara, json={"tag": "power", "value": 1,
                                                            "minutes": 5})).status_code == 422
    assert (await b.client.delete(f"{timers}/{listed[0]['automation_id']}", headers=sara)
            ).status_code == 204
    assert (await b.client.get(timers, headers=sara)).json() == []


async def test_sms_local_rules_and_presence(backend: Backend) -> None:
    b = backend
    _, sara, omid, device_id, secret = await owned_device(b)
    sms = f"/api/platform/my/devices/{device_id}/sms"
    assert (await b.client.get(sms, headers=sara)).json()["enabled"] is False
    r = await b.client.put(sms, headers=sara, json={"phone": "+989121234567"})
    assert r.status_code == 200 and r.json()["sent_to_device"] == "http", r.text
    key = r.json()["key"]
    assert len(key) == 43 and (await b.client.get(sms, headers=sara)).json()["key"] == key
    assert (await b.client.put(sms, headers=omid, json={})).status_code == 404
    sim = NativeDeviceSim(device_id, secret, {"relay_1": False, "relay_2": False, "power": 0.0})
    for message in await _fetch(b, device_id, secret):
        sim.receive(message)
    assert sim.sms_key == key

    def signed(counter: int, *items: str, k: str = key) -> str:
        sig = sms_signature(k, device_id, counter, list(items))
        return " ".join(["HUB", str(counter), *items, sig])
    message = signed(1730000000, "relay_1=1", "relay_2=1")
    assert sim.sms(message) == ["relay_1", "relay_2"]
    sim.values["relay_1"] = False
    assert sim.sms(message) == [] and sim.values["relay_1"] is False      # replayed
    assert sim.sms(message.replace("relay_2=1", "relay_2=0")) == []       # changed
    assert sim.sms(signed(1730000001, "relay_1=1", k="A" * 43)) == []     # another key
    assert sim.sms(signed(1730000002, "nope=1")) == []
    assert sim.sms(signed(1730000003, "relay_1=1")) == ["relay_1"]
    # the key is kept on a later save; a new one only when asked
    assert (await b.client.put(sms, headers=sara, json={})).json()["key"] == key
    renewed = (await b.client.put(sms, headers=sara, json={"new_key": True})).json()["key"]
    assert renewed != key
    for message in await _fetch(b, device_id, secret):
        sim.receive(message)
    assert sim.sms_key == renewed and sim.sms(signed(1730000004, "relay_1=0")) == []

    home = await _home(b, sara)
    autos = f"/api/platform/organizations/{home}/automations"
    local = {"name": "پیک", "local": True, "trigger": {"type": "value", "device_id": device_id,
             "tag": "power", "op": "gt", "value": 5},
             "actions": [{"type": "write", "device_id": device_id, "tag": "relay_2",
                          "value": False}]}
    r = await b.client.post(autos, headers=sara, json=local)
    assert r.status_code == 201, r.text
    hub_only = local | {"name": "اعلان", "actions": [{"type": "notify", "text": "x"}]}
    assert (await b.client.post(autos, headers=sara, json=hub_only)).status_code == 201
    pushed = [m for m in await _fetch(b, device_id, secret) if m["type"] == "rules"]
    assert len(pushed) == 1 and [x["name"] for x in pushed[0]["rules"]] == ["پیک"]
    r = await b.client.get(f"/ingest/v1/devices/{device_id}/rules",
                           headers={"Authorization": f"Bearer {secret}"})
    rules = r.json()
    assert rules["rules"][0]["trigger"] == {"type": "value", "tag": "power", "op": "gt", "value": 5,
                                            "for_s": 0}
    assert rules["utc_offset_min"] == 210 and rules["server_time"].endswith("Z")
    assert (await b.client.get(f"/ingest/v1/devices/{device_id}/rules")).status_code == 401
    sim.receive(rules)
    sim.values["power"] = 7.0                     # cut off from the hub, the device decides
    assert sim.run_local(_noon()) == [rules["rules"][0]["id"]] and sim.values["relay_2"] is False

    sun = {"name": "غروب", "trigger": {"type": "sun", "event": "sunset", "offset_min": -10,
                                        "latitude": 26.53, "longitude": 53.98},
           "actions": [{"type": "write", "device_id": device_id, "tag": "relay_1", "value": True}]}
    r = await b.client.post(autos, headers=sara, json=sun)
    assert r.status_code == 201, r.text
    assert r.json()["next_runs"], r.json()
    arrive = {"name": "ورود", "trigger": {"type": "presence", "event": "arrive"},
              "actions": [{"type": "write", "device_id": device_id, "tag": "relay_1",
                           "value": True}]}
    assert (await b.client.post(autos, headers=sara, json=arrive)).status_code == 201
    r = await b.client.post(f"/api/platform/organizations/{home}/presence", headers=sara,
                            json={"event": "arrive"})
    assert r.json() == {"event": "arrive", "automations": 1}
    assert (await b.client.post(f"/api/platform/organizations/{home}/presence", headers=omid,
                                json={"event": "arrive"})).status_code == 404


def _noon() -> Any:
    from datetime import datetime

    return datetime(2026, 10, 28, 12, 0)


async def test_calculated_tags_and_commands(backend: Backend) -> None:
    b = backend
    h = await _setup(b)
    template = native_template()
    template["tags"].append({"key": "watts", "kind": "calculated", "display_name": {"fa": "وات"},
                             "data_type": "float64", "unit": "W", "formula": "power"})
    template["commands"] = [{"key": "all_on", "display_name": {"fa": "همه روشن"},
                             "params": [{"tag": "relay_1", "value": True},
                                        {"tag": "relay_2"}]}]
    body = PRODUCT | {"code": "CALC-8R", "identifier_pattern": "CALC-{000}"}
    pid = await _product(b, h["alice"], template, body=body)
    bad = native_template()
    bad["tags"].append({"key": "x", "kind": "calculated", "display_name": {"fa": "x"},
                        "data_type": "float64", "unit": "W", "formula": "power + 5[°C]"})
    r = await b.client.put(f"/api/platform/products/{pid}/draft", headers=h["alice"],
                           json={"definition": bad})
    assert r.status_code == 422 and "units" in r.text
    await register(b, h["alice"], pid, 1, protocol="native")
    rows = await labels(b, h["alice"], pid, credentials=True)
    sara = await account(b, "sara@example.com", "Sara")
    assert (await claim(b, sara, rows[0]["identifier"], rows[0]["claim_code"])).status_code == 200
    device_id, secret = rows[0]["device_id"], rows[0]["device_secret"]
    await b.client.patch(f"/api/platform/my/devices/{device_id}", headers=sara,
                         json={"enabled": True})
    r = await b.client.post(f"/ingest/v1/devices/{device_id}/telemetry",
                            headers={"Authorization": f"Bearer {secret}"},
                            json={"values": {"power": 2.5}})
    assert r.status_code == 200
    path = f"platform.devices.{device_id}.watts"
    for _ in range(40):
        sample = b.services.hub.latest.get(path)
        if sample is not None and sample.value is not None:
            break
        await asyncio.sleep(0.1)
    assert sample is not None and sample.value == pytest.approx(2500.0)   # kW -> W
    view = (await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)).json()
    watts = next(t for t in view["tags"] if t["key"] == "watts")
    assert watts["kind"] == "calculated" and watts["value"] == pytest.approx(2500.0)
    assert [c["key"] for c in view["commands"]] == ["all_on"]

    run = f"/api/platform/my/devices/{device_id}/actions/all_on"
    r = await b.client.post(run, headers=sara, json={})
    assert r.status_code == 422 and "relay_2" in r.text           # the user's value is needed
    assert (await b.client.post(f"/api/platform/my/devices/{device_id}/actions/nope",
                                headers=sara, json={})).status_code == 404
    sim = NativeDeviceSim(device_id, secret, {"relay_1": False, "relay_2": False, "power": 2.5})
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        r = await b.client.post(run, headers=sara, json={"values": {"relay_2": True}})
        stop.set()
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and [x["result"] for x in r.json()["results"]] == ["applied", "applied"]
    assert sim.values["relay_1"] is True and sim.values["relay_2"] is True


async def test_tickets_branding_trend_csv_billing_and_delete(backend: Backend) -> None:
    b = backend
    h, sara, omid, device_id, _ = await owned_device(b)
    alice, bob, boss = h["alice"], h["bob"], h["boss"]
    tickets = f"/api/platform/my/devices/{device_id}/tickets"
    assert (await b.client.post(tickets, headers=sara, json={
        "kind": "warranty", "subject": "خراب", "body": "رله ۱ کار نمی‌کند"})).status_code == 422
    await b.client.patch(f"/api/platform/devices/{device_id}", headers=alice,
                         json={"warranty_months": 18})
    await b.client.put("/api/platform/organizations/maker_a/branding", headers=alice,
                       json={"support_email": "help@kish.example"})
    r = await b.client.post(tickets, headers=sara, json={
        "kind": "warranty", "subject": "خراب", "body": "رله ۱ کار نمی‌کند"})
    assert r.status_code == 201, r.text
    tid = r.json()["ticket"]["ticket_id"]
    assert (await b.client.post(tickets, headers=omid, json={
        "kind": "support", "subject": "x", "body": "y"})).status_code == 404
    listed = (await b.client.get("/api/platform/tickets?status=open", headers=alice)).json()
    assert [t["ticket_id"] for t in listed] == [tid] and listed[0]["opened_by_name"] == "Sara"
    assert (await b.client.get("/api/platform/tickets", headers=bob)).json() == []
    assert (await b.client.get(f"/api/platform/tickets/{tid}", headers=bob)).status_code == 404
    r = await b.client.post(f"/api/platform/tickets/{tid}/messages", headers=h["sam"],
                            json={"body": "تأیید شد؛ برای تعویض بفرستید", "status": "approved"})
    assert r.status_code == 200 and r.json()["ticket"]["status"] == "approved", r.text
    mine = (await b.client.get(f"/api/platform/my/tickets/{tid}", headers=sara)).json()
    assert [m["side"] for m in mine["messages"]] == ["buyer", "maker"]
    assert (await b.client.get(f"/api/platform/my/tickets/{tid}", headers=omid)).status_code == 404
    mails = b.services.accounts.mailer.sent
    to_maker = [m for m in mails if f"/maker/#/support/tickets/{tid}" in m[2]]
    to_buyer = [m for m in mails if f"/#/ticket/{tid}" in m[2] and "/maker/" not in m[2]]
    assert [m[0] for m in to_maker] == ["help@kish.example"], mails
    assert "رله ۱ کار نمی‌کند" in to_maker[0][2]
    assert [m[0] for m in to_buyer] == ["sara@example.com"] and "تعویض" in to_buyer[0][2]

    brand = "/api/platform/organizations/maker_a/branding"
    r = await b.client.put(brand, headers=alice, json={"brand_name": "Kish Smart",
                                                       "accent": "#7FD1C7",
                                                       "support_email": "help@kish.example"})
    assert r.json()["display_name"] == "Kish Smart"
    assert (await b.client.put(brand, headers=h["sam"], json={})).status_code == 403
    assert (await b.client.put(brand, headers=bob, json={})).status_code == 404
    assert (await b.client.put(f"{brand}/logo", headers=alice | {"content-type": "image/png"},
                               content=PNG)).status_code == 204
    public = (await b.client.get("/api/public/brand/maker_a")).json()
    assert public["display_name"] == "Kish Smart" and public["has_logo"]
    assert (await b.client.get("/api/public/brand/maker_a/logo")).content == PNG
    assert (await b.client.get(f"/api/public/brand/{await _home(b, sara)}")).status_code == 404

    trend = (await b.client.get("/api/platform/trend?weeks=4", headers=alice)).json()
    assert sum(trend["registered"]) == 2 and sum(trend["activated"]) == 1
    assert sum(trend["tickets"]) == 1
    csv_text = (await b.client.get("/api/platform/devices.csv", headers=alice)).text
    assert csv_text.count("\n") == 3 and "NAT-001" in csv_text and "sara" not in csv_text.lower()

    bill = (await b.client.get("/api/platform/organizations/maker_a/billing", headers=alice)).json()
    assert bill["plan"]["plan_id"] == "starter" and bill["active_devices"] == 1
    assert bill["current"]["amount"] == 0
    assert (await b.client.put("/api/platform/organizations/maker_a/plan", headers=alice,
                               json={"plan_id": "business"})).status_code == 403
    r = await b.client.put("/api/platform/organizations/maker_a/plan", headers=boss,
                           json={"plan_id": "business"})
    assert r.json()["plan_id"] == "business"
    bill = (await b.client.get("/api/platform/organizations/maker_a/billing", headers=alice)).json()
    assert bill["current"]["amount"] == 50_000_000 and bill["current"]["status"] == "estimate"
    assert bill["invoices"] == []                         # looking stores nothing
    from datetime import UTC, datetime, timedelta

    from backend.api.maker import close_month

    next_month = datetime.now(UTC) + timedelta(days=32)
    assert await close_month(b.services, next_month) == 2          # maker_a and maker_b
    assert await close_month(b.services, next_month) == 0          # once per month
    bill = (await b.client.get("/api/platform/organizations/maker_a/billing", headers=alice)).json()
    (invoice,) = bill["invoices"]
    assert invoice["amount"] == 50_000_000 and invoice["status"] == "issued"
    r = await b.client.post(f"/api/platform/invoices/{invoice['invoice_id']}/status",
                            headers=boss, json={"status": "paid"})
    assert r.json()["status"] == "paid"

    r = await b.client.post("/api/account/delete", headers=sara,
                            json={"password": "wrong-password-1", "confirm": "DELETE"})
    assert r.status_code == 403
    r = await b.client.post("/api/account/delete", headers=sara,
                            json={"password": "Correct-Horse-42", "confirm": "DELETE"})
    assert r.status_code == 204, r.text
    d = (await b.client.get(f"/api/platform/devices/{device_id}", headers=alice)).json()
    assert d["lifecycle"] == "sold"                       # released, a new owner may claim it
    r = await b.client.post("/api/auth/login", json={"username": "sara@example.com",
                                                     "password": "Correct-Horse-42"})
    assert r.status_code == 401



async def test_cycle_timer_momentary_and_switch_types(backend: Backend) -> None:
    b = backend
    _, sara, omid, device_id, secret = await owned_device(b)
    base = f"/api/platform/my/devices/{device_id}"
    sim = NativeDeviceSim(device_id, secret, {"relay_1": False, "relay_2": False, "power": 0.0})
    seen: list[bool] = []
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        await b.wait(lambda: f"platform.devices.{device_id}.relay_1" in b.services.hub.latest)
        # a repeating timer: on 1 s, off 1 s, twice, starting now; the output ends off
        r = await b.client.post(f"{base}/timers", headers=sara, json={
            "tag": "relay_1", "mode": "cycle", "minutes": 0, "on_s": 1, "off_s": 1, "count": 2})
        assert r.status_code == 201, r.text
        listed = (await b.client.get(f"{base}/timers", headers=sara)).json()
        assert listed[0]["mode"] == "cycle" and listed[0]["count"] == 2
        for _ in range(60):
            seen.append(sim.values["relay_1"])
            await asyncio.sleep(0.1)
        assert True in seen and sim.values["relay_1"] is False
        # an endless one is cancelled: it stops and leaves the output off
        r = await b.client.post(f"{base}/timers", headers=sara, json={
            "tag": "relay_2", "mode": "cycle", "minutes": 0, "on_s": 30, "off_s": 1})
        tid = r.json()["automation_id"]
        await b.wait(lambda: sim.values["relay_2"] is True)
        assert (await b.client.delete(f"{base}/timers/{tid}", headers=sara)).status_code == 204
        await b.wait(lambda: sim.values["relay_2"] is False)
        # a momentary key: on now, off by itself
        r = await b.client.post(f"{base}/pulse", headers=sara, json={"tag": "relay_1",
                                                                     "seconds": 0.5})
        assert r.json()["result"] == "applied", r.text
        assert sim.values["relay_1"] is True
        await b.wait(lambda: sim.values["relay_1"] is False)
        assert (await b.client.post(f"{base}/pulse", headers=omid,
                                    json={"tag": "relay_1"})).status_code == 404
        stop.set()
    r = await b.client.put(f"{base}/labels/relay_3", headers=sara,
                           json={"name": "در", "icon": "door", "mode": "momentary"})
    assert r.json()["pulse_s"] == 1.0
    await b.client.put(f"{base}/labels/relay_4", headers=sara, json={"mode": "dual"})
    view = (await b.client.get(f"{base}/view", headers=sara)).json()
    modes = {t["key"]: (t["mode"], t["pulse_s"]) for t in view["tags"]}
    assert modes["relay_3"] == ("momentary", 1.0) and modes["relay_4"] == ("dual", None)
    assert modes["relay_1"] == ("toggle", None)


async def test_contacts_sms_alerts_and_sms_scenes(backend: Backend) -> None:
    from backend.sms import SmsOutbox

    b = backend
    b.services.sms = outbox = SmsOutbox()
    b.services.settings.sms_inbound_token = "t" * 24
    _, sara, omid, device_id, secret = await owned_device(b)
    home = await _home(b, sara)
    base = f"/api/platform/organizations/{home}"
    r = await b.client.post(f"{base}/contacts", headers=sara, json={
        "name": "سارا", "phone": "+989121234567", "sms": True, "call": True, "command": True})
    assert r.status_code == 201 and r.json()["phone"] == "09121234567", r.text
    assert (await b.client.post(f"{base}/contacts", headers=sara, json={
        "name": "x", "phone": "09121234567"})).status_code == 409
    assert (await b.client.get(f"{base}/contacts", headers=omid)).status_code == 404
    assert (await b.client.put(f"{base}/sms-pin", headers=sara,
                               json={"pin": "4821"})).status_code == 204
    listed = (await b.client.get(f"{base}/contacts", headers=sara)).json()
    assert listed["pin_set"] is True and listed["gateway"] is True
    assert "4821" not in str(listed)
    # an alert to the contacts by SMS and a call
    auto = (await b.client.post(f"{base}/automations", headers=sara, json={
        "name": "هشدار", "trigger": {"type": "time", "at": "08:00"},
        "actions": [{"type": "notify", "text": "دمای زیاد", "channels": ["sms", "call"]}]})).json()
    r = await b.client.post(f"{base}/automations/{auto['automation_id']}/test", headers=sara)
    assert r.status_code == 200, r.text
    record = await b.services.store.automations.automation(None, auto["automation_id"])
    from backend.automations import AutomationIn

    await b.services.automations.run(record, AutomationIn.from_record(record), "test-run")
    assert [k for k, _, _ in outbox.sent] == ["sms", "call"]
    assert "دمای زیاد" in outbox.sent[0][2]
    # a scene by an SMS keyword and the PIN; a wrong PIN and a stranger get nothing
    scene = (await b.client.post(f"{base}/scenes", headers=sara, json={
        "name": "شب", "sms_keyword": "شب", "actions": [
            {"type": "write", "device_id": device_id, "tag": "relay_1", "value": True}]})).json()
    assert (await b.client.post(f"{base}/scenes", headers=sara, json={
        "name": "x", "sms_keyword": "شب", "actions": [
            {"type": "write", "device_id": device_id, "tag": "relay_2", "value": True}]})
            ).status_code == 409
    inbound = "/api/sms/inbound?token=" + "t" * 24
    assert (await b.client.post("/api/sms/inbound?token=wrong", data={
        "from": "09121234567", "message": "شب 4821"})).status_code == 403
    outbox.sent.clear()
    r = await b.client.post(inbound, data={"from": "09121234567", "message": "شب 1111"})
    assert r.json()["result"] == "ignored" and outbox.sent == []
    r = await b.client.post(inbound, data={"from": "09350000000", "message": "شب 4821"})
    assert r.json()["result"] == "ignored"
    sim = NativeDeviceSim(device_id, secret, {"relay_1": False, "relay_2": False, "power": 0.0})
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        await b.wait(lambda: f"platform.devices.{device_id}.relay_1" in b.services.hub.latest)
        r = await b.client.post(inbound, json={"from": "+989121234567", "message": "4821 شب"})
        stop.set()
    assert r.json() == {"result": "ok", "scene_id": scene["scene_id"]}, r.text
    assert sim.values["relay_1"] is True and outbox.sent[-1][1] == "09121234567"
