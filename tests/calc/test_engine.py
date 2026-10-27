"""Calc engine: ordering, cycles, running totals, restart/reload state, and the hub end to end."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.calc.engine import CalcEngine, compile_calcs, definition_version
from hub.config_loader import load_config
from hub.models import CalcDevice, DataType, Quality, Sample, TagBase
from hub.runtime import Hub
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice

T0 = datetime(2026, 10, 1, 8, tzinfo=UTC)
CATALOGUE = {"s.a.m.p1": TagBase(name="p1", unit="kW", data_type=DataType.FLOAT32),
             "s.a.m.p2": TagBase(name="p2", unit="kW", data_type=DataType.FLOAT32),
             "s.a.m.run": TagBase(name="run", data_type=DataType.BOOL)}


def calc_device(*tags: dict[str, Any]) -> CalcDevice:
    return CalcDevice.model_validate({"device_id": "calc", "name": "C", "site": "s",
                                      "area": "a", "protocol": "calc", "tags": list(tags)})


def compile_device(device: CalcDevice) -> tuple[dict, list[str], list]:
    catalogue = dict(CATALOGUE)
    catalogue.update({device.tag_path(t.name): t for t in device.tags})
    return compile_calcs([device], catalogue)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def engine_for(device: CalcDevice, clock: Clock) -> tuple[CalcEngine, list[Sample]]:
    defs, order, issues = compile_device(device)
    assert not [i for i in issues if i.error], issues
    emitted: list[Sample] = []

    async def emit(samples: list[Sample]) -> None:
        emitted.extend(samples)

    return CalcEngine(defs, order, emit, clock), emitted


def feed(engine: CalcEngine, clock: Clock, **values: Any) -> None:
    engine.feed([Sample(tag=f"s.a.m.{k}", value=v, timestamp=clock.now)
                 for k, v in values.items()])


def test_dependencies_are_ordered_and_chained_in_one_pass() -> None:
    device = calc_device(
        {"name": "share", "unit": "%", "data_type": "float64", "formula": "p1 / total",
         "inputs": {"p1": "s.a.m.p1", "total": "s.a.calc.total"}},
        {"name": "total", "unit": "kW", "data_type": "float64", "formula": "p1 + p2",
         "inputs": {"p1": "s.a.m.p1", "p2": "s.a.m.p2"}})
    clock = Clock()
    engine, _ = engine_for(device, clock)
    assert engine.order == ["s.a.calc.total", "s.a.calc.share"]
    feed(engine, clock, p1=30.0, p2=10.0)
    out = {s.tag: s for s in engine.evaluate_due(0.0)}
    assert out["s.a.calc.total"].value == pytest.approx(40)
    assert out["s.a.calc.share"].value == pytest.approx(75)
    assert out["s.a.calc.share"].quality is Quality.GOOD


def test_cycles_are_refused_with_their_path() -> None:
    device = calc_device(
        {"name": "a", "unit": "kW", "data_type": "float64", "formula": "b + p1",
         "inputs": {"b": "s.a.calc.b", "p1": "s.a.m.p1"}},
        {"name": "b", "unit": "kW", "data_type": "float64", "formula": "a",
         "inputs": {"a": "s.a.calc.a"}},
        {"name": "ok", "unit": "kW", "data_type": "float64", "formula": "p1",
         "inputs": {"p1": "s.a.m.p1"}},
        {"name": "self", "unit": "kW", "data_type": "float64", "formula": "x",
         "inputs": {"x": "s.a.calc.self"}})
    defs, order, issues = compile_device(device)
    messages = [i.message for i in issues if i.error]
    assert any("circular dependency: s.a.calc.a -> s.a.calc.b -> s.a.calc.a" in m
               for m in messages)
    assert any("reads its own result" in m for m in messages)
    assert order == ["s.a.calc.ok"]


def test_min_interval_and_period() -> None:
    device = calc_device({"name": "t", "unit": "kW", "data_type": "float64", "formula": "p1",
                          "inputs": {"p1": "s.a.m.p1"}, "min_interval_s": 2, "period_s": 10})
    clock = Clock()
    engine, _ = engine_for(device, clock)
    feed(engine, clock, p1=1.0)
    assert len(engine.evaluate_due(0.0)) == 1
    feed(engine, clock, p1=2.0)
    assert engine.evaluate_due(1.0) == []           # changed, but within min_interval
    assert len(engine.evaluate_due(2.0)) == 1       # changed and min_interval passed
    assert engine.evaluate_due(5.0) == []           # nothing new
    assert len(engine.evaluate_due(12.5)) == 1      # period


def test_integrate_power_to_energy_and_gaps() -> None:
    device = calc_device({"name": "e", "unit": "kWh", "data_type": "float64",
                          "formula": "integrate(p1)", "inputs": {"p1": "s.a.m.p1"},
                          "max_gap_s": 600, "period_s": 1, "min_interval_s": 0.1})
    clock = Clock()
    engine, _ = engine_for(device, clock)
    feed(engine, clock, p1=100.0)                    # 100 kW
    engine.evaluate_due(0)
    clock.now += timedelta(minutes=6)
    feed(engine, clock, p1=100.0)
    (s,) = engine.evaluate_due(360)
    assert s.value == pytest.approx(10.0)            # 100 kW for 0.1 h
    assert s.quality is Quality.GOOD
    clock.now += timedelta(hours=2)                  # gap longer than max_gap_s: not counted
    feed(engine, clock, p1=50.0)
    (s,) = engine.evaluate_due(7560)
    assert s.value == pytest.approx(10.0) and s.quality is Quality.UNCERTAIN


def test_runtime_counts_running_hours_and_survives_restart() -> None:
    device = calc_device({"name": "h", "unit": "h", "data_type": "float64",
                          "formula": "runtime(run)", "inputs": {"run": "s.a.m.run"},
                          "max_gap_s": 7200, "period_s": 1, "min_interval_s": 0.1})
    clock = Clock()
    engine, _ = engine_for(device, clock)
    feed(engine, clock, run=True)
    engine.evaluate_due(0)
    clock.now += timedelta(minutes=30)
    feed(engine, clock, run=False)
    (s,) = engine.evaluate_due(1800)
    assert s.value == pytest.approx(0.5)
    clock.now += timedelta(minutes=30)               # stopped: no time counted
    feed(engine, clock, run=False)
    (s,) = engine.evaluate_due(3600)
    assert s.value == pytest.approx(0.5)

    restarted, _ = engine_for(device, clock)         # e.g. after a hub restart
    restarted.seed([s])
    feed(restarted, clock, run=True)
    restarted.evaluate_due(0)
    clock.now += timedelta(minutes=15)
    feed(restarted, clock, run=True)
    (s,) = restarted.evaluate_due(900)
    assert s.value == pytest.approx(0.75)


def test_reload_keeps_totals_only_for_unchanged_versions() -> None:
    spec = {"name": "h", "unit": "h", "data_type": "float64", "formula": "runtime(run)",
            "inputs": {"run": "s.a.m.run"}}
    clock = Clock()
    old, _ = engine_for(calc_device(spec), clock)
    old.states["s.a.calc.h"].acc = 7200.0
    same, _ = engine_for(calc_device({**spec, "description": "text only"}), clock)
    same.adopt(old)
    assert same.states["s.a.calc.h"].acc == 7200.0
    changed, _ = engine_for(calc_device({**spec, "max_gap_s": 60}), clock)
    changed.adopt(old)
    assert changed.states["s.a.calc.h"].acc == 0.0


def test_definition_version_ignores_description() -> None:
    a = calc_device({"name": "t", "unit": "kW", "data_type": "float64", "formula": "p1",
                     "inputs": {"p1": "s.a.m.p1"}}).tags[0]
    b = a.model_copy(update={"description": "x"})
    c = a.model_copy(update={"formula": "p1 * 2"})
    assert definition_version(a) == definition_version(b) != definition_version(c)


def test_math_error_is_a_bad_sample() -> None:
    device = calc_device({"name": "r", "data_type": "float64", "formula": "p1 / p2",
                          "inputs": {"p1": "s.a.m.p1", "p2": "s.a.m.p2"}})
    clock = Clock()
    engine, _ = engine_for(device, clock)
    feed(engine, clock, p1=1.0, p2=0.0)
    (s,) = engine.evaluate_due(0)
    assert s.quality is Quality.BAD and s.reason == "math_error" and s.value is None


# --- with the hub and a simulated Modbus meter ---------------------------------------------


def write_config(tmp_path: Path, port: int) -> Path:
    devices = tmp_path / "config" / "devices"
    devices.mkdir(parents=True)
    meter = {
        "device_id": "meter_02", "name": "Gate meter", "site": "kish", "area": "gate",
        "protocol": "modbus_tcp", "poll_interval_ms": 100,
        "connection": {"host": "127.0.0.1", "port": port, "unit_id": 1, "timeout_ms": 300,
                       "retries": 0},
        "tags": [
            {"name": "p1", "unit": "kW", "data_type": "float32", "register_type": "input",
             "address": 0},
            {"name": "p2", "unit": "W", "data_type": "float32", "register_type": "input",
             "address": 2},
        ],
    }
    calc = {
        "device_id": "calc_gate", "name": "Gate calculations", "site": "kish", "area": "gate",
        "protocol": "calc",
        "tags": [{"name": "total", "unit": "kW", "data_type": "float64", "formula": "a + b",
                  "inputs": {"a": "kish.gate.meter_02.p1", "b": "kish.gate.meter_02.p2"},
                  "min_interval_s": 0.1}],
    }
    for name, doc in (("meter_02", meter), ("calc_gate", calc)):
        (devices / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path / "config"


@pytest.mark.integration
async def test_hub_computes_calculated_tags_from_live_data(tmp_path: Path) -> None:
    async with ModbusSimServer() as server:
        report = load_config(write_config(tmp_path, server.port), env={})
        assert not report.has_errors, report.issues
        sim = SimulatedModbusDevice(server, report.devices[1].device)
        sim.set("p1", 12.0)
        sim.set("p2", 500.0)
        hub = Hub(report)
        async with hub:
            total = "kish.gate.calc_gate.total"

            async def ready() -> None:
                while total not in hub.latest or hub.latest[total].value is None or \
                        abs(hub.latest[total].value - 12.5) > 1e-6:
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(ready(), 10)
            assert hub.latest[total].quality is Quality.GOOD
            assert hub.latest[total].received_at is not None
            assert str(hub.statuses["calc_gate"].state) == "online"
            sim.set("p1", 20.0)

            async def updated() -> None:
                while abs((hub.latest[total].value or 0) - 20.5) > 1e-6:
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(updated(), 10)
            driver, _tag = hub.driver_for_tag(total)
            assert await driver.read_tag("total") == pytest.approx(20.5)

            # a reload that changes the formula (calc devices have no connection)
            calc = report.devices[0].device
            assert isinstance(calc, CalcDevice)
            changed = calc.model_copy(update={"tags": [calc.tags[0].model_copy(
                update={"formula": "a + b + 1[kW]"})]})
            (tmp_path / "config" / "devices" / "calc_gate.yaml").write_text(
                yaml.safe_dump(changed.model_dump(mode="json", exclude_none=True)), "utf-8")
            new_report = load_config(tmp_path / "config", env={})
            assert not new_report.has_errors, new_report.issues
            changes = await hub.reload(new_report)
            assert changes["restarted"] == ["calc_gate"] and changes["unchanged"] == ["meter_02"]

            async def reloaded() -> None:
                while abs((hub.latest[total].value or 0) - 21.5) > 1e-6:
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(reloaded(), 10)
            # removing every calculated device stops the engine
            (tmp_path / "config" / "devices" / "calc_gate.yaml").unlink()
            await hub.reload(load_config(tmp_path / "config", env={}))
            assert hub.calc is None and total not in hub.latest

