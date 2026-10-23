from __future__ import annotations

import pytest

from tests.api.conftest import PASSWORD, Backend, make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


# --- IP throttling (item 8) -----------------------------------------------------------------


async def test_login_throttled_per_address(backend: Backend) -> None:
    backend.services.auth.settings = make_settings(login_ip_max_failures=4,
                                                   login_max_failures=100)
    await backend.user("op.one", ("operator", None, None))
    # guessing across many usernames from one address
    for name in ("a1", "b2", "c3", "d4"):
        r = await backend.client.post("/api/auth/login",
                                      json={"username": name, "password": "x"})
        assert r.status_code == 401
    r = await backend.client.post("/api/auth/login",
                                  json={"username": "op.one", "password": PASSWORD})
    assert r.status_code == 429 and r.json()["error"] == "login_throttled"
    assert int(r.headers["retry-after"]) > 0
    assert any(a.event == "login_throttled" for a in await backend.store.list_auth_audit())


async def test_successful_logins_are_not_counted(backend: Backend) -> None:
    backend.services.auth.settings = make_settings(login_ip_max_failures=2)
    await backend.user("op.one", ("operator", None, None))
    for _ in range(5):
        await backend.login("op.one")


# --- sessions (item 9) ----------------------------------------------------------------------


async def test_list_and_end_own_sessions(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    first = await backend.headers("op.one")
    second = await backend.headers("op.one")
    sessions = (await backend.client.get("/api/auth/sessions", headers=second)).json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    other = next(s for s in sessions if not s["current"])
    assert other["client_addr"] == "10.1.2.3"
    r = await backend.client.delete(f"/api/auth/sessions/{other['session_id']}", headers=second)
    assert r.status_code == 204
    assert (await backend.client.get("/api/devices", headers=first)).status_code == 401
    assert (await backend.client.get("/api/devices", headers=second)).status_code == 200


async def test_cannot_end_someone_elses_session(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    await backend.user("op.two", ("operator", None, None))
    one = await backend.headers("op.one")
    two = await backend.headers("op.two")
    sid = (await backend.client.get("/api/auth/sessions", headers=one)).json()[0]["session_id"]
    r = await backend.client.delete(f"/api/auth/sessions/{sid}", headers=two)
    assert r.status_code == 404
    assert (await backend.client.get("/api/devices", headers=one)).status_code == 200


async def test_admin_logs_a_user_out_everywhere(backend: Backend) -> None:
    await backend.user("admin", ("admin", None, None))
    uid = await backend.user("op.one", ("operator", None, None))
    op = await backend.headers("op.one")
    admin = await backend.headers("admin")
    listed = (await backend.client.get(f"/api/users/{uid}/sessions", headers=admin)).json()
    assert len(listed) == 1
    assert (await backend.client.delete(f"/api/users/{uid}/sessions", headers=admin)
            ).status_code == 204
    assert (await backend.client.get("/api/devices", headers=op)).status_code == 401


# --- dashboards (item 10) -------------------------------------------------------------------


async def test_dashboards(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    await backend.user("op.two", ("viewer", None, None))
    one = await backend.headers("op.one")
    two = await backend.headers("op.two")
    layout = {"widgets": [{"type": "gauge", "tag": "kish.gate.meter_02.voltage_l1"}]}
    r = await backend.client.post("/api/dashboards", headers=one,
                                  json={"name": "Gate", "layout": layout, "is_default": True})
    assert r.status_code == 201, r.text
    private = r.json()
    assert private["layout"] == layout and private["owner"] == "op.one"
    dup = await backend.client.post("/api/dashboards", headers=one, json={"name": "Gate"})
    assert dup.status_code == 409

    shared = (await backend.client.post("/api/dashboards", headers=one, json={
        "name": "Plant", "shared": True, "is_default": True})).json()
    mine = (await backend.client.get("/api/dashboards", headers=one)).json()
    assert {d["name"]: d["is_default"] for d in mine} == {"Gate": False, "Plant": True}

    others = (await backend.client.get("/api/dashboards", headers=two)).json()
    assert [d["name"] for d in others] == ["Plant"]
    assert (await backend.client.get(f"/api/dashboards/{private['dashboard_id']}",
                                     headers=two)).status_code == 404
    r = await backend.client.put(f"/api/dashboards/{shared['dashboard_id']}", headers=two,
                                 json={"name": "Hijack"})
    assert r.status_code == 403
    r = await backend.client.put(f"/api/dashboards/{shared['dashboard_id']}", headers=one,
                                 json={"name": "Plant v2", "shared": True,
                                       "layout": {"rows": 2}})
    assert r.status_code == 200 and r.json()["layout"] == {"rows": 2}
    too_big = await backend.client.post("/api/dashboards", headers=one, json={
        "name": "Huge", "layout": {"x": "y" * 300_000}})
    assert too_big.status_code == 422
    assert (await backend.client.delete(f"/api/dashboards/{private['dashboard_id']}",
                                        headers=one)).status_code == 204
