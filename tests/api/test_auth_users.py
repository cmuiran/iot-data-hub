from __future__ import annotations

import pytest

from tests.api.conftest import PASSWORD, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


async def test_login_me_and_permissions(backend: Backend) -> None:
    await backend.user("op.gate", ("operator", "kish", "gate"))
    tokens = await backend.login("op.gate")
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == 15 * 60
    me = (await backend.client.get("/api/auth/me", headers={
        "Authorization": f"Bearer {tokens['access_token']}"})).json()
    assert me["username"] == "op.gate"
    assert me["permissions"]["tag.write"] == [{"site": "kish", "area": "gate"}]
    assert "user.manage" not in me["permissions"]
    audit = await backend.store.list_auth_audit(event="login_ok")
    assert audit[0].username == "op.gate" and audit[0].client_addr == "10.1.2.3"


async def test_requests_without_valid_token(backend: Backend) -> None:
    for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic x"}):
        r = await backend.client.get("/api/devices", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"] in ("not_authenticated", "invalid_token")


async def test_wrong_password_and_lockout(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    for _ in range(3):
        r = await backend.client.post("/api/auth/login",
                                      json={"username": "op.one", "password": "wrong-password"})
        assert r.status_code == 401 and r.json()["error"] == "invalid_credentials"
    r = await backend.client.post("/api/auth/login",
                                  json={"username": "op.one", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["error"] == "account_locked"
    events = [a.event for a in await backend.store.list_auth_audit()]
    assert "locked" in events

    await backend.user("admin", ("admin", None, None))
    admin = await backend.headers("admin")
    uid = (await backend.store.get_user_by_username("op.one")).user_id  # type: ignore[union-attr]
    assert (await backend.client.post(f"/api/users/{uid}/unlock", headers=admin)).status_code == 204
    assert (await backend.login("op.one"))["access_token"]


async def test_unknown_user_looks_like_wrong_password(backend: Backend) -> None:
    r = await backend.client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_credentials"


async def test_refresh_rotation_and_reuse_detection(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    first = await backend.login("op.one")
    r = await backend.client.post("/api/auth/refresh",
                                  json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    headers2 = {"Authorization": f"Bearer {second['access_token']}"}
    assert (await backend.client.get("/api/devices", headers=headers2)).status_code == 200

    # the old refresh token is replayed: every session of the user is revoked
    r = await backend.client.post("/api/auth/refresh",
                                  json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    assert (await backend.client.get("/api/devices", headers=headers2)).status_code == 401


async def test_logout_ends_the_session(backend: Backend) -> None:
    await backend.user("op.one", ("operator", None, None))
    headers = await backend.headers("op.one")
    assert (await backend.client.post("/api/auth/logout", headers=headers)).status_code == 204
    assert (await backend.client.get("/api/devices", headers=headers)).status_code == 401


async def test_must_change_password_first(backend: Backend) -> None:
    await backend.user("new.user", ("viewer", None, None), must_change=True)
    tokens = await backend.login("new.user")
    assert tokens["must_change_password"] is True
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await backend.client.get("/api/devices", headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "password_change_required"
    r = await backend.client.post("/api/auth/change-password", headers=headers,
                                  json={"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 422 and r.json()["error"] == "weak_password"
    r = await backend.client.post("/api/auth/change-password", headers=headers, json={
        "current_password": PASSWORD, "new_password": "Brand-New-Secret-7"})
    assert r.status_code == 204
    assert (await backend.client.get("/api/devices", headers=headers)).status_code == 401
    fresh = await backend.login("new.user", "Brand-New-Secret-7")
    assert fresh["must_change_password"] is False
    fresh = {"Authorization": f"Bearer {fresh['access_token']}"}
    assert (await backend.client.get("/api/devices", headers=fresh)).status_code == 200


# --- user management ------------------------------------------------------------------------


async def test_admin_manages_users(backend: Backend) -> None:
    await backend.user("admin", ("admin", None, None))
    admin = await backend.headers("admin")
    r = await backend.client.post("/api/users", headers=admin, json={
        "username": "op.pump", "display_name": "Pump operator", "password": "Pump-Operator-9",
        "roles": [{"role": "operator", "site": "kish", "area": "pump_station"}]})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["must_change_password"] is True
    assert [(g["role"], g["area"]) for g in created["roles"]] == [("operator", "pump_station")]

    dup = await backend.client.post("/api/users", headers=admin, json={
        "username": "op.pump", "display_name": "x", "password": "Pump-Operator-9"})
    assert dup.status_code == 409 and dup.json()["error"] == "username_taken"
    weak = await backend.client.post("/api/users", headers=admin, json={
        "username": "weak.one", "display_name": "x", "password": "aaaaaaaaaaaa"})
    assert weak.status_code == 422 and weak.json()["error"] == "weak_password"
    bad_role = await backend.client.post(f"/api/users/{created['user_id']}/roles",
                                         headers=admin, json={"role": "superuser"})
    assert bad_role.status_code == 422

    uid = created["user_id"]
    grant = await backend.client.post(f"/api/users/{uid}/roles", headers=admin,
                                      json={"role": "viewer", "site": "kish"})
    assert grant.status_code == 201
    r = await backend.client.delete(f"/api/users/{uid}/roles/{grant.json()['user_role_id']}",
                                    headers=admin)
    assert r.status_code == 204

    r = await backend.client.patch(f"/api/users/{uid}", headers=admin, json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    events = {a.event for a in await backend.store.list_auth_audit()}
    assert {"user_created", "role_granted", "role_revoked", "user_disabled"} <= events


async def test_disabled_user_loses_access_immediately(backend: Backend) -> None:
    await backend.user("admin", ("admin", None, None))
    uid = await backend.user("op.one", ("operator", None, None))
    op = await backend.headers("op.one")
    admin = await backend.headers("admin")
    await backend.client.patch(f"/api/users/{uid}", headers=admin, json={"is_active": False})
    assert (await backend.client.get("/api/devices", headers=op)).status_code == 401


async def test_last_admin_cannot_be_removed(backend: Backend) -> None:
    uid = await backend.user("admin", ("admin", None, None))
    admin = await backend.headers("admin")
    r = await backend.client.patch(f"/api/users/{uid}", headers=admin, json={"is_active": False})
    assert r.status_code == 409 and r.json()["error"] == "self_disable"
    grant = (await backend.store.grants(uid))[0]
    r = await backend.client.delete(f"/api/users/{uid}/roles/{grant.user_role_id}", headers=admin)
    assert r.status_code == 409 and r.json()["error"] == "last_admin"
    # with a second admin it works
    await backend.user("admin2", ("admin", None, None))
    r = await backend.client.delete(f"/api/users/{uid}/roles/{grant.user_role_id}", headers=admin)
    assert r.status_code == 204


async def test_non_admin_cannot_manage_users(backend: Backend) -> None:
    await backend.user("eng.one", ("engineer", None, None))
    r = await backend.client.get("/api/users", headers=await backend.headers("eng.one"))
    assert r.status_code == 403
