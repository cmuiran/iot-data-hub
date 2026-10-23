from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from hub.models import (
    DEVICE_ADAPTER,
    QUALITY_CODES,
    Broker,
    ModbusDevice,
    ModbusTag,
    MqttConnection,
    MqttTag,
    OpcUaConnection,
    OpcUaTag,
    Quality,
    Sample,
    TimestampFormat,
    make_tag_path,
    parse_device_timestamp,
    parse_json_path,
    topics_overlap,
    validate_segment,
)
from tests.hub.builders import modbus_device, mqtt_device, opcua_device


def modbus_tag(**overrides: Any) -> dict[str, Any]:
    tag = {"name": "speed", "data_type": "uint16", "register_type": "holding", "address": 10}
    tag.update(overrides)
    return tag


# --- naming ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kish", "plc_01", "motor_speed", "a" * 64])
def test_valid_segments(name: str) -> None:
    assert validate_segment(name) == name


@pytest.mark.parametrize(
    "name", ["", "Kish", "plc-01", "01_plc", "_plc", "motor speed", "دما", "café", "a" * 65]
)
def test_invalid_segments(name: str) -> None:
    with pytest.raises(ValueError, match="not a valid name"):
        validate_segment(name)


def test_tag_path() -> None:
    assert make_tag_path("kish", "pump_station", "plc_01", "motor_speed") == (
        "kish.pump_station.plc_01.motor_speed"
    )
    device = ModbusDevice.model_validate(modbus_device())
    assert device.tag_path("voltage") == "kish.substation.meter_01.voltage"


def test_device_id_is_validated() -> None:
    with pytest.raises(ValidationError, match="not a valid name"):
        DEVICE_ADAPTER.validate_python(modbus_device("Meter-01"))


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DEVICE_ADAPTER.validate_python(modbus_device(poll_intervall_ms=500))


def test_mqtt_device_has_no_poll_interval() -> None:
    with pytest.raises(ValidationError, match="poll_interval_ms"):
        DEVICE_ADAPTER.validate_python(mqtt_device(poll_interval_ms=1000))


def test_unknown_protocol() -> None:
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        DEVICE_ADAPTER.validate_python(modbus_device(protocol="bacnet"))


def test_discriminated_union_picks_model() -> None:
    assert type(DEVICE_ADAPTER.validate_python(mqtt_device())).__name__ == "MqttDevice"
    assert type(DEVICE_ADAPTER.validate_python(opcua_device())).__name__ == "OpcUaDevice"


# --- value semantics (all protocols) --------------------------------------------------------


def test_tags_are_read_only_by_default() -> None:
    assert ModbusTag.model_validate(modbus_tag()).access == "read"


def test_read_write_requires_min_and_max() -> None:
    with pytest.raises(ValidationError, match="must define both min and max"):
        ModbusTag.model_validate(modbus_tag(access="read_write", min=0))
    tag = ModbusTag.model_validate(modbus_tag(access="read_write", min=0, max=1500))
    assert tag.max == 1500


def test_read_write_bool_needs_no_range() -> None:
    tag = ModbusTag.model_validate(
        modbus_tag(data_type="bool", register_type="coil", access="read_write")
    )
    assert tag.access == "read_write"


def test_read_write_string_is_rejected() -> None:
    with pytest.raises(ValidationError, match="read_write string tags are not supported"):
        MqttTag.model_validate(
            {"name": "label", "data_type": "string", "json_path": "$.l", "access": "read_write"}
        )


def test_min_must_be_below_max() -> None:
    with pytest.raises(ValidationError, match="must be less than max"):
        ModbusTag.model_validate(modbus_tag(min=10, max=10))


def test_range_must_fit_raw_type() -> None:
    # uint16 with scale 0.1 can represent at most 6553.5
    with pytest.raises(ValidationError, match="outside the uint16 range"):
        ModbusTag.model_validate(modbus_tag(scale=0.1, min=0, max=7000))
    ModbusTag.model_validate(modbus_tag(scale=0.1, min=0, max=6000))


@pytest.mark.parametrize("field,value", [("scale", 2.0), ("offset", 1.0), ("min", 0)])
def test_scaling_not_allowed_on_bool(field: str, value: float) -> None:
    with pytest.raises(ValidationError, match="only apply to numeric tags"):
        ModbusTag.model_validate(
            modbus_tag(data_type="bool", register_type="coil", **{field: value})
        )


def test_scale_zero_rejected() -> None:
    with pytest.raises(ValidationError, match="non-zero"):
        ModbusTag.model_validate(modbus_tag(scale=0))


# --- Modbus addressing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "data_type,expected", [("int16", 1), ("uint32", 2), ("float32", 2), ("float64", 4)]
)
def test_register_count_derived_from_type(data_type: str, expected: int) -> None:
    assert ModbusTag.model_validate(modbus_tag(data_type=data_type)).register_count == expected


def test_count_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="occupies 2 register"):
        ModbusTag.model_validate(modbus_tag(data_type="float32", count=1))


def test_string_needs_count() -> None:
    with pytest.raises(ValidationError, match="string tags need count"):
        ModbusTag.model_validate(modbus_tag(data_type="string"))
    assert ModbusTag.model_validate(modbus_tag(data_type="string", count=8)).register_count == 8


def test_bit_areas_require_bool() -> None:
    with pytest.raises(ValidationError, match="coil tags must have data_type bool"):
        ModbusTag.model_validate(modbus_tag(register_type="coil"))
    with pytest.raises(ValidationError, match="bool tags must use register_type coil"):
        ModbusTag.model_validate(modbus_tag(data_type="bool"))


@pytest.mark.parametrize("register_type", ["holding", "input"])
def test_bit_in_register(register_type: str) -> None:
    tag = ModbusTag.model_validate(
        modbus_tag(data_type="bool", register_type=register_type, bit=15)
    )
    assert tag.bit == 15
    assert tag.register_count == 1


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"data_type": "uint16", "bit": 3}, "must have data_type bool"),
        ({"data_type": "bool", "register_type": "coil", "bit": 0}, "already a single bit"),
        ({"data_type": "bool", "bit": 16}, "less than or equal to 15"),
        ({"data_type": "bool", "bit": -1}, "greater than or equal to 0"),
        ({"data_type": "bool", "bit": 2, "access": "read_write"}, "read-modify-write"),
    ],
)
def test_bit_in_register_invalid(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ModbusTag.model_validate(modbus_tag(**overrides))


@pytest.mark.parametrize("data_type", ["int64", "uint64"])
def test_64_bit_integers_use_four_registers(data_type: str) -> None:
    tag = ModbusTag.model_validate(modbus_tag(data_type=data_type))
    assert tag.register_count == 4
    assert tag.last_address == 13


def test_uint64_range_check() -> None:
    with pytest.raises(ValidationError, match="outside the uint64 range"):
        ModbusTag.model_validate(
            modbus_tag(data_type="uint64", access="read_write", min=-1, max=10)
        )


@pytest.mark.parametrize("register_type,data_type", [("input", "uint16"),
                                                     ("discrete_input", "bool")])
def test_read_only_areas_cannot_be_written(register_type: str, data_type: str) -> None:
    extra = {"min": 0, "max": 10} if data_type != "bool" else {}
    with pytest.raises(ValidationError, match="read-only in Modbus"):
        ModbusTag.model_validate(
            modbus_tag(register_type=register_type, data_type=data_type,
                       access="read_write", **extra)
        )


def test_address_range_overflow() -> None:
    with pytest.raises(ValidationError, match="exceeds 65535"):
        ModbusTag.model_validate(modbus_tag(data_type="float64", address=65533))


# --- MQTT -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,parts",
    [
        ("$.data.temp", ("data", "temp")),
        ("$.values[2].v", ("values", 2, "v")),
        ("$['outdoor temp']", ("outdoor temp",)),
        ("$.a-b", ("a-b",)),
    ],
)
def test_json_path_valid(path: str, parts: tuple[str | int, ...]) -> None:
    assert parse_json_path(path) == parts


@pytest.mark.parametrize("path", ["data.temp", "$", "$.", "$..temp", "$.a[x]", "$.a[*]", "$[-1]"])
def test_json_path_invalid(path: str) -> None:
    with pytest.raises(ValueError):
        parse_json_path(path)


def test_mqtt_command_topic_rejects_wildcards() -> None:
    with pytest.raises(ValidationError, match="wildcards are not allowed"):
        MqttConnection.model_validate(
            {"broker_ref": "b", "telemetry_topic": "a/b", "command_topic": "a/+/cmd"}
        )


def test_mqtt_timestamp_path() -> None:
    conn = MqttConnection.model_validate(
        {"broker_ref": "b", "telemetry_topic": "a/b", "timestamp_path": "$.ts",
         "timestamp_format": "unix_s"}
    )
    assert conn.timestamp_format == "unix_s"


@pytest.mark.parametrize(
    "extra,match",
    [
        ({"timestamp_path": "$.ts"}, "go together"),
        ({"timestamp_format": "unix_ms"}, "go together"),
        ({"timestamp_path": "ts", "timestamp_format": "unix_s"}, "must start with"),
        ({"timestamp_path": "$.ts", "timestamp_format": "epoch"}, "unix_s"),
    ],
)
def test_mqtt_timestamp_invalid(extra: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        MqttConnection.model_validate({"broker_ref": "b", "telemetry_topic": "a/b"} | extra)


@pytest.mark.parametrize(
    "value,fmt",
    [
        (1767225600, TimestampFormat.UNIX_S),
        (1767225600.0, TimestampFormat.UNIX_S),
        (1767225600000, TimestampFormat.UNIX_MS),
        ("2026-01-01T00:00:00Z", TimestampFormat.ISO8601),
        ("2026-01-01T03:30:00+03:30", TimestampFormat.ISO8601),
    ],
)
def test_parse_device_timestamp(value: object, fmt: TimestampFormat) -> None:
    assert parse_device_timestamp(value, fmt) == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "value,fmt,match",
    [
        ("2026-01-01T00:00:00", TimestampFormat.ISO8601, "no UTC offset"),
        (1767225600, TimestampFormat.ISO8601, "ISO 8601 string"),
        ("1767225600", TimestampFormat.UNIX_S, "expected a number"),
        (True, TimestampFormat.UNIX_MS, "expected a number"),
        (float("nan"), TimestampFormat.UNIX_S, "expected a number"),
    ],
)
def test_parse_device_timestamp_invalid(value: object, fmt: TimestampFormat, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_device_timestamp(value, fmt)


@pytest.mark.parametrize("topic", ["", "a/#/b", "a/b#", "a/b+/c"])
def test_mqtt_invalid_topic_filters(topic: str) -> None:
    with pytest.raises(ValidationError):
        MqttConnection.model_validate({"broker_ref": "b", "telemetry_topic": topic})


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("a/b", "a/b", True),
        ("a/b", "a/c", False),
        ("a/+", "a/b", True),
        ("a/+", "a", False),
        ("a/#", "a", True),
        ("a/#", "a/b/c", True),
        ("+/b", "a/+", True),
        ("a/b/c", "a/b", False),
        ("#", "x/y", True),
    ],
)
def test_topics_overlap(a: str, b: str, expected: bool) -> None:
    assert topics_overlap(a, b) is expected
    assert topics_overlap(b, a) is expected


# --- OPC UA ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node_id",
    ["i=2258", "ns=2;s=Pump1.Speed", "nsu=urn:x:y;s=A B", "s=Speed",
     "ns=3;g=09087e75-8e5e-499b-954f-f2a9603db28a", "ns=1;b=M/RbKBsRVkePCePcx24oRA=="],
)
def test_node_id_valid(node_id: str) -> None:
    OpcUaTag.model_validate({"name": "t", "data_type": "float32", "node_id": node_id})


@pytest.mark.parametrize("node_id", ["", "Speed", "ns=2;Speed", "ns=x;i=1", "ns=2;i=abc"])
def test_node_id_invalid(node_id: str) -> None:
    with pytest.raises(ValidationError, match="not an OPC UA NodeId"):
        OpcUaTag.model_validate({"name": "t", "data_type": "float32", "node_id": node_id})


def opcua_conn(**overrides: Any) -> dict[str, Any]:
    conn = {"endpoint_url": "opc.tcp://plc:4840", "security_policy": "None",
            "security_mode": "None"}
    conn.update(overrides)
    return conn


def test_opcua_policy_and_mode_must_agree() -> None:
    with pytest.raises(ValidationError, match="must both be None or both be set"):
        OpcUaConnection.model_validate(opcua_conn(security_mode="SignAndEncrypt"))


def test_opcua_secure_policy_needs_certificate() -> None:
    secure = opcua_conn(security_policy="Basic256Sha256", security_mode="SignAndEncrypt")
    with pytest.raises(ValidationError, match="client_certificate_env"):
        OpcUaConnection.model_validate(secure)
    OpcUaConnection.model_validate(
        secure | {"client_certificate_env": "CERT", "client_private_key_env": "KEY"}
    )


@pytest.mark.parametrize("url", ["http://plc:4840", "opc.tcp://", "opc.tcp://plc:port"])
def test_opcua_bad_endpoint(url: str) -> None:
    with pytest.raises(ValidationError, match="endpoint URL"):
        OpcUaConnection.model_validate(opcua_conn(endpoint_url=url))


def test_opcua_auth_takes_env_names_not_secrets() -> None:
    with pytest.raises(ValidationError, match="not an environment variable name"):
        OpcUaConnection.model_validate(
            opcua_conn(auth={"type": "username", "username_env": "PLC_USER",
                             "password_env": "hunter2!"})
        )


def test_opcua_unknown_auth_type() -> None:
    with pytest.raises(ValidationError):
        OpcUaConnection.model_validate(opcua_conn(auth={"type": "certificate"}))


# --- brokers / samples ----------------------------------------------------------------------


def test_broker_password_requires_username() -> None:
    with pytest.raises(ValidationError, match="password_env requires username_env"):
        Broker.model_validate({"broker_id": "b", "host": "h", "password_env": "PW"})


def test_sample_timestamp_normalized_to_utc() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    sample = Sample(tag="kish.a.plc_01.speed", value=1.5,
                    timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=tehran))
    assert sample.timestamp.utcoffset() == timedelta(0)
    assert sample.timestamp.hour == 8


def test_quality_codes_cover_every_quality() -> None:
    assert set(QUALITY_CODES) == set(Quality)
    assert sorted(QUALITY_CODES.values()) == [0, 1, 2]


def test_sample_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        Sample(tag="kish.a.plc_01.speed", value=1, timestamp=datetime(2026, 1, 1))


# --- history / RTU / staleness (0.3) ----------------------------------------------------------


def test_history_deadband_numeric_only() -> None:
    ModbusTag.model_validate(modbus_tag(history_deadband=0.5))
    with pytest.raises(ValidationError, match="history_deadband only applies"):
        ModbusTag.model_validate(modbus_tag(data_type="bool", register_type="coil",
                                            history_deadband=1))


@pytest.mark.parametrize("port", ["COM3", "COM12", "/dev/ttyUSB0", "rfc2217://10.0.0.5:4001",
                                  "socket://127.0.0.1:5020"])
def test_serial_ports(port: str) -> None:
    device = DEVICE_ADAPTER.validate_python(modbus_device(
        protocol="modbus_rtu", connection={"port": port, "unit_id": 1}))
    assert device.connection.port == port and device.connection.baudrate == 9600


@pytest.mark.parametrize("port", ["COM0", "3", "http://x:1", "/dev/", "COM3 "])
def test_bad_serial_ports(port: str) -> None:
    with pytest.raises(ValidationError, match="not a serial port"):
        DEVICE_ADAPTER.validate_python(modbus_device(
            protocol="modbus_rtu", connection={"port": port, "unit_id": 1}))


def test_rtu_unit_id_range_and_line_settings() -> None:
    with pytest.raises(ValidationError):
        DEVICE_ADAPTER.validate_python(modbus_device(
            protocol="modbus_rtu", connection={"port": "COM3", "unit_id": 0}))
    with pytest.raises(ValidationError):
        DEVICE_ADAPTER.validate_python(modbus_device(
            protocol="modbus_rtu", connection={"port": "COM3", "unit_id": 1, "baudrate": 9601}))


def test_modbus_rtu_over_tcp_framer() -> None:
    device = DEVICE_ADAPTER.validate_python(modbus_device(
        connection={"host": "10.0.0.5", "port": 4001, "unit_id": 3, "framer": "rtu"}))
    assert device.connection.framer == "rtu"


def test_mqtt_stale_after() -> None:
    conn = MqttConnection.model_validate({"broker_ref": "b", "telemetry_topic": "a/b",
                                          "stale_after_s": 90})
    assert conn.stale_after_s == 90
    with pytest.raises(ValidationError):
        MqttConnection.model_validate({"broker_ref": "b", "telemetry_topic": "a/b",
                                       "stale_after_s": 0})
