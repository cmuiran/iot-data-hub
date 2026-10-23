"""MQTT over TLS with a plant CA and client certificates (mutual TLS) against amqtt."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

from hub.drivers.base import DeviceState, DriverError
from hub.drivers.mqtt_json import MqttJsonDriver, broker_tls_context
from hub.models import Broker, MqttDevice
from simulators.mqtt_sim import EmbeddedBroker
from tests.drivers.helpers import Recorder, device
from tests.tls_helpers import PlantCA

pytestmark = pytest.mark.integration


def sensor() -> MqttDevice:
    return device({
        "device_id": "th_01", "name": "TH", "site": "kish", "area": "gate", "protocol": "mqtt",
        "connection": {"broker_ref": "plant", "telemetry_topic": "kish/gate/th_01/data"},
        "tags": [{"name": "temperature", "data_type": "float32", "json_path": "$.t"}]})


@pytest.fixture
def pki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ca = PlantCA(tmp_path)
    server = ca.issue("broker", server_names=["127.0.0.1", "mqtt.kish.local"])
    client = ca.issue("hub-client")
    (tmp_path / "other").mkdir()
    stranger = PlantCA(tmp_path / "other", "Other CA").issue("intruder")
    monkeypatch.setenv("HUB_T_CA", str(ca.pair.cert))
    monkeypatch.setenv("HUB_T_CERT", str(client.cert))
    monkeypatch.setenv("HUB_T_KEY", str(client.key))
    monkeypatch.setenv("HUB_T_BAD_CERT", str(stranger.cert))
    monkeypatch.setenv("HUB_T_BAD_KEY", str(stranger.key))
    return {"ca": ca, "server": server}


@pytest.fixture
async def broker(pki: dict[str, Any]) -> AsyncIterator[EmbeddedBroker]:
    b = EmbeddedBroker(tls={"certfile": str(pki["server"].cert), "keyfile": str(pki["server"].key),
                            "cafile": str(pki["ca"].pair.cert), "require_client_cert": True})
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


def entry(b: EmbeddedBroker, **kw: Any) -> Broker:
    return Broker(broker_id="plant", host="127.0.0.1", port=b.port, tls=True, **kw)


MTLS = {"ca_file_env": "HUB_T_CA", "client_certificate_env": "HUB_T_CERT",
        "client_private_key_env": "HUB_T_KEY"}


async def test_mutual_tls_receives_data(broker: EmbeddedBroker, pki: dict[str, Any]) -> None:
    dev = sensor()
    rec = Recorder()
    driver = MqttJsonDriver(dev, rec.on_samples, rec.on_status,
                            broker=entry(broker, **MTLS, tls_server_name="mqtt.kish.local"))
    await driver.start()
    try:
        # the driver subscribes once its TLS session is up; publish until the value arrives
        async with aiomqtt.Client("127.0.0.1", broker.port,
                                  tls_context=broker_tls_context(entry(broker, **MTLS))) as c:
            for _ in range(50):
                await c.publish("kish/gate/th_01/data", '{"t": 21.5}', qos=1)
                if "temperature" in rec.latest():
                    break
                await asyncio.sleep(0.2)
        await rec.wait_for(lambda: "temperature" in rec.latest())
        assert rec.state is DeviceState.ONLINE
        assert rec.latest()["temperature"].value == 21.5
    finally:
        await driver.stop()


@pytest.mark.parametrize(("settings", "expect"), [
    ({"client_certificate_env": "HUB_T_CERT", "client_private_key_env": "HUB_T_KEY"},
     "CERTIFICATE_VERIFY_FAILED"),                                    # plant CA not trusted
    (MTLS | {"tls_server_name": "evil.example"}, "Hostname mismatch"),
])
async def test_server_certificate_is_checked(broker: EmbeddedBroker, settings: dict[str, Any],
                                             expect: str) -> None:
    rec = Recorder()
    driver = MqttJsonDriver(sensor(), rec.on_samples, rec.on_status,
                            broker=entry(broker, **settings))
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert expect in (rec.statuses[-1].last_error or "")
    finally:
        await driver.stop()


@pytest.mark.parametrize("settings", [
    {"ca_file_env": "HUB_T_CA"},                                       # no client certificate
    {"ca_file_env": "HUB_T_CA", "client_certificate_env": "HUB_T_BAD_CERT",
     "client_private_key_env": "HUB_T_BAD_KEY"},                       # certificate of another CA
])
async def test_broker_refuses_clients_without_a_plant_certificate(
        broker: EmbeddedBroker, settings: dict[str, Any]) -> None:
    rec = Recorder()
    driver = MqttJsonDriver(sensor(), rec.on_samples, rec.on_status,
                            broker=entry(broker, **settings))
    await driver.start()
    try:
        await asyncio.sleep(2.0)
        assert rec.state is not DeviceState.ONLINE
    finally:
        await driver.stop()


def test_tls_files_must_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HUB_T_CA", str(tmp_path / "missing.pem"))
    with pytest.raises(DriverError, match="not a file"):
        broker_tls_context(Broker(broker_id="b", host="h", tls=True, ca_file_env="HUB_T_CA"))
    monkeypatch.delenv("HUB_T_CA")
    with pytest.raises(DriverError, match="not set"):
        broker_tls_context(Broker(broker_id="b", host="h", tls=True, ca_file_env="HUB_T_CA"))
