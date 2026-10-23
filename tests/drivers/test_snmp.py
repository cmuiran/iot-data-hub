"""SNMP driver against the SNMP agent simulator (pysnmp engine: real v2c and v3 security)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from hub.drivers.base import DeviceState
from hub.drivers.snmp import SnmpDriver, convert
from hub.models import Quality, SnmpDevice
from simulators.snmp_sim import SimulatedSnmpDevice, SnmpSimAgent
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration

TAGS = [
    {"name": "battery", "unit": "%", "data_type": "uint16", "oid": "1.3.6.1.2.1.33.1.2.4.0",
     "min": 0, "max": 100},
    {"name": "load", "unit": "%", "data_type": "float32", "oid": "1.3.6.1.4.1.9999.1.1.0"},
    {"name": "sysname", "data_type": "string", "oid": "1.3.6.1.2.1.1.5.0"},
    {"name": "octets", "data_type": "uint64", "oid": "1.3.6.1.2.1.31.1.1.1.6.1"},
    {"name": "on_battery", "data_type": "bool", "oid": "1.3.6.1.4.1.9999.1.2.0"},
    {"name": "status", "data_type": "int16", "oid": "1.3.6.1.2.1.33.1.2.1.0",
     "states": {1: "unknown", 2: "normal", 3: "low"}},
]
V3 = {"version": "3", "username": "hubmon", "auth_protocol": "sha256",
      "auth_key_env": "HUB_T_AUTH", "priv_protocol": "aes128", "priv_key_env": "HUB_T_PRIV"}


def ups(port: int, security: dict[str, Any], **conn: Any) -> SnmpDevice:
    return device({
        "device_id": "ups_01", "name": "UPS", "site": "kish", "area": "it", "protocol": "snmp",
        "poll_interval_ms": 150,
        "connection": {"host": "127.0.0.1", "port": port, "security": security,
                       "timeout_ms": 400, "retries": 0, "max_oids_per_request": 4} | conn,
        "tags": TAGS})


@pytest.fixture
async def agent(monkeypatch: pytest.MonkeyPatch
                ) -> AsyncIterator[tuple[SnmpSimAgent, SimulatedSnmpDevice]]:
    # the agent's UDP socket belongs to the running event loop: start it inside the test loop
    monkeypatch.setenv("HUB_T_COMM", "s3cret")
    monkeypatch.setenv("HUB_T_AUTH", "authpass123")
    monkeypatch.setenv("HUB_T_PRIV", "privpass123")
    a = SnmpSimAgent()
    a.add_community("s3cret")
    a.add_user("hubmon", "sha256", "authpass123", "aes128", "privpass123")
    sim = SimulatedSnmpDevice(a, ups(a.port, {"version": "2c", "community_env": "HUB_T_COMM"}))
    for name, value in (("battery", 87), ("load", 41.5), ("sysname", "ups-a"),
                        ("octets", 2**40), ("on_battery", False), ("status", 2)):
        sim.set(name, value)
    a.start()
    try:
        yield a, sim
    finally:
        a.stop()


async def run(dev: SnmpDevice, rec: Recorder) -> SnmpDriver:
    driver = SnmpDriver(dev, rec.on_samples, rec.on_status)
    driver._backoff.initial_s = 0.1  # noqa: SLF001
    await driver.start()
    return driver


@pytest.mark.parametrize("security", [{"version": "2c", "community_env": "HUB_T_COMM"}, V3])
async def test_reads_v2c_and_v3(agent: tuple[SnmpSimAgent, SimulatedSnmpDevice],
                                security: dict[str, Any]) -> None:
    a, _ = agent
    rec = Recorder()
    driver = await run(ups(a.port, security), rec)
    try:
        await rec.wait_for(lambda: len(rec.latest()) == 6 and rec.state is DeviceState.ONLINE)
        v = {k: s.value for k, s in rec.latest().items()}
        assert v == {"battery": 87, "load": pytest.approx(41.5), "sysname": "ups-a",
                     "octets": 2**40, "on_battery": False, "status": 2}
        assert await driver.read_tag("battery") == 87
    finally:
        await driver.stop()


async def test_missing_object_is_bad_and_error(
        agent: tuple[SnmpSimAgent, SimulatedSnmpDevice]) -> None:
    a, sim = agent
    sim.set("load", None)
    rec = Recorder()
    driver = await run(ups(a.port, V3), rec)
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.ERROR and "load" in rec.latest())
        assert rec.latest()["load"].quality is Quality.BAD
        assert rec.latest()["battery"].quality is Quality.GOOD
        assert "no such object" in (rec.statuses[-1].last_error or "")
    finally:
        await driver.stop()


@pytest.mark.parametrize(("security", "env", "expect"), [
    ({"version": "2c", "community_env": "HUB_T_COMM"}, {"HUB_T_COMM": "guess"}, "No SNMP response"),
    (V3, {"HUB_T_AUTH": "wrongpass123"}, "digest"),
])
async def test_wrong_credentials_are_offline(agent: tuple[SnmpSimAgent, SimulatedSnmpDevice],
                                             security: dict[str, Any], env: dict[str, str],
                                             expect: str,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    a, _ = agent
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rec = Recorder()
    driver = await run(ups(a.port, security), rec)
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert expect.lower() in (rec.statuses[-1].last_error or "").lower()
        assert not any(s.quality is Quality.GOOD for s in rec.samples)
    finally:
        await driver.stop()


def test_convert_values() -> None:
    from pysnmp.proto import rfc1902

    dev = ups(161, {"version": "2c", "community_env": "HUB_T_COMM"})
    by = {t.name: t for t in dev.tags}
    assert convert(by["on_battery"], rfc1902.Integer32(1)) is True        # TruthValue
    assert convert(by["on_battery"], rfc1902.Integer32(2)) is False
    assert convert(by["load"], rfc1902.OctetString("23.5")) == 23.5      # numbers as text
    assert convert(by["battery"], rfc1902.Gauge32(99)) == 99
    assert convert(by["sysname"], rfc1902.OctetString("  ups  ")) == "ups"
    from pysnmp.proto.rfc1905 import NoSuchInstance

    assert convert(by["battery"], NoSuchInstance()) is None
