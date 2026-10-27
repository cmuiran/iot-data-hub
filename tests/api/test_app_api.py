"""Platform phase 5: the consumer app is served at /app under the dashboard's CSP, and its
device view (template + live values + what the caller may operate)."""

from __future__ import annotations

import pytest

from backend.settings import Settings
from tests.api.conftest import Backend, make_settings
from tests.api.test_accounts_api import last_link
from tests.api.test_ingest_api import owned_device

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token="h" * 32)


async def test_app_is_served(backend: Backend) -> None:
    r = await backend.client.get("/app/")
    assert r.status_code == 200 and '<div id="app"' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe" not in csp
    for path in ("/app/js/main.js", "/app/css/app.css", "/app/i18n/fa.json", "/app/sw.js",
                 "/app/manifest.webmanifest"):
        assert (await backend.client.get(path)).status_code == 200, path


async def test_device_view(backend: Backend) -> None:
    b = backend
    _, sara, omid, device_id, secret = await owned_device(b)
    url = f"/api/platform/my/devices/{device_id}/view"
    r = await b.client.get(url, headers=sara)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["device"]["access"] == "owner" and view["product"] == "Smart 8-relay"
    tags = {t["key"]: t for t in view["tags"]}
    assert list(tags)[:2] == ["relay_1", "relay_2"]             # template order
    assert tags["relay_1"]["writable"] and not tags["power"]["writable"]
    assert tags["relay_1"]["display_name"] == {"fa": "رله 1"}
    assert tags["power"]["unit"] == "kW" and tags["power"]["value"] is None

    r = await b.client.post(f"/ingest/v1/devices/{device_id}/telemetry",
                            headers={"Authorization": f"Bearer {secret}"},
                            json={"values": {"power": 2.5, "relay_1": True}})
    assert r.status_code == 200
    tags = {t["key"]: t for t in (await b.client.get(url, headers=sara)).json()["tags"]}
    assert tags["power"]["value"] == 2.5 and tags["relay_1"]["value"] is True

    assert (await b.client.get(url, headers=omid)).status_code == 404
    await b.client.post(f"/api/platform/devices/{device_id}/shares", headers=sara,
                        json={"email": "omid@example.com", "level": "viewer"})
    await b.client.post("/api/account/invitations/accept", headers=omid,
                        json={"token": last_link(b, "omid@example.com", "invitation")})
    view = (await b.client.get(url, headers=omid)).json()
    assert view["device"]["access"] == "viewer"
    assert not any(t["writable"] for t in view["tags"])
    me = (await b.client.get("/api/auth/me", headers=omid)).json()
    assert me["email"] == "omid@example.com"
