"""Platform phase 2 through the API: sign-up with e-mail and verification, sign-in by e-mail,
password reset, no account enumeration, rate limits, invitations (family members / staff),
members, and sharing a device with another organization; isolation between customers."""

from __future__ import annotations

import re
from typing import Any

import pytest

from backend.settings import Settings
from hub.platform.devices import ProductIn
from hub.platform.template import ProductTemplate
from tests.api.conftest import PASSWORD, Backend, make_settings
from tests.platform.test_template import relay_template

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
LINK = re.compile(r"#/account/(verify|reset|invitation|forgot)/(\S*)")


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test")


def outbox(b: Backend) -> list[tuple[str, str, str]]:
    return b.services.accounts.mailer.sent


def last_link(b: Backend, to: str, kind: str) -> str:
    for address, _, body in reversed(outbox(b)):
        m = LINK.search(body)
        if address == to and m and m[1] == kind:
            return m[2]
    raise AssertionError(f"no {kind} e-mail to {to}: {outbox(b)}")


async def signup(b: Backend, email: str, name: str = "Sara", **extra: Any) -> dict[str, Any]:
    r = await b.client.post("/api/account/signup", json={
        "email": email, "password": PASSWORD, "display_name": name} | extra)
    assert r.status_code == 202, r.text
    return r.json()


async def login(b: Backend, email: str, password: str = PASSWORD) -> Any:
    return await b.client.post("/api/auth/login", json={"username": email, "password": password})


async def account(b: Backend, email: str, name: str = "Sara") -> dict[str, str]:
    """Sign up, verify, sign in; the auth header."""
    await signup(b, email, name)
    r = await b.client.post("/api/account/verify", json={"token": last_link(b, email, "verify")})
    assert r.status_code == 200, r.text
    r = await login(b, email)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def personal_org(b: Backend, h: dict[str, str]) -> str:
    r = await b.client.get("/api/account/organizations", headers=h)
    (org,) = [o for o in r.json() if o["personal"]]
    return org["org_id"]


async def test_signup_verify_login(backend: Backend) -> None:
    b = backend
    assert await signup(b, "Sara@Example.COM", language="fa") == {"status": "check_email"}
    r = await login(b, "sara@example.com")
    assert r.status_code == 401 and r.json()["error"] == "email_not_verified"
    (to, subject, body) = outbox(b)[-1]
    assert to == "Sara@example.com" and subject == "تأیید نشانی ایمیل"
    assert "https://hub.test/#/account/verify/" in body

    token = last_link(b, "Sara@example.com", "verify")
    r = await b.client.post("/api/account/verify", json={"token": token})
    assert r.status_code == 200 and r.json()["organization"].startswith("o"), r.text
    r = await b.client.post("/api/account/verify", json={"token": token})
    assert r.status_code == 404 and r.json()["error"] == "invalid_token"      # used once

    r = await login(b, "SARA@example.com")                  # the address, any case
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await b.client.get("/api/account/organizations", headers=h)
    orgs = r.json()
    assert [(o["kind"], o["role_id"], o["personal"]) for o in orgs] == [
        ("customer", "customer_owner", True)]
    r = await b.client.get("/api/auth/me", headers=h)
    assert r.json()["permissions"]["device.share"] == [{"org": orgs[0]["org_id"]}]
    assert r.json()["permissions"].get("data.read") is None       # no engineering access

    events = [a.event for a in await b.store.list_auth_audit()]
    assert {"account_signup", "email_verified", "login_ok"} <= set(events)


async def test_no_enumeration_and_weak_passwords(backend: Backend) -> None:
    b = backend
    await account(b, "ali@example.com", "Ali")
    sent = len(outbox(b))
    # the same answer for an existing address; its owner is told by e-mail
    assert await signup(b, "ali@example.com", "Mallory") == {"status": "check_email"}
    assert outbox(b)[-1][0] == "ali@example.com" and "#/account/forgot/" in outbox(b)[-1][2]
    for path in ("/api/account/verify/resend", "/api/account/password/forgot"):
        r = await b.client.post(path, json={"email": "nobody@example.com"})
        assert (r.status_code, r.json()) == (202, {"status": "check_email"})
    assert len(outbox(b)) == sent + 1                          # nothing to unknown addresses

    r = await b.client.post("/api/account/signup", json={
        "email": "weak@example.com", "password": "short", "display_name": "W"})
    assert r.status_code == 422 and r.json()["error"] == "weak_password"
    r = await b.client.post("/api/account/signup", json={
        "email": "not-an-address", "password": PASSWORD, "display_name": "W"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_email"


async def test_password_reset(backend: Backend) -> None:
    b = backend
    await account(b, "reza@example.com", "Reza")
    r = await login(b, "reza@example.com")
    refresh = r.json()["refresh_token"]
    r = await b.client.post("/api/account/password/forgot", json={"email": "reza@example.com"})
    assert r.status_code == 202
    token = last_link(b, "reza@example.com", "reset")

    r = await b.client.post("/api/account/password/reset",
                            json={"token": token, "new_password": "aaaa"})
    assert r.status_code == 422                               # policy; the link still works
    r = await b.client.post("/api/account/password/reset",
                            json={"token": token, "new_password": "Brand-New-Pass-77"})
    assert r.status_code == 200, r.text
    r = await b.client.post("/api/account/password/reset",
                            json={"token": token, "new_password": "Other-Pass-9911"})
    assert r.status_code == 404                               # used once

    r = await b.client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401                               # every old session ended
    assert (await login(b, "reza@example.com")).status_code == 401
    assert (await login(b, "reza@example.com", "Brand-New-Pass-77")).status_code == 200
    assert {"password_reset_requested", "password_reset"} <= {
        a.event for a in await b.store.list_auth_audit()}


async def test_signup_closed_but_invitations_work(backend: Backend) -> None:
    b = backend
    owner = await account(b, "owner@example.com", "Owner")
    org = await personal_org(b, owner)
    b.services.accounts.settings = b.services.accounts.settings.model_copy(
        update={"signup_enabled": False})
    r = await b.client.post("/api/account/signup", json={
        "email": "x@example.com", "password": PASSWORD, "display_name": "X"})
    assert r.status_code == 403 and r.json()["error"] == "signup_disabled"

    r = await b.client.post(f"/api/platform/organizations/{org}/invitations", headers=owner,
                            json={"email": "Mina@Example.com", "role": "customer_member"})
    assert r.status_code == 201, r.text
    token = last_link(b, "Mina@example.com", "invitation")
    r = await b.client.post("/api/account/signup", json={
        "email": "other@example.com", "password": PASSWORD, "display_name": "O",
        "invitation_token": token})
    assert r.status_code == 403 and r.json()["error"] == "invitation_other_email"
    assert await signup(b, "mina@example.com", "Mina", invitation_token=token) == {
        "status": "active"}                                   # verified by the invitation
    r = await login(b, "mina@example.com")
    assert r.status_code == 200, r.text
    mina = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await b.client.get("/api/account/organizations", headers=mina)
    assert {(o["org_id"], o["role_id"]) for o in r.json()} >= {(org, "customer_member")}


async def test_members_and_roles(backend: Backend) -> None:
    b = backend
    owner = await account(b, "home@example.com", "Home")
    org = await personal_org(b, owner)
    for bad in ({"email": "a@example.com", "role": "manufacturer_admin"},
                {"email": "a@example.com", "role": "admin"}):
        r = await b.client.post(f"/api/platform/organizations/{org}/invitations",
                                headers=owner, json=bad)
        assert r.status_code == 422 and r.json()["error"] == "invalid_role"
    r = await b.client.post(f"/api/platform/organizations/{org}/invitations", headers=owner,
                            json={"email": "kid@example.com", "role": "customer_viewer"})
    invitation_id = r.json()["invitation_id"]
    kid = await account(b, "kid@example.com", "Kid")
    r = await b.client.post("/api/account/invitations/accept", headers=kid,
                            json={"token": last_link(b, "kid@example.com", "invitation")})
    assert r.status_code == 200 and r.json()["organization"] == org, r.text
    r = await b.client.get(f"/api/platform/organizations/{org}/invitations", headers=owner)
    assert invitation_id not in [i["invitation_id"] for i in r.json()]      # accepted

    r = await b.client.get(f"/api/platform/organizations/{org}/members", headers=owner)
    members = {m["email"]: m for m in r.json()}
    assert set(members) == {"home@example.com", "kid@example.com"}
    # the viewer cannot manage the home (its own personal org does not count)
    r = await b.client.get(f"/api/platform/organizations/{org}/members", headers=kid)
    assert r.status_code == 404
    r = await b.client.post(f"/api/platform/organizations/{org}/invitations", headers=kid,
                            json={"email": "z@example.com", "role": "customer_owner"})
    assert r.status_code == 404
    # the owner of a personal home stays; the kid leaves
    owner_grant = members["home@example.com"]["user_role_id"]
    r = await b.client.delete(f"/api/platform/organizations/{org}/members/{owner_grant}",
                              headers=owner)
    assert r.status_code == 409 and r.json()["error"] in ("last_owner", "personal_owner")
    r = await b.client.delete(f"/api/account/organizations/{org}", headers=kid)
    assert r.status_code == 204
    r = await b.client.get(f"/api/platform/organizations/{org}/members", headers=owner)
    assert [m["email"] for m in r.json()] == ["home@example.com"]


async def _owned_device(b: Backend, org_id: str) -> str:
    """A manufactured device owned by ``org_id`` (the claim itself is phase 3)."""
    p = b.store.platform
    if "maker_z" not in await p.organization_ids():
        await p.create_organization("maker_z", "Maker Z", "manufacturer")
    product = await p.create_product({"maker_z"}, "maker_z", ProductIn(
        code=f"R{org_id[-4:].upper()}", name="Relay", identifier_pattern=f"R{org_id[-4:]}-{{000}}"
        .upper()), None)
    await p.save_draft({"maker_z"}, product.product_id,
                       ProductTemplate.model_validate(relay_template()), None, None)
    await p.publish({"maker_z"}, product.product_id, 1, None)
    (device,) = await p.register_devices(
        {"maker_z"}, product.product_id, 1, protocol="mqtt", entries=[
            (product.identifier_pattern.replace("{000}", "001"), None)], batch=None,
        warranty_months=None, connection={}, options={}, enabled=False, user_id=None)
    await b.store.accounts.set_owner(device.device_id, org_id, None)
    return device.device_id


async def test_device_sharing_and_isolation(backend: Backend) -> None:
    b = backend
    sara = await account(b, "sara@example.com", "Sara")
    omid = await account(b, "omid@example.com", "Omid")
    device = await _owned_device(b, await personal_org(b, sara))

    r = await b.client.get("/api/platform/my/devices", headers=sara)
    assert [(d["device_id"], d["access"]) for d in r.json()] == [(device, "owner")]
    r = await b.client.get("/api/platform/my/devices", headers=omid)
    assert r.json() == []
    for method, url, body in (("GET", f"/api/platform/devices/{device}/shares", None),
                              ("POST", f"/api/platform/devices/{device}/shares",
                               {"email": "omid@example.com", "level": "operator"})):
        r = await b.client.request(method, url, headers=omid, json=body)
        assert r.status_code == 404, (method, r.text)

    r = await b.client.post(f"/api/platform/devices/{device}/shares", headers=sara,
                            json={"email": "omid@example.com", "level": "viewer"})
    assert r.status_code == 201, r.text
    token = last_link(b, "omid@example.com", "invitation")
    r = await b.client.post("/api/account/invitations/accept", headers=sara,
                            json={"token": token})
    assert r.status_code == 403 and r.json()["error"] == "invitation_other_email"
    r = await b.client.get("/api/account/invitations", headers=omid)
    assert [i["device_id"] for i in r.json()] == [device]
    r = await b.client.post("/api/account/invitations/accept", headers=omid,
                            json={"token": token})
    assert r.status_code == 200 and r.json()["kind"] == "device", r.text
    r = await b.client.post("/api/account/invitations/accept", headers=omid,
                            json={"token": token})
    assert r.status_code == 404                                       # used once

    r = await b.client.get("/api/platform/my/devices", headers=omid)
    assert [(d["device_id"], d["access"]) for d in r.json()] == [(device, "viewer")]
    r = await b.client.post(f"/api/platform/devices/{device}/shares", headers=omid,
                            json={"email": "third@example.com", "level": "viewer"})
    assert r.status_code == 404                                       # no re-sharing
    r = await b.client.get(f"/api/platform/devices/{device}/shares", headers=sara)
    (share,) = r.json()["shares"]
    assert share["level"] == "viewer" and share["org_id"] == await personal_org(b, omid)

    r = await b.client.delete(f"/api/platform/devices/{device}/shares/{share['access_id']}",
                              headers=sara)
    assert r.status_code == 204
    r = await b.client.get("/api/platform/my/devices", headers=omid)
    assert r.json() == []
    # the manufacturer still does not see who owns or uses its device
    r = await b.client.get(f"/api/platform/devices/{device}/shares", headers=omid)
    assert r.status_code == 404
    events = {a.event for a in await b.store.list_auth_audit()}
    assert {"invitation_created", "device_shared", "device_share_revoked"} <= events


async def test_account_rate_limits(backend: Backend) -> None:
    b = backend
    b.services.accounts.settings = b.services.accounts.settings.model_copy(
        update={"account_ip_per_hour": 3})
    for _ in range(3):
        r = await b.client.post("/api/account/password/forgot",
                                json={"email": "x@example.com"})
        assert r.status_code == 202
    r = await b.client.post("/api/account/password/forgot", json={"email": "x@example.com"})
    assert r.status_code == 429 and r.json()["error"] == "account_throttled"
    # at most account_email_per_hour e-mails to one address, whatever the client
    b.services.accounts.settings = b.services.accounts.settings.model_copy(
        update={"account_ip_per_hour": 100})
    await account(b, "lim@example.com", "Lim")
    before = len(outbox(b))
    for _ in range(5):
        await b.client.post("/api/account/password/forgot", json={"email": "lim@example.com"})
    assert len(outbox(b)) - before <= 3
