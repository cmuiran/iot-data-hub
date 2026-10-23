"""hub-api serves the dashboard at / with its own content security policy."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.app import create_app
from tests.api.conftest import Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


async def test_dashboard_is_served_with_a_strict_csp(backend: Backend) -> None:
    r = await backend.client.get("/")
    assert r.status_code == 200 and '<div id="app"' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "connect-src 'self' ws://hub" in csp        # the live WebSocket of this host only
    assert "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-cache"     # revalidated, so updates arrive
    assert r.headers["x-frame-options"] == "DENY"

    js = await backend.client.get("/js/main.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    fa = await backend.client.get("/i18n/fa.json")
    assert fa.json()["app"]["name"] == "هاب داده"


async def test_api_keeps_its_own_rules(backend: Backend) -> None:
    r = await backend.client.get("/api/no-such-route")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    missing = await backend.client.get("/js/nothing-here.js")
    assert missing.status_code == 404


async def test_dashboard_can_be_switched_off(backend: Backend) -> None:
    settings = backend.services.settings
    backend.services.settings = settings.model_copy(update={"frontend_dir": None})
    try:
        app = create_app(backend.services)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub") as client:
            assert (await client.get("/")).status_code == 404
            assert (await client.get("/api/health")).status_code in (200, 503)
    finally:
        backend.services.settings = settings


async def test_missing_index_serves_the_api_only(backend: Backend, tmp_path: Path) -> None:
    settings = backend.services.settings
    backend.services.settings = settings.model_copy(update={"frontend_dir": tmp_path})
    try:
        app = create_app(backend.services)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://hub") as client:
            assert (await client.get("/")).status_code == 404
    finally:
        backend.services.settings = settings


async def test_system_info_for_any_signed_in_user(backend: Backend) -> None:
    await backend.user("view.one", ("viewer", "kish", "gate"))
    headers = await backend.headers("view.one")
    r = await backend.client.get("/api/system/info", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "Asia/Tehran" and body["control_enabled"] is True
    assert {"version", "node", "role", "session_idle_minutes", "password_min_length"} <= set(body)
    assert (await backend.client.get("/api/system/info")).status_code == 401


async def test_system_config_lists_channel_names_only(backend: Backend) -> None:
    await backend.user("eng.one", ("engineer", None, None))
    r = await backend.client.get("/api/system/config", headers=await backend.headers("eng.one"))
    assert r.status_code == 200
    assert r.json()["notifications"] == {"channels": {}, "rules": []}
