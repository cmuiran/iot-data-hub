from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.config_loader import load_config
from hub.drivers.base import DeviceState
from hub.models import Quality
from hub.runtime import Hub
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice

pytestmark = pytest.mark.integration

VOLT = "kish.gate.meter_02.voltage_l1"


def write_config(tmp_path: Path, port: int, alarms: dict | None = None) -> Path:
    devices = tmp_path / "config" / "devices"
    devices.mkdir(parents=True)
    meter = {
        "device_id": "meter_02", "name": "Gate meter", "site": "kish", "area": "gate",
        "protocol": "modbus_tcp", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "unit_id": 1, "timeout_ms": 300,
                       "retries": 0},
        "tags": [
            {"name": "voltage_l1", "unit": "V", "data_type": "float32",
             "register_type": "input", "address": 0},
            {"name": "breaker", "data_type": "bool", "register_type": "discrete_input",
             "address": 0},
        ],
    }
    (devices / "meter_02.yaml").write_text(yaml.safe_dump(meter), encoding="utf-8")
    if alarms is not None:
        (tmp_path / "config" / "alarms.yaml").write_text(yaml.safe_dump(alarms), "utf-8")
    return tmp_path / "config"


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_loop(), timeout)


@pytest.fixture
async def sim() -> Any:
    async with ModbusSimServer() as server:
        yield server


async def test_hub_without_store_collects_and_publishes(sim: ModbusSimServer,
                                                        tmp_path: Path) -> None:
    report = load_config(write_config(tmp_path, sim.port), env={})
    simdev = SimulatedModbusDevice(sim, report.devices[0].device)
    simdev.set("voltage_l1", 229.0)
    hub = Hub(report)
    queue = hub.bus.subscribe()
    async with hub:
        await wait_until(lambda: VOLT in hub.latest)
        assert hub.latest[VOLT].value == pytest.approx(229.0)
        assert hub.statuses["meter_02"].state is DeviceState.ONLINE
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert {"samples", "status"} <= {e["type"] for e in events if e}
        driver, tag = hub.driver_for_tag(VOLT)
        assert tag.name == "voltage_l1" and driver.device.device_id == "meter_02"


@pytest.mark.postgres
async def test_hub_writes_to_store_and_raises_alarms(sim: ModbusSimServer, tmp_path: Path,
                                                     store: Any) -> None:
    alarms = {"offline_delay_s": 0, "rules": [{"tag": VOLT, "hi": 250, "severity": "high"}]}
    report = load_config(write_config(tmp_path, sim.port, alarms), env={})
    simdev = SimulatedModbusDevice(sim, report.devices[0].device)
    simdev.set("voltage_l1", 230.0)
    hub = Hub(report, store=store, flush_interval_s=0.1)
    async with hub:
        await wait_until(lambda: hub.stats.samples_written >= 2)
        simdev.set("voltage_l1", 260.0)
        await wait_until(lambda: hub.latest[VOLT].value > 250)
        await asyncio.sleep(0.3)
        active = await store.active_alarms()
        assert [(a.source, a.kind, a.severity) for a in active] == [(VOLT, "hi", "high")]

        sim.unit(1).offline = True   # device goes away -> offline alarm (delay 0)
        await wait_until(lambda: hub.statuses["meter_02"].state is DeviceState.OFFLINE)
        await asyncio.sleep(1.5)
        sources = {a.source for a in await store.active_alarms()}
        assert "device:meter_02" in sources
    latest = {lv.tag_path: lv for lv in await store.latest()}
    assert latest[VOLT].quality is Quality.BAD           # last thing seen: device offline
    status = {s.device_id: s for s in await store.device_statuses()}
    assert status["meter_02"].state == "offline"
    assert (await store.history(VOLT, latest[VOLT].ts.replace(year=2000), latest[VOLT].ts))


class FlakyStore:
    """Fails the first `failures` writes, like a database that is restarting."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.written: list[Any] = []

    async def sync_config(self, devices: Any) -> None: ...
    async def open_alarm_sources(self) -> dict: return {}
    async def open_alarm(self, **kw: Any) -> None: ...
    async def clear_alarm(self, *a: Any) -> None: ...
    async def upsert_device_status(self, status: Any) -> None: ...

    async def write_samples(self, samples: Any) -> int:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("database restarting")
        self.written.extend(samples)
        return len(samples)


async def test_samples_survive_a_database_outage(sim: ModbusSimServer, tmp_path: Path) -> None:
    report = load_config(write_config(tmp_path, sim.port), env={})
    SimulatedModbusDevice(sim, report.devices[0].device)
    flaky = FlakyStore(failures=2)
    hub = Hub(report, store=flaky, flush_interval_s=0.1)
    async with hub:
        await wait_until(lambda: hub.stats.write_errors >= 2, timeout=15)
        await wait_until(lambda: len(flaky.written) > 0, timeout=15)
    assert hub.stats.samples_dropped == 0
    # nothing lost: everything that passed the history filter was written once the DB was back
    assert len(flaky.written) == hub.stats.samples_received - hub.stats.samples_filtered
    assert len(flaky.written) >= 2


async def test_buffer_is_bounded(sim: ModbusSimServer, tmp_path: Path) -> None:
    report = load_config(write_config(tmp_path, sim.port), env={})
    SimulatedModbusDevice(sim, report.devices[0].device)
    simdev = SimulatedModbusDevice(sim, report.devices[0].device)
    hub = Hub(report, store=FlakyStore(failures=10**6), flush_interval_s=0.1, max_buffer=10)
    await hub.start()
    try:
        for i in range(200):          # changing values pass the filter
            simdev.set("voltage_l1", 220.0 + i)
            await asyncio.sleep(0.05)
            if hub.stats.samples_dropped > 0:
                break
        await wait_until(lambda: hub.stats.samples_dropped > 0)
        assert len(hub._buffer) == 10
    finally:
        hub._buffer.clear()  # stop() would try to flush into the broken store
        hub.store = None
        await hub.stop()


def test_hub_refuses_invalid_config(tmp_path: Path) -> None:
    (tmp_path / "devices").mkdir()
    (tmp_path / "devices" / "x.yaml").write_text("device_id: Bad-Id\n", "utf-8")
    hub = Hub(load_config(tmp_path, env={}))
    with pytest.raises(RuntimeError, match="configuration has errors"):
        asyncio.run(hub.start())
