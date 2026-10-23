"""Two hub nodes on one database: one active, one standby, failover (item 18)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import yaml

from backend.app import create_app
from backend.ha import HaCoordinator
from backend.services import Services
from hub.config_loader import load_config
from hub.drivers.base import DeviceState
from hub.runtime import Hub
from hub.storage.postgres import PostgresStore
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from tests.api.conftest import make_settings

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@dataclass
class Node:
    services: Services
    ha: HaCoordinator
    client: httpx.AsyncClient
    store: PostgresStore


async def wait(predicate: Any, timeout: float = 20.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_loop(), timeout)


@pytest.fixture
async def pair(tmp_path: Path, database_url: str) -> AsyncIterator[tuple[Node, Node, Any]]:
    async with ModbusSimServer() as sim:
        config = tmp_path / "config"
        (config / "devices").mkdir(parents=True)
        meter = {"device_id": "meter_02", "name": "Meter", "site": "kish", "area": "gate",
                 "protocol": "modbus_tcp", "poll_interval_ms": 100,
                 "connection": {"host": "127.0.0.1", "port": sim.port, "unit_id": 1,
                                "timeout_ms": 300, "retries": 0},
                 "tags": [{"name": "voltage_l1", "data_type": "float32",
                           "register_type": "input", "address": 0}]}
        (config / "devices" / "meter_02.yaml").write_text(yaml.safe_dump(meter), "utf-8")
        report = load_config(config, env={})
        SimulatedModbusDevice(sim, report.devices[0].device).set("voltage_l1", 230.0)
        nodes = []
        for name in ("node_a", "node_b"):
            store = PostgresStore(database_url, max_size=4)
            await store.open()
            settings = make_settings(node_id=name, config_dir=config, ha="on",
                                     database_url=database_url)
            services = Services.build(settings, report, store,
                                      Hub(report, store=store, flush_interval_s=0.1))
            ha = HaCoordinator(services, database_url, retry_s=0.2)
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(services)),
                                       base_url="http://hub")
            nodes.append(Node(services, ha, client, store))
        try:
            yield nodes[0], nodes[1], sim
        finally:
            for n in nodes:
                await n.ha.stop()
                await n.client.aclose()
                await n.store.close()


async def test_one_active_one_standby_then_failover(pair: tuple[Node, Node, Any],
                                                    database_url: str) -> None:
    a, b, sim = pair
    await a.ha.start()
    await wait(lambda: a.services.role == "active")
    await b.ha.start()
    await asyncio.sleep(1.0)
    assert b.services.role == "standby"
    assert b.services.hub.drivers == {} or all(
        d._task is None for d in b.services.hub.drivers.values())

    health_a = await a.client.get("/api/health")
    health_b = await b.client.get("/api/health")
    assert health_a.status_code == 200 and health_a.json()["role"] == "active"
    assert health_b.status_code == 503 and health_b.json()["role"] == "standby"
    blocked = await b.client.post("/api/auth/login", json={"username": "x", "password": "y"})
    assert blocked.status_code == 503 and blocked.json()["error"] == "standby"
    await wait(lambda: a.services.hub.statuses["meter_02"].state is DeviceState.ONLINE)

    # node A goes away cleanly -> B takes over within a few retries
    await a.ha.stop()
    await wait(lambda: b.services.role == "active")
    await wait(lambda: b.services.hub.statuses["meter_02"].state is DeviceState.ONLINE)
    assert (await b.client.get("/api/health")).status_code == 200

    # the role row is written when the takeover has finished starting everything
    async def roles() -> dict[str, str]:
        return {n["node_id"]: n["role"] for n in await b.store.nodes()}

    for _ in range(100):
        if (await roles()).get("node_b") == "active":
            break
        await asyncio.sleep(0.1)
    assert await roles() == {"node_a": "stopped", "node_b": "active"}
    kinds = [(e["node_id"], e["detail"]["role"]) for e in await b.store.events("failover", 10)]
    assert ("node_b", "active") in kinds and ("node_a", "active") in kinds


async def test_lost_database_connection_fences_the_active_node(pair: tuple[Node, Node, Any],
                                                               database_url: str) -> None:
    a, b, sim = pair
    await a.ha.start()
    await wait(lambda: a.services.role == "active")
    await b.ha.start()
    await asyncio.sleep(0.5)
    assert b.services.role == "standby"

    # kill A's lock connection on the server (like a network split between A and the DB)
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = "
                      "'advisory' AND granted")
    await wait(lambda: "standby" in a.ha.transitions)      # A stopped its drivers
    assert all(d._task is None for d in a.services.hub.drivers.values())
    await wait(lambda: b.services.role == "active" or a.services.role == "active")
    actives = [n for n in (a, b) if n.services.role == "active"]
    assert len(actives) == 1                                 # never two active nodes
