"""Cross-file checks for phase 3: northbound.yaml, modbus_server.yaml, the new device kinds and
Modbus bit fields."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hub.config_loader import Severity
from tests.hub.builders import WriteConfig, load, messages, modbus_device

V = "kish.substation.meter_01.voltage"


def add(path: Path, name: str, data: dict[str, Any]) -> Path:
    (path / name).write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def writable_meter() -> dict[str, Any]:
    return modbus_device(tags=[
        {"name": "voltage", "data_type": "float32", "register_type": "input", "address": 0},
        {"name": "limit", "data_type": "uint16", "register_type": "holding", "address": 10,
         "access": "read_write", "min": 0, "max": 500},
        {"name": "breaker", "data_type": "bool", "register_type": "coil", "address": 0,
         "access": "read_write"},
        {"name": "status", "data_type": "uint16", "register_type": "holding", "address": 20},
        {"name": "mode", "data_type": "uint16", "register_type": "holding", "address": 20,
         "bit": 4, "bit_count": 3},
        {"name": "fault", "data_type": "bool", "register_type": "holding", "address": 20,
         "bit": 7},
    ])


def test_bit_fields_may_share_a_register_but_not_bits(write_config: WriteConfig) -> None:
    report = load(write_config(writable_meter()))
    assert messages(report, Severity.ERROR) == []
    meter = writable_meter()
    meter["tags"].append({"name": "clash", "data_type": "bool", "register_type": "holding",
                          "address": 20, "bit": 5})
    errors = messages(load(write_config(meter)), Severity.ERROR)
    assert any("bit 5 of holding 20 is already used" in e for e in errors)


def test_northbound_checks(write_config: WriteConfig) -> None:
    path = write_config(writable_meter())
    add(path, "northbound.yaml", {"targets": [
        {"name": "ho", "broker_ref": "test_broker", "tags": ["kish.substation.*"]},
        {"name": "ho", "broker_ref": "nowhere", "tags": ["kish.nothing.*"]}]})
    report = load(path)
    errors = messages(report, Severity.ERROR)
    assert any("duplicate target name 'ho'" in e for e in errors)
    assert any("unknown broker 'nowhere'" in e for e in errors)
    assert any("matches no tag" in w for w in messages(report, Severity.WARNING))


def test_modbus_server_checks(write_config: WriteConfig) -> None:
    path = write_config(writable_meter())
    add(path, "modbus_server.yaml", {
        "host": "0.0.0.0",
        "points": [
            {"tag": V, "register_type": "input", "address": 0, "data_type": "float32"},
            {"tag": V, "register_type": "input", "address": 1, "source": "quality"},   # overlap
            {"tag": "kish.substation.meter_01.nothing", "register_type": "input", "address": 5},
            {"tag": "kish.substation.meter_01.status", "register_type": "holding",
             "address": 0, "data_type": "uint16", "writable": True},                # read-only tag
            {"tag": "kish.substation.meter_01.breaker", "register_type": "holding",
             "address": 2, "data_type": "uint16", "writable": True},                # bool not coil
            {"tag": V, "register_type": "coil", "address": 0},                      # not bool
        ],
        "writes": {"as_user": "scada", "allowed_clients": ["10.0.0.5"]}})
    report = load(path)
    errors = messages(report, Severity.ERROR)
    assert any("input 1 is already used by points[0]" in e for e in errors)
    assert any("unknown tag kish.substation.meter_01.nothing" in e for e in errors)
    assert any("is read-only in its device config" in e for e in errors)
    assert any("writable bool tag is served as a coil" in e for e in errors)
    assert any("coil needs a bool tag" in e for e in errors)
    assert any("limit readers with allowed_clients" in w
               for w in messages(report, Severity.WARNING))


def test_http_and_snmp_warnings(write_config: WriteConfig) -> None:
    http = {"device_id": "inv_01", "name": "Inverter", "site": "kish", "area": "solar",
            "protocol": "http",
            "connection": {"base_url": "http://10.0.0.7", "auth": {"type": "bearer",
                                                                   "token_env": "HUB_T_TOK"}},
            "tags": [{"name": "p", "data_type": "float32", "path": "/s", "json_path": "$.p"},
                     {"name": "p2", "data_type": "float32", "path": "/s", "json_path": "$.p"}]}
    snmp = {"device_id": "ups_01", "name": "UPS", "site": "kish", "area": "it",
            "protocol": "snmp",
            "connection": {"host": "10.0.0.8", "security": {"version": "2c",
                                                            "community_env": "HUB_T_COMM"}},
            "tags": [{"name": "b", "data_type": "uint16", "oid": "1.3.6.1.2.1.33.1.2.4.0"}]}
    s7 = {"device_id": "plc_s7", "name": "S7", "site": "kish", "area": "water",
          "protocol": "s7", "connection": {"host": "10.0.0.9"},
          "tags": [{"name": "a", "data_type": "float32", "address": "DB1.DBD0"},
                   {"name": "b", "data_type": "float32", "address": "db1.dbd0"}]}
    report = load(write_config(http, snmp, s7))
    assert messages(report, Severity.ERROR) == []
    warnings = messages(report, Severity.WARNING)
    assert any("unencrypted over http://" in w for w in warnings)
    assert any("env var HUB_T_TOK is not set" in w for w in warnings)
    assert any("SNMPv2c sends the community in clear text" in w for w in warnings)
    assert any("same path/json_path" in w for w in warnings)
    assert any("same address as tags[0]" in w for w in warnings)


def test_tls_env_vars_of_brokers_are_checked(write_config: WriteConfig) -> None:
    report = load(write_config(brokers=[{
        "broker_id": "test_broker", "host": "mqtt.kish.local", "tls": True,
        "ca_file_env": "HUB_T_CA", "client_certificate_env": "HUB_T_C",
        "client_private_key_env": "HUB_T_K"}]))
    warnings = messages(report, Severity.WARNING)
    for name in ("HUB_T_CA", "HUB_T_C", "HUB_T_K"):
        assert any(f"env var {name} is not set" in w for w in warnings)
