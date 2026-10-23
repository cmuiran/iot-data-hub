"""/api/v1 is the stable API; /api is the current-version alias; unknown versions are refused."""

from __future__ import annotations

import pytest

from tests.api.conftest import PASSWORD, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


async def test_v1_and_alias_answer_the_same(backend: Backend) -> None:
    for path in ("/api/health", "/api/v1/health"):
        r = await backend.client.get(path)
        assert r.status_code == 200 and r.json()["status"] == "ok"
        assert r.headers["api-version"] == "1"
    await backend.user("op.one", ("operator", None, None))
    login = await backend.client.post("/api/v1/auth/login",
                                      json={"username": "op.one", "password": PASSWORD})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    a = await backend.client.get("/api/v1/tags", headers=headers)
    b = await backend.client.get("/api/tags", headers=headers)
    assert a.status_code == b.status_code == 200
    assert [t["tag"] for t in a.json()] == [t["tag"] for t in b.json()]


async def test_versions_and_unsupported(backend: Backend) -> None:
    r = await backend.client.get("/api/versions")
    assert r.json()["current"] == "1" and r.json()["supported"] == ["1"]
    assert r.json()["base"] == "/api/v1" and r.json()["hub"]
    r = await backend.client.get("/api/v2/health")
    assert r.status_code == 404 and r.json()["error"] == "unsupported_api_version"
    # the dashboard (not under /api) carries no API version
    page = await backend.client.get("/")
    assert "api-version" not in page.headers


async def test_guards_apply_to_v1(backend: Backend) -> None:
    r = await backend.client.get("/api/v1/users")
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"
