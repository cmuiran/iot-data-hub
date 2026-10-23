"""HTTP/REST driver against the HTTP simulator (uvicorn), including HTTPS with a plant CA."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hub.drivers.base import DeviceState, WriteGrant, WriteNotPermitted
from hub.drivers.http_json import HttpJsonDriver
from hub.models import HttpDevice, Quality
from simulators.http_sim import HttpSimServer, SimulatedHttpDevice
from tests.drivers.helpers import Recorder, device
from tests.tls_helpers import PlantCA

pytestmark = pytest.mark.integration


def inverter(base_url: str, **conn: Any) -> HttpDevice:
    return device({
        "device_id": "inv_01", "name": "Inverter", "site": "kish", "area": "solar",
        "protocol": "http", "poll_interval_ms": 150,
        "connection": {"base_url": base_url, "timeout_ms": 1000} | conn,
        "tags": [
            {"name": "power", "unit": "kW", "data_type": "float32", "path": "/api/status",
             "json_path": "$.ac.power", "min": 0, "max": 50},
            {"name": "state", "data_type": "uint16", "path": "/api/status",
             "json_path": "$.state", "states": {0: "off", 1: "on"}},
            {"name": "first_string", "data_type": "float32", "path": "/api/strings",
             "json_path": "$.list[0].v"},
            {"name": "alarm", "data_type": "bool", "path": "/api/strings",
             "json_path": "$['alarm flag']"},
        ],
    })


async def run(dev: HttpDevice, rec: Recorder) -> HttpJsonDriver:
    driver = HttpJsonDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = 0.1  # noqa: SLF001
    await driver.start()
    return driver


@pytest.mark.parametrize(("auth", "sim_auth", "env"), [
    ({"type": "none"}, {}, {}),
    ({"type": "basic", "username_env": "HUB_T_USER", "password_env": "HUB_T_PW"},
     {"basic": "hub:s3cret"}, {"HUB_T_USER": "hub", "HUB_T_PW": "s3cret"}),
    ({"type": "bearer", "token_env": "HUB_T_TOKEN"}, {"bearer": "tok-1"}, {"HUB_T_TOKEN": "tok-1"}),
    ({"type": "header", "header": "X-Api-Key", "value_env": "HUB_T_KEY"},
     {"header": ("X-Api-Key", "k-9")}, {"HUB_T_KEY": "k-9"}),
])
async def test_reads_with_each_auth(auth: dict[str, Any], sim_auth: dict[str, Any],
                                    env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    async with HttpSimServer() as srv:
        dev = inverter(srv.base_url("inv_01"), auth=auth,
                       timestamp_path="$.ts", timestamp_format="unix_ms")
        sim = srv.add(SimulatedHttpDevice(dev, auth=sim_auth))
        sim.values.update(power=12.5, state=1, first_string=3.25, alarm=True)
        rec = Recorder()
        driver = await run(dev, rec)
        try:
            await rec.wait_for(lambda: len(rec.latest()) == 4 and rec.state is DeviceState.ONLINE)
            v = {k: s.value for k, s in rec.latest().items()}
            assert v == {"power": 12.5, "state": 1, "first_string": 3.25, "alarm": True}
            assert all(s.quality is Quality.GOOD for s in rec.latest().values())
            # two paths, one request each per poll
            polls = sim.requests
            assert polls >= 2 and polls % 2 == 0 or polls > 2
            assert await driver.read_tag("power") == 12.5
            with pytest.raises(WriteNotPermitted):
                await driver.write_tag("power", WriteGrant("t", dev.tag_path("power"), 1.0))
        finally:
            await driver.stop()


async def test_wrong_credentials_and_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_T_TOKEN", "wrong")
    async with HttpSimServer() as srv:
        dev = inverter(srv.base_url("inv_01"), auth={"type": "bearer", "token_env": "HUB_T_TOKEN"})
        sim = srv.add(SimulatedHttpDevice(dev, auth={"bearer": "right"}))
        rec = Recorder()
        driver = await run(dev, rec)
        try:
            await rec.wait_for(lambda: rec.state is DeviceState.ERROR and "power" in rec.latest())
            assert "HTTP 401" in (rec.statuses[-1].last_error or "")
            assert rec.latest()["power"].quality is Quality.BAD
            monkeypatch.setenv("HUB_T_TOKEN", "right")   # read at connect: restart the driver
            await driver.stop()
            rec2 = Recorder()
            driver = await run(dev, rec2)
            await rec2.wait_for(lambda: rec2.state is DeviceState.ONLINE)
            sim.status = 503
            await rec2.wait_for(lambda: rec2.state is DeviceState.ERROR)
            assert "HTTP 503" in (rec2.statuses[-1].last_error or "")
            sim.status, sim.garbage = 200, True
            await rec2.wait_for(lambda: "not JSON" in (rec2.statuses[-1].last_error or ""))
            sim.garbage = False
            await rec2.wait_for(lambda: rec2.state is DeviceState.ONLINE)
        finally:
            await driver.stop()


async def test_missing_field_is_bad_not_fatal() -> None:
    async with HttpSimServer() as srv:
        dev = inverter(srv.base_url("inv_01"))
        sim = srv.add(SimulatedHttpDevice(dev))
        sim.values["first_string"] = None     # the device omits it
        rec = Recorder()
        driver = await run(dev, rec)
        try:
            await rec.wait_for(lambda: len(rec.latest()) == 4)
            assert rec.latest()["first_string"].quality is Quality.BAD
            assert rec.latest()["power"].quality is Quality.GOOD
        finally:
            await driver.stop()


async def test_unreachable_is_offline() -> None:
    from simulators.mqtt_sim import free_port

    rec = Recorder()
    driver = await run(inverter(f"http://127.0.0.1:{free_port()}"), rec)
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert "Connect" in (rec.statuses[-1].last_error or "")   # refused, or a timeout on Windows
    finally:
        await driver.stop()


async def test_https_with_plant_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ca = PlantCA(tmp_path)
    server_cert = ca.issue("inverter", server_names=["127.0.0.1"])
    async with HttpSimServer(tls=(str(server_cert.cert), str(server_cert.key))) as srv:
        url = srv.base_url("inv_01")
        assert url.startswith("https://")
        dev_ca = inverter(url, ca_file_env="HUB_T_CA")
        srv.add(SimulatedHttpDevice(dev_ca))
        monkeypatch.setenv("HUB_T_CA", str(ca.pair.cert))
        rec = Recorder()
        driver = await run(dev_ca, rec)
        try:
            await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        finally:
            await driver.stop()
        # without the plant CA the certificate is refused (no silent acceptance)
        rec2 = Recorder()
        driver = await run(inverter(url), rec2)
        try:
            await rec2.wait_for(lambda: rec2.state is DeviceState.OFFLINE)
            assert "CERTIFICATE_VERIFY_FAILED" in (rec2.statuses[-1].last_error or "")
        finally:
            await driver.stop()


async def test_missing_secret_env_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_T_NOPE", raising=False)
    rec = Recorder()
    driver = await run(inverter("https://127.0.0.1:9", auth={"type": "bearer",
                                                            "token_env": "HUB_T_NOPE"}), rec)
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert "HUB_T_NOPE is not set" in (rec.statuses[-1].last_error or "")
    finally:
        await driver.stop()
