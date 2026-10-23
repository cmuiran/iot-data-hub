"""Regression tests for the add-device skill's bulk importer (scripts/excel_to_devices.py)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

openpyxl = pytest.importorskip("openpyxl")

SKILL = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "add-device"
TEMPLATE = SKILL / "templates" / "devices_template.xlsx"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "excel_to_devices", SKILL / "scripts" / "excel_to_devices.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it executes
    spec.loader.exec_module(module)
    return module


excel_to_devices = _load_script()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    (config / "devices").mkdir(parents=True)
    (config / "brokers.yaml").write_text(
        yaml.safe_dump({"brokers": [{"broker_id": "plant_broker", "host": "127.0.0.1"}]}),
        encoding="utf-8",
    )
    return config


def _workbook(tmp_path: Path, sheets: dict[str, list[dict[str, Any]]]) -> Path:
    """Copy the template and append rows by column name (so column order can change)."""
    path = tmp_path / "devices.xlsx"
    shutil.copy(TEMPLATE, path)
    wb = openpyxl.load_workbook(path)
    for sheet, rows in sheets.items():
        ws = wb[sheet]
        header = [c.value for c in ws[1]]
        for row in rows:
            unknown = set(row) - set(header)
            assert not unknown, f"{sheet} has no column(s) {unknown}"
            ws.append([row.get(h) for h in header])
    wb.save(path)
    return path


def _run(workbook: Path, config_dir: Path, *extra: str) -> int:
    return excel_to_devices.main([str(workbook), "--out", str(config_dir / "devices"), *extra])


def _device(config_dir: Path, device_id: str) -> dict[str, Any]:
    return yaml.safe_load((config_dir / "devices" / f"{device_id}.yaml").read_text("utf-8"))


def test_template_examples_are_skipped(tmp_path: Path, config_dir: Path) -> None:
    assert _run(_workbook(tmp_path, {}), config_dir) == 0
    assert list((config_dir / "devices").iterdir()) == []


def test_modbus_addresses_bits_and_64_bit(tmp_path: Path, config_dir: Path) -> None:
    wb = _workbook(tmp_path, {
        "Devices": [{"device_id": "meter_02", "name": "Meter", "protocol": "modbus_tcp",
                     "site": "kish", "area": "gate", "host": "192.168.10.22", "unit_id": 1,
                     "word_order": "little"}],
        "ModbusRegisters": [
            {"device_id": "meter_02", "tag": "voltage_l1", "data_type": "float32",
             "address_style": "modicon", "address": 30001, "unit": "V"},
            {"device_id": "meter_02", "tag": "energy_total", "data_type": "int64",
             "address_style": "modicon", "address": 33204, "unit": "Wh"},
            {"device_id": "meter_02", "tag": "status_word", "data_type": "uint16",
             "address_style": "one_based", "register_type": "holding", "address": 101},
            {"device_id": "meter_02", "tag": "breaker_tripped", "data_type": "bool",
             "address_style": "one_based", "register_type": "holding", "address": 101,
             "bit": 3},
            {"device_id": "meter_02", "tag": "relay", "data_type": "bool",
             "address_style": "modicon", "address": "00001", "access": "read_write"},
        ],
    })
    assert _run(wb, config_dir) == 0
    tags = {t["name"]: t for t in _device(config_dir, "meter_02")["tags"]}
    assert tags["voltage_l1"] == {"name": "voltage_l1", "unit": "V", "data_type": "float32",
                                  "register_type": "input", "address": 0}
    assert (tags["energy_total"]["register_type"], tags["energy_total"]["address"]) == (
        "input", 3203)
    assert tags["breaker_tripped"]["bit"] == 3
    assert tags["breaker_tripped"]["address"] == tags["status_word"]["address"] == 100
    assert (tags["relay"]["register_type"], tags["relay"]["address"]) == ("coil", 0)
    assert tags["relay"]["access"] == "read_write"


def test_mqtt_device_timestamp(tmp_path: Path, config_dir: Path) -> None:
    wb = _workbook(tmp_path, {
        "Devices": [{"device_id": "env_sensor_02", "name": "Sensor", "protocol": "mqtt",
                     "site": "kish", "area": "gate", "broker_ref": "plant_broker",
                     "telemetry_topic": "kish/gate/th-01/data", "timestamp_path": "$.ts",
                     "timestamp_format": "unix_s"}],
        "MqttTags": [{"device_id": "env_sensor_02", "tag": "temperature",
                      "data_type": "float32", "json_path": "$.data.t", "unit": "degC"}],
    })
    assert _run(wb, config_dir) == 0
    conn = _device(config_dir, "env_sensor_02")["connection"]
    assert conn["timestamp_path"] == "$.ts"
    assert conn["timestamp_format"] == "unix_s"


def test_unknown_host_gives_disabled_placeholder(tmp_path: Path, config_dir: Path) -> None:
    wb = _workbook(tmp_path, {
        "Devices": [{"device_id": "vfd_01", "name": "Drive", "protocol": "modbus_tcp",
                     "site": "kish", "area": "pump_station", "host": "TODO", "unit_id": 3}],
        "ModbusRegisters": [
            {"device_id": "vfd_01", "tag": "motor_speed", "data_type": "int16",
             "address_style": "protocol", "register_type": "holding", "address": 0},
            {"device_id": "vfd_01", "tag": "frequency", "data_type": "uint16",
             "address_style": "protocol", "register_type": "holding", "address": "TODO"},
        ],
    })
    assert _run(wb, config_dir) == 0
    text = (config_dir / "devices" / "vfd_01.yaml").read_text("utf-8")
    device = yaml.safe_load(text)
    assert device["enabled"] is False
    assert device["connection"]["host"] == "todo.invalid"
    assert [t["name"] for t in device["tags"]] == ["motor_speed"]
    assert "frequency" in text  # listed in the TODO header


def test_collision_with_existing_device_is_not_written(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = {"device_id": "meter_01", "name": "Old", "site": "kish", "area": "gate",
                "protocol": "modbus_tcp", "connection": {"host": "10.0.0.5", "unit_id": 1},
                "tags": [{"name": "v", "data_type": "uint16", "register_type": "input",
                          "address": 0}]}
    (config_dir / "devices" / "meter_01.yaml").write_text(yaml.safe_dump(existing), "utf-8")
    wb = _workbook(tmp_path, {
        "Devices": [{"device_id": "meter_09", "name": "New", "protocol": "modbus_tcp",
                     "site": "kish", "area": "gate", "host": "10.0.0.5", "unit_id": 1}],
        "ModbusRegisters": [{"device_id": "meter_09", "tag": "v", "data_type": "uint16",
                             "address_style": "protocol", "register_type": "input",
                             "address": 0}],
    })
    assert _run(wb, config_dir) == 1
    assert not (config_dir / "devices" / "meter_09.yaml").exists()
    assert "unit_id 1 is also used by 'meter_01'" in capsys.readouterr().out


def test_invalid_bit_is_refused(tmp_path: Path, config_dir: Path) -> None:
    wb = _workbook(tmp_path, {
        "Devices": [{"device_id": "plc_05", "name": "PLC", "protocol": "modbus_tcp",
                     "site": "kish", "area": "gate", "host": "10.0.0.9", "unit_id": 1}],
        "ModbusRegisters": [{"device_id": "plc_05", "tag": "flag", "data_type": "bool",
                             "address_style": "protocol", "register_type": "holding",
                             "address": 0, "bit": 3, "access": "read_write"}],
    })
    assert _run(wb, config_dir) == 1
    assert not (config_dir / "devices" / "plc_05.yaml").exists()


def test_modbus_rtu_and_history_columns(tmp_path: Path, config_dir: Path) -> None:
    wb = _workbook(tmp_path, {
        "Devices": [
            {"device_id": "rtu_meter_01", "name": "RTU meter", "protocol": "modbus_rtu",
             "site": "kish", "area": "gate", "serial_port": "COM4", "unit_id": 3,
             "baudrate": 19200, "parity": "E", "history_heartbeat_s": 600},
            {"device_id": "env_sensor_09", "name": "Sensor", "protocol": "mqtt",
             "site": "kish", "area": "gate", "broker_ref": "plant_broker",
             "telemetry_topic": "kish/gate/s9/data", "stale_after_s": 120}],
        "ModbusRegisters": [{"device_id": "rtu_meter_01", "tag": "voltage", "data_type": "float32",
                             "address_style": "modicon", "address": 30001,
                             "history_deadband": 0.5},
                            {"device_id": "rtu_meter_01", "tag": "serial_no",
                             "data_type": "uint32", "address_style": "modicon",
                             "address": 30101, "history": False}],
        "MqttTags": [{"device_id": "env_sensor_09", "tag": "temperature",
                      "data_type": "float32", "json_path": "$.t"}],
    })
    assert _run(wb, config_dir) == 0
    rtu = _device(config_dir, "rtu_meter_01")
    assert rtu["connection"] == {"port": "COM4", "unit_id": 3, "baudrate": 19200, "parity": "E"}
    assert rtu["history_heartbeat_s"] == 600
    tags = {t["name"]: t for t in rtu["tags"]}
    assert tags["voltage"]["history_deadband"] == 0.5 and tags["serial_no"]["history"] is False
    assert _device(config_dir, "env_sensor_09")["connection"]["stale_after_s"] == 120
