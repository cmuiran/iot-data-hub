"""Phase 1 security: MFA, idle sessions, password history/expiry, network allowlist, rate
limit, security headers, WebSocket origin, tamper-evident audit, certificate expiry."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest

from backend import totp
from backend.app import create_app
from hub.drivers.base import utcnow
from hub.pki import make_tls_certificate
from tests.api.conftest import PASSWORD, Backend, make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
DATA_KEY = "d" * 40


def with_settings(b: Backend, **overrides: Any) -> None:
    s = b.services.settings.model_copy(update=overrides)
    b.services.settings = s
    b.services.auth.settings = s


def code_now(secret: str, offset_steps: int = 0) -> str:
    return totp.code_at(secret, totp.step_of(time.time()) + offset_steps)


async def enrol(b: Backend, username: str) -> tuple[str, list[str], dict[str, str]]:
    h = await b.headers(username)
    setup = (await b.client.post("/api/auth/mfa/setup", headers=h)).json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    r = await b.client.post("/api/auth/mfa/enable", headers=h, json={"code": code_now(secret)})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"], h


# --- MFA ------------------------------------------------------------------------------------


async def test_mfa_needs_a_data_key(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    r = await backend.client.post("/api/auth/mfa/setup", headers=await backend.headers("op.one"))
    assert r.status_code == 503 and r.json()["error"] == "no_data_key"


async def test_mfa_login_flow(backend: Backend) -> None:
    with_settings(backend, data_key=DATA_KEY)
    await backend.user("op.one", ("operator", None, None))
    secret, recovery, _ = await enrol(backend, "op.one")
    assert len(recovery) == 10

    r = await backend.client.post("/api/auth/login",
                                  json={"username": "op.one", "password": PASSWORD})
    challenge = r.json()
    assert challenge["mfa_required"] is True and "access_token" not in challenge

    # the code used for enabling cannot be replayed; the next step's code is fine
    code = code_now(secret, 1)
    ok = await backend.client.post("/api/auth/mfa/verify",
                                   json={"mfa_token": challenge["mfa_token"], "code": code})
    assert ok.status_code == 200 and ok.json()["access_token"]
    h = {"Authorization": f"Bearer {ok.json()['access_token']}"}
    me = (await backend.client.get("/api/auth/me", headers=h)).json()
    assert me["mfa_enabled"] is True
    replay = await backend.client.post("/api/auth/mfa/verify",
                                       json={"mfa_token": challenge["mfa_token"], "code": code})
    assert replay.status_code == 401 and replay.json()["error"] == "invalid_mfa_code"

    # recovery code works exactly once
    challenge = (await backend.client.post("/api/auth/login", json={
        "username": "op.one", "password": PASSWORD})).json()
    r = await backend.client.post("/api/auth/mfa/verify", json={
        "mfa_token": challenge["mfa_token"], "recovery_code": recovery[0].upper()})
    assert r.status_code == 200
    r = await backend.client.post("/api/auth/mfa/verify", json={
        "mfa_token": challenge["mfa_token"], "recovery_code": recovery[0]})
    assert r.status_code == 401

    # the access token is not an MFA token and vice versa
    bad = await backend.client.post("/api/auth/mfa/verify", json={
        "mfa_token": ok.json()["access_token"], "code": code})
    assert bad.status_code == 401
    events = {a.event for a in await backend.store.list_auth_audit()}
    assert {"mfa_enabled", "mfa_failed", "login_ok"} <= events


async def test_wrong_mfa_codes_lock_the_account(backend: Backend) -> None:
    with_settings(backend, data_key=DATA_KEY)      # login_max_failures = 3 in the fixture
    await backend.user("op.one", ("operator", None, None))
    await enrol(backend, "op.one")
    challenge = (await backend.client.post("/api/auth/login", json={
        "username": "op.one", "password": PASSWORD})).json()
    for _ in range(3):
        await backend.client.post("/api/auth/mfa/verify", json={
            "mfa_token": challenge["mfa_token"], "code": "000000"})
    r = await backend.client.post("/api/auth/login",
                                  json={"username": "op.one", "password": PASSWORD})
    assert r.json()["error"] == "account_locked"


async def test_mfa_required_role_blocks_until_enrolled(backend: Backend) -> None:
    with_settings(backend, data_key=DATA_KEY, mfa_required_roles=["admin"])
    await backend.user("admin", ("admin", None, None))
    await backend.user("viewer", ("viewer", None, None))
    h = await backend.headers("admin")
    r = await backend.client.get("/api/users", headers=h)
    assert r.status_code == 403 and r.json()["error"] == "mfa_enrollment_required"
    me = (await backend.client.get("/api/auth/me", headers=h)).json()
    assert me["mfa_enrollment_required"] is True
    assert (await backend.client.get("/api/devices",
                                     headers=await backend.headers("viewer"))).status_code == 200
    secret, _, h = await enrol(backend, "admin")
    challenge = (await backend.client.post("/api/auth/login", json={
        "username": "admin", "password": PASSWORD})).json()
    tokens = (await backend.client.post("/api/auth/mfa/verify", json={
        "mfa_token": challenge["mfa_token"], "code": code_now(secret, 1)})).json()
    r = await backend.client.get("/api/users",
                                 headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200


async def test_disable_and_admin_reset(backend: Backend) -> None:
    with_settings(backend, data_key=DATA_KEY)
    await backend.user("admin", ("admin", None, None))
    uid = await backend.user("op.one", ("operator", None, None))
    secret, _, h = await enrol(backend, "op.one")
    r = await backend.client.post("/api/auth/mfa/disable", headers=h,
                                  json={"password": "wrong-pass", "code": code_now(secret, 1)})
    assert r.status_code == 401
    admin = await backend.headers("admin")
    assert (await backend.client.post(f"/api/users/{uid}/mfa/reset", headers=admin)
            ).status_code == 204
    r = await backend.client.post("/api/auth/login",
                                  json={"username": "op.one", "password": PASSWORD})
    assert "access_token" in r.json()                 # MFA gone after the reset
    assert any(a.event == "mfa_reset" for a in await backend.store.list_auth_audit())


async def test_lost_data_key_is_a_clear_error(backend: Backend) -> None:
    with_settings(backend, data_key=DATA_KEY)
    await backend.user("op.one", ("operator", None, None))
    await enrol(backend, "op.one")
    with_settings(backend, data_key="e" * 40)        # key changed: secret cannot be read
    challenge = (await backend.client.post("/api/auth/login", json={
        "username": "op.one", "password": PASSWORD})).json()
    r = await backend.client.post("/api/auth/mfa/verify", json={
        "mfa_token": challenge["mfa_token"], "code": "123456"})
    assert r.status_code == 503 and r.json()["error"] == "mfa_unreadable"


# --- sessions and passwords -----------------------------------------------------------------


async def test_idle_session_expires(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    tokens = await backend.login("op.one")
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await backend.client.get("/api/devices", headers=h)).status_code == 200
    later = utcnow() + timedelta(minutes=31)          # idle limit 30 min
    backend.services.auth.clock = lambda: later
    r = await backend.client.get("/api/devices", headers=h)
    assert r.status_code == 401 and r.json()["error"] == "session_idle"
    r = await backend.client.post("/api/auth/refresh",
                                  json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    assert any(a.event == "session_expired" for a in await backend.store.list_auth_audit())


async def test_password_history_and_expiry(backend: Backend) -> None:
    uid = await backend.user("op.one", ("operator", None, None))
    h = await backend.headers("op.one")
    r = await backend.client.post("/api/auth/change-password", headers=h, json={
        "current_password": PASSWORD, "new_password": "Second-Secret-77"})
    assert r.status_code == 204
    h = {"Authorization": "Bearer " + (await backend.login("op.one", "Second-Secret-77"))[
        "access_token"]}
    r = await backend.client.post("/api/auth/change-password", headers=h, json={
        "current_password": "Second-Secret-77", "new_password": PASSWORD})
    assert r.status_code == 422 and r.json()["error"] == "password_reused"

    with_settings(backend, password_max_age_days=90)
    await backend.store.update_user(uid, password_changed_at=utcnow() - timedelta(days=91))
    tokens = await backend.login("op.one", "Second-Secret-77")
    assert tokens["must_change_password"] is True
    assert any(a.event == "password_expired" for a in await backend.store.list_auth_audit())


# --- network guards and headers -------------------------------------------------------------


async def client_for(b: Backend) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(b.services), client=("10.1.2.3", 50000))
    return httpx.AsyncClient(transport=transport, base_url="http://hub")


async def test_network_allowlist(backend: Backend) -> None:
    backend.services.settings = make_settings(allowed_networks=["192.168.0.0/16"])
    async with await client_for(backend) as c:
        r = await c.get("/api/devices")
        assert r.status_code == 403 and r.json()["error"] == "network_not_allowed"
        assert (await c.get("/api/health")).status_code == 200
    backend.services.settings = make_settings(allowed_networks=["10.0.0.0/8"])
    async with await client_for(backend) as c:
        assert (await c.get("/api/devices")).status_code == 401   # allowed; needs a token


async def test_rate_limit(backend: Backend) -> None:
    backend.services.settings = make_settings(rate_limit_per_min=10)
    async with await client_for(backend) as c:
        codes = [(await c.get("/api/devices")).status_code for _ in range(12)]
        assert codes[:10] == [401] * 10 and codes[10] == 429
        r = await c.get("/api/devices")
        assert int(r.headers["retry-after"]) > 0
        assert (await c.get("/api/health")).status_code == 200   # health is not limited


async def test_security_headers(backend: Backend, tmp_path: Any) -> None:
    r = await backend.client.get("/api/health")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in r.headers
    cert, key = make_tls_certificate(tmp_path, ["127.0.0.1"])
    backend.services.settings = make_settings(tls_cert_file=cert, tls_key_file=key)
    async with await client_for(backend) as c:
        assert "max-age=31536000" in (await c.get("/api/health")).headers[
            "strict-transport-security"]


def test_websocket_origin_check() -> None:
    from backend.guards import origin_allowed

    s = make_settings(cors_origins=["https://dash.kish.local"])
    assert origin_allowed(None, "hub:8000", s)                           # not a browser
    assert origin_allowed("http://hub:8000", "hub:8000", s)              # same host
    assert origin_allowed("https://dash.kish.local", "hub:8000", s)      # configured
    assert not origin_allowed("https://evil.example", "hub:8000", s)


# --- audit chain ----------------------------------------------------------------------------


async def test_audit_chain_detects_tampering(backend: Backend, database_url: str) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    await backend.user("auditor", ("engineer", None, None))
    h = await backend.headers("op.gate")
    for value in (210.0, 220.0, 230.0):
        await backend.client.post("/api/control/write", headers=h, json={
            "tag": "kish.gate.meter_02.demand_limit", "value": value})
    a = await backend.headers("auditor")
    ok = (await backend.client.get("/api/audit/verify", headers=a)).json()
    assert ok["write_audit"]["ok"] and ok["write_audit"]["checked"] == 6
    assert ok["auth_audit"]["ok"]

    with psycopg.connect(database_url, autocommit=True) as conn:   # a malicious DBA
        conn.execute("ALTER TABLE write_audit DISABLE TRIGGER write_audit_append_only")
        conn.execute("UPDATE write_audit SET requested_value = '999' WHERE chain_seq = 3")
    broken = (await backend.client.get("/api/audit/verify", headers=a)).json()["write_audit"]
    assert not broken["ok"] and broken["problem"] == "row content was changed"

    report = await backend.services.security_watch.check_audit(utcnow())
    assert not report["write_audit"]["ok"]
    alarms = (await backend.client.get("/api/alarms", headers=a)).json()
    assert any(x["source"] == "system:audit" and x["severity"] == "critical" for x in alarms)

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM write_audit WHERE chain_seq = 3")
    gap = (await backend.client.get("/api/audit/verify", headers=a)).json()["write_audit"]
    assert not gap["ok"] and "gap" in gap["problem"]
    anchors = await backend.store.events("audit_anchor", 5)
    assert anchors and anchors[0]["detail"]["heads"]["write_audit"]


# --- certificates ---------------------------------------------------------------------------


async def test_certificate_expiry_alarm(backend: Backend, tmp_path: Any) -> None:
    from hub.pki import make_tls_certificate as mk

    cert, key = mk(tmp_path / "tls", ["127.0.0.1"], days=10)
    backend.services.settings = make_settings(tls_cert_file=cert, tls_key_file=key,
                                              pki_dir=tmp_path / "pki", cert_warn_days=30)
    watch = backend.services.security_watch
    report = await watch.check_certificates(utcnow())
    assert [r["days_left"] for r in report] == [9]
    active = await backend.store.active_alarms()
    alarm = next(a for a in active if a.source == f"system:cert:{cert.name}")
    assert alarm.severity == "high" and "expires in 9 days" in alarm.message

    mk(tmp_path / "tls", ["127.0.0.1"], days=825)     # renewed in place
    await watch.check_certificates(utcnow())
    open_sources = await backend.store.open_alarm_sources()
    assert f"system:cert:{cert.name}" not in open_sources
