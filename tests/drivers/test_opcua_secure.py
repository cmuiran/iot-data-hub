"""OPC UA with Basic256Sha256 / SignAndEncrypt and certificate trust (item 11)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from hub.drivers.base import DeviceState
from hub.drivers.opcua import OpcUaDriver
from hub.pki import application_uri, make_certificate
from simulators.opcua_sim import SimulatedOpcUaServer
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def plc(endpoint: str) -> Any:
    return device({
        "device_id": "plc_sec", "name": "Secure PLC", "site": "kish", "area": "pump_station",
        "protocol": "opcua", "poll_interval_ms": 100,
        "connection": {"endpoint_url": endpoint, "security_policy": "Basic256Sha256",
                       "security_mode": "SignAndEncrypt",
                       "client_certificate_env": "TEST_OPC_CERT",
                       "client_private_key_env": "TEST_OPC_KEY", "timeout_ms": 4000},
        "tags": [{"name": "speed", "unit": "rpm", "data_type": "float64",
                  "node_id": "ns=2;s=Pump.Speed"}],
    })


@pytest.fixture
async def pki(tmp_path: Path) -> dict[str, Path]:
    server_cert, server_key = await make_certificate(tmp_path / "server", "sim", "server",
                                                     uri="urn:hub:sim", hostname="127.0.0.1")
    client_cert, client_key = await make_certificate(tmp_path / "client", "hub-client",
                                                     "client")
    other_cert, other_key = await make_certificate(tmp_path / "other", "intruder", "client",
                                                   uri="urn:intruder")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    shutil.copy(client_cert, trusted / client_cert.name)
    return {"server_cert": server_cert, "server_key": server_key, "client_cert": client_cert,
            "client_key": client_key, "other_cert": other_cert, "other_key": other_key,
            "trusted": trusted}


async def _run(server: SimulatedOpcUaServer, cert: Path, key: Path,
               monkeypatch: pytest.MonkeyPatch, until: Any) -> Recorder:
    monkeypatch.setenv("TEST_OPC_CERT", str(cert))
    monkeypatch.setenv("TEST_OPC_KEY", str(key))
    rec = Recorder()
    driver = OpcUaDriver(plc(server.endpoint), rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.3
    await driver.start()
    try:
        await rec.wait_for(lambda: until(rec), timeout=30)
    finally:
        await driver.stop()
    return rec


async def test_certificates_carry_the_application_uri(pki: dict[str, Path]) -> None:
    assert application_uri(pki["client_cert"]) == "urn:iot-data-hub:client"
    assert application_uri(pki["server_cert"]) == "urn:hub:sim"
    assert pki["client_key"].read_bytes().startswith(b"-----BEGIN")


async def test_sign_and_encrypt_with_trusted_certificate(pki: dict[str, Path],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    sim = SimulatedOpcUaServer([plc("opc.tcp://x:1/")], certificate=pki["server_cert"],
                               private_key=pki["server_key"], trusted_dir=pki["trusted"])
    await sim.start()
    try:
        await sim.set("plc_sec", "speed", 1234.5)
        rec = await _run(sim, pki["client_cert"], pki["client_key"], monkeypatch,
                         lambda r: "speed" in r.latest())
        assert rec.latest()["speed"].value == pytest.approx(1234.5)
        assert any(s.state is DeviceState.ONLINE for s in rec.statuses)
    finally:
        await sim.stop()


async def test_untrusted_certificate_is_rejected(pki: dict[str, Path],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    sim = SimulatedOpcUaServer([plc("opc.tcp://x:1/")], certificate=pki["server_cert"],
                               private_key=pki["server_key"], trusted_dir=pki["trusted"])
    await sim.start()
    try:
        rec = await _run(sim, pki["other_cert"], pki["other_key"], monkeypatch,
                         lambda r: r.state is DeviceState.OFFLINE)
        assert rec.samples == [] or all(s.value is None for s in rec.samples)
    finally:
        await sim.stop()


async def test_missing_certificate_env_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_OPC_CERT", raising=False)
    rec = Recorder()
    driver = OpcUaDriver(plc("opc.tcp://127.0.0.1:1/"), rec.on_samples, rec.on_status)
    driver._backoff.initial_s = driver._backoff.max_s = 0.2
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert "TEST_OPC_CERT" in (rec.statuses[-1].last_error or "")
    finally:
        await driver.stop()


def test_hub_admin_make_cert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
    from backend import admin

    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    assert admin.main(["make-cert", "--role", "client", "--name", "hub-client", "--out",
                       str(tmp_path), "--uri", "urn:kish:hub"]) == 0
    out = capsys.readouterr().out
    assert "urn:kish:hub" in out
    assert application_uri(tmp_path / "hub-client.der") == "urn:kish:hub"
