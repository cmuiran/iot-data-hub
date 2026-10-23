"""Builders for throw-away config directories used by the config tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.config_loader import ConfigReport, Severity, load_config


def modbus_device(device_id: str = "meter_01", **overrides: Any) -> dict[str, Any]:
    device: dict[str, Any] = {
        "device_id": device_id,
        "name": "Test meter",
        "site": "kish",
        "area": "substation",
        "protocol": "modbus_tcp",
        "connection": {"host": "127.0.0.1", "port": 5020, "unit_id": 1},
        "tags": [
            {"name": "voltage", "data_type": "float32", "register_type": "input", "address": 0},
            {"name": "current", "data_type": "float32", "register_type": "input", "address": 2},
        ],
    }
    device.update(overrides)
    return device


def mqtt_device(device_id: str = "sensor_01", **overrides: Any) -> dict[str, Any]:
    device: dict[str, Any] = {
        "device_id": device_id,
        "name": "Test sensor",
        "site": "kish",
        "area": "warehouse",
        "protocol": "mqtt",
        "connection": {
            "broker_ref": "test_broker",
            "telemetry_topic": f"kish/warehouse/{device_id}/telemetry",
            "stale_after_s": 60,
        },
        "tags": [{"name": "temperature", "data_type": "float32", "json_path": "$.data.temp"}],
    }
    device.update(overrides)
    return device


def opcua_device(device_id: str = "plc_01", **overrides: Any) -> dict[str, Any]:
    device: dict[str, Any] = {
        "device_id": device_id,
        "name": "Test PLC",
        "site": "kish",
        "area": "pump_station",
        "protocol": "opcua",
        "connection": {
            "endpoint_url": "opc.tcp://127.0.0.1:4840",
            "security_policy": "None",
            "security_mode": "None",
        },
        "tags": [{"name": "motor_speed", "data_type": "float32", "node_id": "ns=2;s=Speed"}],
    }
    device.update(overrides)
    return device


DEFAULT_BROKERS = [{"broker_id": "test_broker", "host": "127.0.0.1"}]

WriteConfig = Callable[..., Path]


@pytest.fixture
def write_config(tmp_path: Path) -> WriteConfig:
    """Write devices (one file each, named <device_id>.yaml) and brokers into tmp_path."""

    def _write(
        *devices: dict[str, Any],
        brokers: list[dict[str, Any]] | None = DEFAULT_BROKERS,
        files: dict[str, str] | None = None,
    ) -> Path:
        devices_dir = tmp_path / "devices"
        devices_dir.mkdir(exist_ok=True)
        if brokers is not None:
            (tmp_path / "brokers.yaml").write_text(
                yaml.safe_dump({"brokers": brokers}), encoding="utf-8"
            )
        for i, device in enumerate(devices):
            name = f"{device.get('device_id', f'device_{i}')}.yaml"
            if (devices_dir / name).exists():
                name = f"copy_{i}_{name}"
            (devices_dir / name).write_text(
                yaml.safe_dump(device, allow_unicode=True), encoding="utf-8"
            )
        for name, text in (files or {}).items():
            (devices_dir / name).write_text(text, encoding="utf-8")
        return tmp_path

    return _write


def messages(report: ConfigReport, severity: Severity | None = None) -> list[str]:
    """Issues flattened to 'source | location | message' strings for easy asserts."""
    return [
        f"{i.source} | {i.location} | {i.message}"
        for i in report.issues
        if severity is None or i.severity is severity
    ]


def load(path: Path) -> ConfigReport:
    return load_config(path, env={})
