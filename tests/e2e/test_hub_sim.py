"""hub-sim against the repository's own config: every device must come online, no alarms."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from hub.config_loader import load_config
from hub.drivers.base import DeviceState
from hub.runtime import Hub
from simulators import run as sim_run
from simulators.mqtt_sim import free_port

pytestmark = pytest.mark.integration
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


async def test_repo_config_runs_against_simulators(tmp_path: Path) -> None:
    task = asyncio.create_task(sim_run._main(REPO_CONFIG, tmp_path, free_port(), free_port(),
                                             free_port()))
    try:
        sim_config = tmp_path / "config"
        for _ in range(200):
            if (sim_config / "brokers.yaml").exists() and not task.done():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(2.0)  # servers starting
        assert not task.done(), task.exception() if task.done() else None

        report = load_config(sim_config, env={})
        assert not report.has_errors, report.issues
        original = load_config(REPO_CONFIG, env={})
        assert {d.device.device_id for d in report.devices} == {
            d.device.device_id for d in original.devices}

        raised: list[str] = []
        hub = Hub(report)
        hub.bus.subscribe()
        original_raise = hub._raise_alarm

        async def capture(alarm):  # type: ignore[no-untyped-def]
            raised.append(alarm.source)
            await original_raise(alarm)

        hub.alarms._on_raise = capture
        async with hub:
            for _ in range(300):
                if all(s.state is DeviceState.ONLINE for s in hub.statuses.values()):
                    break
                await asyncio.sleep(0.05)
            assert {d: str(s.state) for d, s in hub.statuses.items()} == {
                d: "online" for d in hub.statuses}
            await asyncio.sleep(1.5)
            assert len(hub.latest) == len(hub.tags)
        assert raised == []   # values start in their normal range
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_rtu_devices_are_simulated(tmp_path: Path) -> None:
    import yaml

    config = tmp_path / "src"
    (config / "devices").mkdir(parents=True)
    for unit in (1, 2):
        (config / "devices" / f"rtu_{unit}.yaml").write_text(yaml.safe_dump({
            "device_id": f"rtu_{unit}", "name": "RTU meter", "site": "kish", "area": "gate",
            "protocol": "modbus_rtu", "poll_interval_ms": 200,
            "connection": {"port": "COM7", "unit_id": unit, "baudrate": 19200,
                           "timeout_ms": 500, "retries": 0},
            "tags": [{"name": "voltage", "data_type": "float32", "register_type": "input",
                      "address": 0}]}), encoding="utf-8")
    out = tmp_path / "out"
    task = asyncio.create_task(sim_run._main(config, out, free_port(), free_port(), free_port()))
    try:
        for _ in range(200):
            if (out / "config" / "brokers.yaml").exists():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        report = load_config(out / "config", env={})
        ports = {d.device.connection.port for d in report.devices}
        assert len(ports) == 1 and next(iter(ports)).startswith("socket://127.0.0.1:")
        async with Hub(report) as hub:
            for _ in range(200):
                if all(s.state is DeviceState.ONLINE for s in hub.statuses.values()):
                    break
                await asyncio.sleep(0.05)
            assert {d: str(s.state) for d, s in hub.statuses.items()} == {
                "rtu_1": "online", "rtu_2": "online"}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
