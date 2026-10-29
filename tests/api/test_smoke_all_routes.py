"""Smoke test of the whole API: every route, called by four kinds of user (platform admin,
manufacturer, customer owner, outsider) with real ids, with unknown ids, and with empty or
wrong bodies, must never answer a server error (5xx). Catches unhandled exceptions anywhere."""

from __future__ import annotations

import re
from typing import Any

import pytest

from backend.settings import Settings
from tests.api.conftest import Backend, make_settings
from tests.api.test_platform_pentest import _routes, victim

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
SKIP = {("POST", "/api/auth/logout"), ("POST", "/api/system/backup"),
        ("POST", "/api/system/restore-test"),            # end the session / slow host jobs
        ("GET", "/api/platform/my/stream"),               # never ends (tested on its own)
        ("POST", "/api/account/delete")}                  # would delete the test users
UNKNOWN = {"org_id": "o_unknown", "product_id": "00000000-0000-0000-0000-000000000000",
           "device_id": "dzzzzzzzzzzzzzzzzzzzz", "hub_id": "dzzzzzzzzzzzzzzzzzzzz",
           "child_id": "dzzzzzzzzzzzzzzzzzzzz", "scene_id": 999999, "automation_id": 999999,
           "holiday_id": 999999, "release_id": 999999, "version": 999, "access_id": 999999,
           "user_role_id": 999999, "invitation_id": 999999, "alarm_id": 999999,
           "channel": "nope", "dashboard_id": 999999, "equipment_id": "nope",
           "location_id": "nope", "request_id": "00000000-0000-0000-0000-000000000000",
           "schedule_id": 999999, "session_id": "00000000-0000-0000-0000-000000000000",
           "snapshot_id": "nope", "source": "kish.x.y.z", "tag_path": "kish.x.y.z",
           "user_id": 999999, "group_id": 999999, "ticket_id": 999999,
           "invoice_id": 999999, "key": "nope"}


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token="h" * 32,
                         data_key="k" * 40)          # so MFA and backups run their real code


def _fill(path: str, ids: dict[str, Any]) -> str | None:
    names = re.findall(r"\{(\w+)\}", path)
    if any(n not in ids for n in names):
        return None
    return re.sub(r"\{(\w+)\}", lambda m: str(ids[m[1]]), path)


async def test_no_route_answers_a_server_error(backend: Backend) -> None:
    b = backend
    eve, ids = await victim(b)
    from tests.api.conftest import GATE_METER

    ids = ids | {"alarm_id": 1, "channel": "email", "dashboard_id": 1,
                 "equipment_id": "gate_meter", "location_id": "kish_campus",
                 "request_id": "00000000-0000-0000-0000-000000000001", "schedule_id": 1,
                 "session_id": "00000000-0000-0000-0000-000000000001", "snapshot_id": "x",
                 "source": f"{GATE_METER}.voltage_l1", "tag_path": f"{GATE_METER}.voltage_l1",
                 "user_id": 1}
    platform_tag = f"platform.devices.{ids['device_id']}.relay_1"
    users = {"eve": eve}
    for name in ("boss", "alice.x", "sara@example.com"):
        login = name if "@" in name else name
        r = await b.client.post("/api/auth/login", json={"username": login,
                                                         "password": "Correct-Horse-42"})
        assert r.status_code == 200, (name, r.text)
        users[name] = {"Authorization": f"Bearer {r.json()['access_token']}"}
    routes = [r for r in _routes(b, "/api/") if r not in SKIP] + _routes(b, "/ingest/")
    assert len(routes) > 120
    bodies: list[Any] = [{}, {"name": ""}, [], "x"]
    failures = []
    calls = 0
    for method, path in routes:
        for values in (ids, ids | {"tag_path": platform_tag, "source": platform_tag}, UNKNOWN):
            url = _fill(path, values)
            if url is None:
                continue
            for who, headers in users.items():
                for body in (bodies if method in ("POST", "PUT", "PATCH") else [None]):
                    r = await b.client.request(method, url, headers=headers, json=body)
                    calls += 1
                    if r.status_code >= 500:
                        failures.append(f"{r.status_code} {method} {path} as {who} "
                                        f"body={body!r}: {r.text[:200]}")
    assert calls > 1000
    assert failures == [], "\n".join(failures[:40])
