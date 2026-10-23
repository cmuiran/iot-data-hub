"""The hub as an OPC UA server, checked with a real (encrypted) OPC UA client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

from backend.opcua_server import HubOpcUaServer
from hub.pki import make_certificate
from simulators.mqtt_sim import free_port
from tests.api.conftest import GATE_METER, PASSWORD, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
VOLT = f"{GATE_METER}.voltage_l1"
LIMIT = f"{GATE_METER}.demand_limit"


async def start_server(b: Backend, tmp_path: Path, **settings: Any) -> HubOpcUaServer:
    b.services.settings = b.services.settings.model_copy(update={
        "opcua_server": True, "opcua_server_port": free_port(), "pki_dir": tmp_path / "pki",
        **settings})
    server = HubOpcUaServer(b.services)
    await server.start()
    return server


async def client(server: HubOpcUaServer, tmp_path: Path, username: str | None,
                 password: str = PASSWORD) -> Client:
    cert, key = await make_certificate(tmp_path / "client", "scada", "client",
                                       uri="urn:scada:client")
    c = Client(server.endpoint, timeout=5)
    c.application_uri = "urn:scada:client"
    await c.set_security(SecurityPolicyBasic256Sha256, str(cert), str(key),
                         mode=ua.MessageSecurityMode.SignAndEncrypt)
    if username:
        c.set_user(username)
        c.set_password(password)
    await c.connect()
    return c


@pytest.fixture
async def scada(backend: Backend, tmp_path: Path) -> AsyncIterator[HubOpcUaServer]:
    await backend.user("scada", ("viewer", None, None))
    server = await start_server(backend, tmp_path)
    try:
        yield server
    finally:
        await server.stop()


async def test_reads_tags_with_quality_and_units(backend: Backend, scada: HubOpcUaServer,
                                                 tmp_path: Path) -> None:
    c = await client(scada, tmp_path, "scada")
    try:
        ns = await c.get_namespace_index("urn:iot-data-hub")
        node = c.get_node(f"ns={ns};s={VOLT}")
        dv = await node.read_data_value()
        assert dv.Value.Value == pytest.approx(230.0) and dv.StatusCode.is_good()
        unit = await (await node.get_child([f"{ns}:Unit"])).read_value()
        assert unit == "V"
        status = await c.get_node(f"ns={ns};s=kish.gate.meter_02._status").read_value()
        assert status == "online"
        browse = await c.nodes.objects.get_child([f"{ns}:Hub", f"{ns}:kish", f"{ns}:gate",
                                                  f"{ns}:meter_02"])
        names = {(await n.read_browse_name()).Name for n in await browse.get_children()}
        assert {"voltage_l1", "demand_limit", "Status"} <= names
    finally:
        await c.disconnect()


async def test_values_follow_the_devices(backend: Backend, scada: HubOpcUaServer,
                                         tmp_path: Path) -> None:
    c = await client(scada, tmp_path, "scada")
    try:
        ns = await c.get_namespace_index("urn:iot-data-hub")
        node = c.get_node(f"ns={ns};s={VOLT}")
        backend.meter.set("voltage_l1", 241.5)
        for _ in range(100):
            if abs(await node.read_value() - 241.5) < 1e-3:
                break
            await asyncio.sleep(0.05)
        assert await node.read_value() == pytest.approx(241.5)
    finally:
        await c.disconnect()


async def test_login_rules(backend: Backend, scada: HubOpcUaServer, tmp_path: Path) -> None:
    await backend.user("view.gate", ("viewer", "kish", "gate"))       # scoped: refused
    for username, password in ((None, PASSWORD), ("scada", "wrong-password"),
                               ("view.gate", PASSWORD), ("nobody", PASSWORD)):
        with pytest.raises(Exception):  # noqa: B017 - asyncua raises various UaErrors
            c = await client(scada, tmp_path, username, password)
            await c.disconnect()
    assert scada.users.failures >= 3


async def test_writes_off_by_default(backend: Backend, scada: HubOpcUaServer,
                                     tmp_path: Path) -> None:
    c = await client(scada, tmp_path, "scada")
    try:
        ns = await c.get_namespace_index("urn:iot-data-hub")
        with pytest.raises(ua.UaStatusCodeError) as err:
            await c.get_node(f"ns={ns};s={LIMIT}").write_value(
                ua.DataValue(ua.Variant(250, ua.VariantType.UInt16)))
        assert err.value.code in (ua.StatusCodes.BadNotWritable,
                                  ua.StatusCodes.BadUserAccessDenied)
    finally:
        await c.disconnect()
    assert backend.sim.unit(1).writes == []


async def test_writes_go_through_the_safety_layer(backend: Backend, tmp_path: Path) -> None:
    await backend.user("scada.op", ("operator", None, None))
    server = await start_server(backend, tmp_path, opcua_server_writes=True)
    try:
        c = await client(server, tmp_path, "scada.op")
        try:
            ns = await c.get_namespace_index("urn:iot-data-hub")
            node = c.get_node(f"ns={ns};s={LIMIT}")
            await node.write_value(ua.DataValue(ua.Variant(250.0, ua.VariantType.Double)))
            assert ("holding", 100, [2500]) in backend.sim.unit(1).writes
            with pytest.raises(ua.UaStatusCodeError) as err:   # above max (500)
                await node.write_value(ua.DataValue(ua.Variant(900.0, ua.VariantType.Double)))
            assert err.value.code == ua.StatusCodes.BadOutOfRange
            with pytest.raises(ua.UaStatusCodeError):          # read-only tag
                await c.get_node(f"ns={ns};s={VOLT}").write_value(
                    ua.DataValue(ua.Variant(1.0, ua.VariantType.Float)))
        finally:
            await c.disconnect()
    finally:
        await server.stop()
    audit = await backend.store.list_write_audit(tag_path=LIMIT)
    outcomes = sorted(a.result for a in audit if a.phase == "outcome")
    assert outcomes == ["applied", "rejected_range"]
    assert {a.username for a in audit} == {"scada.op"}
    assert {a.reason for a in audit if a.phase == "requested"} == {"OPC UA write"}
