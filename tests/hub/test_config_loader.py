from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from hub.config_loader import Severity, load_config
from tests.hub.builders import (
    WriteConfig,
    load,
    messages,
    modbus_device,
    mqtt_device,
    opcua_device,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


class _EverythingSet(Mapping[str, str]):
    """An environment in which every variable is set."""

    def __getitem__(self, key: str) -> str:
        return "set"

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


# --- shipped examples -----------------------------------------------------------------------


def test_repository_examples_are_valid() -> None:
    report = load_config(REPO_CONFIG, env=_EverythingSet())
    assert messages(report) == []
    protocols = sorted(d.device.protocol for d in report.devices)
    assert protocols == ["http", "modbus_tcp", "mqtt", "opcua", "s7", "snmp"]


def test_repository_examples_warn_about_unset_env_vars() -> None:
    report = load_config(REPO_CONFIG, env={})
    assert not report.has_errors
    assert any("HUB_OPCUA_PLC01_PASSWORD is not set" in m for m in messages(report))


def test_valid_mixed_config(write_config: WriteConfig) -> None:
    report = load(write_config(modbus_device(), mqtt_device(), opcua_device()))
    assert messages(report) == []
    assert len(report.devices) == 3


# --- file-level problems --------------------------------------------------------------------


def test_invalid_yaml(write_config: WriteConfig) -> None:
    report = load(write_config(files={"broken.yaml": "device_id: [unclosed\n"}))
    assert report.has_errors
    assert "invalid YAML" in messages(report)[0]


def test_duplicate_yaml_key(write_config: WriteConfig) -> None:
    text = "device_id: a\ndevice_id: b\n"
    report = load(write_config(files={"dup.yaml": text}))
    assert any("duplicate key 'device_id'" in m for m in messages(report, Severity.ERROR))


@pytest.mark.parametrize("text", ["", "- just\n- a list\n"])
def test_device_file_must_be_mapping(write_config: WriteConfig, text: str) -> None:
    report = load(write_config(files={"empty.yaml": text}))
    assert "devices/empty.yaml | None | a device file must contain one YAML mapping" in (
        messages(report)
    )


def test_schema_errors_point_at_tag(write_config: WriteConfig) -> None:
    device = modbus_device()
    device["tags"][1]["address"] = 70000
    report = load(write_config(device))
    assert any(
        m.startswith("devices/meter_01.yaml | tags[1] (current).address |")
        for m in messages(report, Severity.ERROR)
    )


def test_read_write_without_limits_is_error(write_config: WriteConfig) -> None:
    device = modbus_device()
    device["tags"].append({"name": "setpoint", "data_type": "uint16",
                           "register_type": "holding", "address": 0, "access": "read_write"})
    report = load(write_config(device))
    assert messages(report, Severity.ERROR) == [
        "devices/meter_01.yaml | tags[2] (setpoint) | read_write tags must define both min "
        "and max (engineering units); the control-safety layer rejects writes outside this "
        "range"
    ]
    assert report.devices == []  # an invalid device is never handed to the hub


def test_file_name_should_match_device_id(write_config: WriteConfig, tmp_path: Path) -> None:
    write_config(modbus_device())
    (tmp_path / "devices" / "meter_01.yaml").rename(tmp_path / "devices" / "meter-01.yaml")
    report = load(tmp_path)
    assert messages(report) == [
        "devices/meter-01.yaml | None | file name should match device_id: "
        "rename to meter_01.yaml"
    ]
    assert not report.has_errors


# --- cross-device checks --------------------------------------------------------------------


def test_duplicate_device_id_across_files(write_config: WriteConfig) -> None:
    other_site = modbus_device(site="bandar_abbas", connection={"host": "10.0.0.9",
                                                                "unit_id": 1})
    report = load(write_config(modbus_device(), other_site))
    errors = messages(report, Severity.ERROR)
    assert len(errors) == 2
    assert all("duplicate device_id 'meter_01'" in e for e in errors)


def test_duplicate_tag_names(write_config: WriteConfig) -> None:
    device = modbus_device()
    device["tags"][1]["name"] = "voltage"
    report = load(write_config(device))
    assert messages(report, Severity.ERROR) == [
        "devices/meter_01.yaml | tags[1] (voltage).name | duplicate tag name 'voltage' "
        "(first used at tags[0])"
    ]


def test_overlapping_modbus_registers(write_config: WriteConfig) -> None:
    device = modbus_device()
    device["tags"][1]["address"] = 1  # float32 at 0..1 and 1..2
    report = load(write_config(device))
    assert messages(report, Severity.ERROR) == [
        "devices/meter_01.yaml | tags[1] (current).address | input range 1..2 overlaps "
        "tags[0] (voltage) at 0..1"
    ]


def _status_word_device() -> dict:
    device = modbus_device()
    device["tags"] = [
        {"name": "status_word", "data_type": "uint16", "register_type": "holding", "address": 10},
        {"name": "running", "data_type": "bool", "register_type": "holding", "address": 10,
         "bit": 0},
        {"name": "fault", "data_type": "bool", "register_type": "holding", "address": 10,
         "bit": 3},
    ]
    return device


def test_bits_may_share_a_register_with_the_word(write_config: WriteConfig) -> None:
    assert messages(load(write_config(_status_word_device()))) == []


def test_same_bit_twice_is_error(write_config: WriteConfig) -> None:
    device = _status_word_device()
    device["tags"][2]["bit"] = 0
    assert messages(load(write_config(device)), Severity.ERROR) == [
        "devices/meter_01.yaml | tags[2] (fault).bit | bit 0 of holding 10 is already used by "
        "tags[1] (running)"
    ]


def test_same_bit_in_other_register_is_fine(write_config: WriteConfig) -> None:
    device = _status_word_device()
    device["tags"][2].update(bit=0, address=11)
    assert messages(load(write_config(device))) == []


def test_same_address_in_different_register_types_is_fine(write_config: WriteConfig) -> None:
    device = modbus_device()
    device["tags"][1]["register_type"] = "holding"
    device["tags"][1]["address"] = 0
    assert messages(load(write_config(device))) == []


def test_tag_larger_than_max_registers_per_read(write_config: WriteConfig) -> None:
    device = modbus_device()
    device["connection"]["max_registers_per_read"] = 1
    report = load(write_config(device))
    errors = messages(report, Severity.ERROR)
    assert len(errors) == 2
    assert "needs 2 registers but connection.max_registers_per_read is 1" in errors[0]


def test_duplicate_modbus_endpoint(write_config: WriteConfig) -> None:
    report = load(write_config(modbus_device("meter_01"), modbus_device("meter_02")))
    errors = messages(report, Severity.ERROR)
    assert errors == [
        "devices/meter_01.yaml | connection.unit_id | 127.0.0.1:5020 unit_id 1 is also used "
        "by 'meter_02' (devices/meter_02.yaml)",
        "devices/meter_02.yaml | connection.unit_id | 127.0.0.1:5020 unit_id 1 is also used "
        "by 'meter_01' (devices/meter_01.yaml)",
    ]


def test_modbus_endpoint_differs_by_port_or_unit(write_config: WriteConfig) -> None:
    other_port = modbus_device("meter_02", connection={"host": "127.0.0.1", "port": 5021,
                                                       "unit_id": 1})
    other_unit = modbus_device("meter_03", connection={"host": "127.0.0.1", "port": 5020,
                                                       "unit_id": 2})
    assert messages(load(write_config(modbus_device(), other_port, other_unit))) == []


def test_modbus_endpoint_host_is_case_insensitive(write_config: WriteConfig) -> None:
    a = modbus_device("meter_01", connection={"host": "GW.local", "unit_id": 1})
    b = modbus_device("meter_02", connection={"host": "gw.LOCAL", "unit_id": 1})
    assert load(write_config(a, b)).has_errors


def test_collision_with_disabled_device_is_warning(write_config: WriteConfig) -> None:
    report = load(write_config(modbus_device("meter_01"),
                               modbus_device("meter_02", enabled=False)))
    assert not report.has_errors
    warnings = messages(report, Severity.WARNING)
    assert len(warnings) == 2
    assert all("a disabled device is involved" in w for w in warnings)


def test_unknown_broker_ref(write_config: WriteConfig) -> None:
    device = mqtt_device()
    device["connection"]["broker_ref"] = "nope"
    report = load(write_config(device))
    assert messages(report, Severity.ERROR) == [
        "devices/sensor_01.yaml | connection.broker_ref | unknown broker_ref 'nope' "
        "(brokers in brokers.yaml: test_broker)"
    ]


def test_missing_brokers_file(write_config: WriteConfig) -> None:
    report = load(write_config(mqtt_device(), brokers=None))
    assert any("brokers in brokers.yaml: none defined" in m for m in messages(report))


def test_identical_telemetry_topics_collide(write_config: WriteConfig) -> None:
    a = mqtt_device("sensor_01")
    b = mqtt_device("sensor_02")
    b["connection"]["telemetry_topic"] = a["connection"]["telemetry_topic"]
    errors = messages(load(write_config(a, b)), Severity.ERROR)
    assert len(errors) == 2
    assert "collides with telemetry_topic" in errors[0]


def test_wildcard_topic_collides(write_config: WriteConfig) -> None:
    a = mqtt_device("sensor_01")
    b = mqtt_device("sensor_02")
    b["connection"]["telemetry_topic"] = "kish/warehouse/+/telemetry"
    assert load(write_config(a, b)).has_errors


def test_same_topic_on_different_brokers_is_fine(write_config: WriteConfig) -> None:
    a = mqtt_device("sensor_01")
    b = mqtt_device("sensor_02")
    b["connection"]["telemetry_topic"] = a["connection"]["telemetry_topic"]
    b["connection"]["broker_ref"] = "other_broker"
    brokers = [{"broker_id": "test_broker", "host": "a"}, {"broker_id": "other_broker",
                                                             "host": "b"}]
    assert messages(load(write_config(a, b, brokers=brokers))) == []


def test_command_topic_must_not_overlap_telemetry(write_config: WriteConfig) -> None:
    device = mqtt_device()
    device["connection"]["telemetry_topic"] = "kish/warehouse/sensor_01/#"
    device["connection"]["command_topic"] = "kish/warehouse/sensor_01/cmd"
    errors = messages(load(write_config(device)), Severity.ERROR)
    assert errors == [
        "devices/sensor_01.yaml | connection.command_topic | telemetry_topic "
        "'kish/warehouse/sensor_01/#' overlaps command_topic 'kish/warehouse/sensor_01/cmd' "
        "of the same device"
    ]


def test_duplicate_json_path_is_warning(write_config: WriteConfig) -> None:
    device = mqtt_device()
    device["tags"].append({"name": "temperature_f", "data_type": "float32",
                           "json_path": "$.data.temp", "scale": 1.8, "offset": 32})
    report = load(write_config(device))
    assert not report.has_errors
    assert messages(report, Severity.WARNING) == [
        "devices/sensor_01.yaml | tags[1] (temperature_f).json_path | same json_path as "
        "tags[0] (temperature); intentional?"
    ]


# --- brokers and secrets --------------------------------------------------------------------


def test_duplicate_broker_id(write_config: WriteConfig) -> None:
    brokers = [{"broker_id": "test_broker", "host": "a"}, {"broker_id": "test_broker",
                                                            "host": "b"}]
    report = load(write_config(brokers=brokers))
    assert "brokers.yaml | None | duplicate broker_id 'test_broker'" in messages(report)


def test_broker_credentials_without_tls_warn(write_config: WriteConfig) -> None:
    brokers = [{"broker_id": "test_broker", "host": "a", "username_env": "U",
                "password_env": "P"}]
    report = load_config(write_config(brokers=brokers), env={"U": "u", "P": "p"})
    assert messages(report) == [
        "brokers.yaml | brokers[test_broker] | credentials are sent without TLS; "
        "set tls: true"
    ]


def test_opcua_username_without_security_warns(write_config: WriteConfig) -> None:
    device = opcua_device()
    device["connection"]["auth"] = {"type": "username", "username_env": "PLC_USER",
                                    "password_env": "PLC_PASSWORD"}
    report = load(write_config(device))
    assert not report.has_errors
    assert messages(report, Severity.WARNING) == [
        "devices/plc_01.yaml | connection.security_policy | username/password is sent "
        "unencrypted with security_policy None",
        "devices/plc_01.yaml | connection.auth.username_env | env var PLC_USER is not set",
        "devices/plc_01.yaml | connection.auth.password_env | env var PLC_PASSWORD is not set",
    ]


def test_empty_device_warns(write_config: WriteConfig) -> None:
    report = load(write_config(opcua_device(tags=[])))
    assert messages(report) == ["devices/plc_01.yaml | tags | device has no tags"]


def test_mqtt_without_lwt_or_stale_timeout_warns(write_config: WriteConfig) -> None:
    device = mqtt_device()
    del device["connection"]["stale_after_s"]
    warnings = messages(load(write_config(device)), Severity.WARNING)
    assert any("set stale_after_s" in w for w in warnings)


def _rtu(device_id: str, unit_id: int, **conn: object) -> dict:
    return {"device_id": device_id, "name": "RTU meter", "site": "kish", "area": "gate",
            "protocol": "modbus_rtu",
            "connection": {"port": "COM3", "unit_id": unit_id, "baudrate": 9600} | conn,
            "tags": [{"name": "v", "data_type": "uint16", "register_type": "input",
                      "address": 0}]}


def test_rtu_devices_on_one_line(write_config: WriteConfig) -> None:
    assert messages(load(write_config(_rtu("m_a", 1), _rtu("m_b", 2)))) == []


def test_rtu_unit_id_collision(write_config: WriteConfig) -> None:
    errors = messages(load(write_config(_rtu("m_a", 1), _rtu("m_b", 1))), Severity.ERROR)
    assert len(errors) == 2 and all("unit_id 1 is also used" in e for e in errors)


def test_rtu_line_settings_must_match(write_config: WriteConfig) -> None:
    errors = messages(load(write_config(_rtu("m_a", 1), _rtu("m_b", 2, baudrate=19200))),
                      Severity.ERROR)
    assert len(errors) == 2 and all("different line settings" in e for e in errors)
