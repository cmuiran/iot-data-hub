"""The Windows socket budget: a configuration that would exhaust select() is refused up front."""

from __future__ import annotations

from typing import Any

from hub.config_loader import (
    DEVICE_CONNECTIONS_MAX,
    DEVICE_CONNECTIONS_WARN,
    ConfigReport,
    Severity,
    _check_connection_budget,
    device_connections,
)
from hub.models import DEVICE_ADAPTER
from tests.hub.builders import modbus_device, mqtt_device


def meters(n: int, *, shared_gateway: bool = False, start: int = 0) -> list[Any]:
    out = []
    for i in range(start, start + n):
        host = "10.0.0.1" if shared_gateway else f"10.0.{i // 250}.{i % 250 + 1}"
        out.append(DEVICE_ADAPTER.validate_python(modbus_device(
            f"meter_{i:04d}", connection={"host": host, "port": 502, "unit_id": i % 247 + 1})))
    return out


def test_counting() -> None:
    assert device_connections(meters(10)) == 10
    assert device_connections(meters(200, shared_gateway=True)) == 1      # one gateway
    sensors = [DEVICE_ADAPTER.validate_python(mqtt_device(f"s_{i}",
                                                          connection={
                                                              "broker_ref": "plant_broker",
                                                              "telemetry_topic": f"t/{i}"}))
               for i in range(5)]
    assert device_connections([*meters(3), *sensors]) == 8               # one client each
    off = meters(4)
    off[0] = off[0].model_copy(update={"enabled": False})
    assert device_connections(off) == 3


def report_with(devices: list[Any]) -> ConfigReport:
    from pathlib import Path

    from hub.config_loader import LoadedDevice

    report = ConfigReport(config_dir=Path("config"))
    report.devices = [LoadedDevice(d, f"devices/{d.device_id}.yaml") for d in devices]
    return report


def test_windows_limit() -> None:
    ok = report_with(meters(DEVICE_CONNECTIONS_WARN))
    _check_connection_budget(ok, "win32")
    assert ok.issues == []
    near = report_with(meters(DEVICE_CONNECTIONS_WARN + 1))
    _check_connection_budget(near, "win32")
    assert [i.severity for i in near.issues] == [Severity.WARNING]
    over = report_with(meters(DEVICE_CONNECTIONS_MAX + 1))
    _check_connection_budget(over, "win32")
    assert [i.severity for i in over.issues] == [Severity.ERROR]
    assert "gateways" in over.issues[0].message and "Linux" in over.issues[0].message
    # the same plant behind gateways is fine, and Linux has no such limit
    gw = report_with(meters(DEVICE_CONNECTIONS_MAX + 1, shared_gateway=True))
    _check_connection_budget(gw, "win32")
    assert gw.issues == []
    linux = report_with(meters(DEVICE_CONNECTIONS_MAX + 1))
    _check_connection_budget(linux, "linux")
    assert linux.issues == []
