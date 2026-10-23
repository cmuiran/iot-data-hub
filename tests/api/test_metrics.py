from __future__ import annotations

import re

import pytest

from tests.api.conftest import GATE_METER, Backend, make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
TOKEN = "metrics-token-0123456789"


async def test_metrics_disabled_by_default(backend: Backend) -> None:
    assert (await backend.client.get("/metrics")).status_code == 404


async def test_metrics_need_the_token(backend: Backend) -> None:
    backend.services.settings = make_settings(metrics_token=TOKEN)
    assert (await backend.client.get("/metrics")).status_code == 401
    r = await backend.client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


async def test_metrics_content(backend: Backend) -> None:
    backend.services.settings = make_settings(metrics_token=TOKEN)
    await backend.user("op.gate", ("operator", "kish", "gate"))
    h = await backend.headers("op.gate")
    await backend.client.post("/api/control/write", headers=h,
                              json={"tag": f"{GATE_METER}.demand_limit", "value": 250.0})
    r = await backend.client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert re.search(r'^hub_device_up\{device_id="meter_02",protocol="modbus_tcp",'
                     r'site="kish",area="gate"\} 1$', text, re.M)
    assert re.search(r'^hub_control_writes_total\{result="applied"\} 1$', text, re.M)
    assert re.search(r'^hub_logins_total\{outcome="login_ok"\} 1$', text, re.M)
    assert re.search(r"^hub_samples_received_total \d+$", text, re.M)
    assert "# TYPE hub_alarms_active gauge" in text
    for line in text.splitlines():   # every line is a comment or "name{labels} number"
        assert line.startswith("#") or re.fullmatch(r"[a-z_]+(\{.*\})? -?[0-9.e+]+", line), line
