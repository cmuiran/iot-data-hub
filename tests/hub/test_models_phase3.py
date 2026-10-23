"""Schema of the phase 3 additions: S7, HTTP, SNMP devices, value states, Modbus bit fields and
BCD, broker TLS, northbound and Modbus server files."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from hub.codec import CodecError, decode_bcd, decode_bit_field, encode_bcd
from hub.models import (
    DEVICE_ADAPTER,
    Broker,
    ModbusServerFile,
    NorthboundFile,
    S7Area,
    parse_s7_address,
)


def s7(*tags: dict[str, Any]) -> dict[str, Any]:
    return {"device_id": "plc_s7", "name": "S7", "site": "kish", "area": "a", "protocol": "s7",
            "connection": {"host": "10.0.0.1"}, "tags": list(tags)}


def tag(**kw: Any) -> dict[str, Any]:
    return {"name": "x"} | kw


# --- S7 addresses ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "area", "db", "byte", "bit", "size"), [
    ("DB1.DBD0", S7Area.DB, 1, 0, None, "D"),
    ("db12.dbx6.7", S7Area.DB, 12, 6, 7, "X"),
    ("DB3.DBB100", S7Area.DB, 3, 100, None, "B"),
    ("M10.0", S7Area.MERKER, 0, 10, 0, "X"),
    ("MW20", S7Area.MERKER, 0, 20, None, "W"),
    ("IW64", S7Area.INPUT, 0, 64, None, "W"),
    ("E1.3", S7Area.INPUT, 0, 1, 3, "X"),        # German mnemonic
    ("A0.0", S7Area.OUTPUT, 0, 0, 0, "X"),
    ("QD4", S7Area.OUTPUT, 0, 4, None, "D"),
])
def test_s7_addresses(text: str, area: S7Area, db: int, byte: int, bit: int | None,
                      size: str) -> None:
    a = parse_s7_address(text)
    assert (a.area, a.db, a.byte, a.bit, a.size) == (area, db, byte, bit, size)


@pytest.mark.parametrize("text", ["DB1.DBX6", "DB1.DBD0.1", "MB10.0", "M10", "DB0.DBW0",
                                  "M10.8", "X1", "DB1.DBQ0", ""])
def test_s7_bad_addresses(text: str) -> None:
    with pytest.raises(ValueError):
        parse_s7_address(text)


@pytest.mark.parametrize(("address", "data_type", "ok"), [
    ("DB1.DBD0", "float32", True), ("DB1.DBD0", "int16", False),
    ("DB1.DBW0", "int16", True), ("DB1.DBW0", "float32", False),
    ("DB1.DBX0.0", "bool", True), ("DB1.DBX0.0", "uint16", False),
    ("DB1.DBB0", "uint16", True), ("DB1.DBB0", "float64", True), ("MD4", "uint32", True),
])
def test_s7_type_must_fit_the_size(address: str, data_type: str, ok: bool) -> None:
    data = s7(tag(address=address, data_type=data_type))
    if ok:
        DEVICE_ADAPTER.validate_python(data)
    else:
        with pytest.raises(ValidationError, match="cannot hold"):
            DEVICE_ADAPTER.validate_python(data)


def test_s7_string_and_byte_length() -> None:
    d = DEVICE_ADAPTER.validate_python(s7(
        tag(name="s", address="DB2.DBB0", data_type="string", string_length=10),
        tag(name="l", address="DB2.DBB20", data_type="float64"),
        tag(name="w", address="DB2.DBW30", data_type="int16")))
    assert [t.byte_length for t in d.tags] == [12, 8, 2]
    with pytest.raises(ValidationError, match="string_length"):
        DEVICE_ADAPTER.validate_python(s7(tag(address="DB2.DBB0", data_type="string")))
    with pytest.raises(ValidationError, match="read-only"):
        DEVICE_ADAPTER.validate_python(s7(tag(address="I0.0", data_type="bool",
                                              access="read_write")))


# --- HTTP / SNMP ----------------------------------------------------------------------------


def http(conn: dict[str, Any], *tags: dict[str, Any]) -> dict[str, Any]:
    return {"device_id": "web", "name": "W", "site": "kish", "area": "a", "protocol": "http",
            "connection": {"base_url": "https://10.0.0.2"} | conn,
            "tags": list(tags) or [tag(data_type="float32", path="/s", json_path="$.v")]}


@pytest.mark.parametrize(("conn", "message"), [
    ({"base_url": "ftp://x"}, "not an http"),
    ({"base_url": "https://x/api?key=1"}, "not an http"),
    ({"headers": {"Authorization": "Bearer x"}}, "credentials do not belong"),
    ({"verify_tls": False, "ca_file_env": "HUB_CA"}, "only used when verify_tls"),
    ({"timestamp_path": "$.ts"}, "go together"),
])
def test_http_connection_rules(conn: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DEVICE_ADAPTER.validate_python(http(conn))


def test_http_tags_are_read_only_and_paths_relative() -> None:
    with pytest.raises(ValidationError, match="read-only"):
        DEVICE_ADAPTER.validate_python(http({}, tag(data_type="float32", path="/s",
                                                    json_path="$.v", access="read_write",
                                                    min=0, max=1)))
    with pytest.raises(ValidationError, match="relative"):
        DEVICE_ADAPTER.validate_python(http({}, tag(data_type="float32", path="http://x/s",
                                                    json_path="$.v")))
    d = DEVICE_ADAPTER.validate_python(http({"base_url": "https://10.0.0.2/api/"}))
    assert d.connection.base_url == "https://10.0.0.2/api"


def snmp(security: dict[str, Any], *tags: dict[str, Any]) -> dict[str, Any]:
    return {"device_id": "ups", "name": "U", "site": "kish", "area": "a", "protocol": "snmp",
            "connection": {"host": "10.0.0.3", "security": security},
            "tags": list(tags) or [tag(data_type="uint16", oid="1.3.6.1.2.1.33.1.2.4.0")]}


def test_snmp_security_rules() -> None:
    DEVICE_ADAPTER.validate_python(snmp({"version": "2c", "community_env": "HUB_C"}))
    DEVICE_ADAPTER.validate_python(snmp({"version": "3", "username": "u",
                                         "auth_protocol": "sha256", "auth_key_env": "HUB_A",
                                         "priv_protocol": "aes128", "priv_key_env": "HUB_P"}))
    for bad, message in [
        ({"version": "3", "username": "u", "priv_protocol": "aes128", "priv_key_env": "HUB_P"},
         "needs authentication"),
        ({"version": "3", "username": "u", "auth_protocol": "sha"}, "go together"),
        ({"version": "2c", "community_env": "public"}, "environment variable name"),
    ]:
        with pytest.raises(ValidationError, match=message):
            DEVICE_ADAPTER.validate_python(snmp(bad))


def test_snmp_oids() -> None:
    d = DEVICE_ADAPTER.validate_python(snmp({"version": "2c", "community_env": "HUB_C"},
                                            tag(data_type="uint16", oid=".1.3.6.1.2.1.1.3.0")))
    assert d.tags[0].oid == "1.3.6.1.2.1.1.3.0"
    for oid in ("sysUpTime.0", "1.3", "1.3.6.01.1", "4.1.2"):
        with pytest.raises(ValidationError):
            DEVICE_ADAPTER.validate_python(snmp({"version": "2c", "community_env": "HUB_C"},
                                                tag(data_type="uint16", oid=oid)))


# --- states, bit fields, BCD ----------------------------------------------------------------


def modbus(*tags: dict[str, Any]) -> dict[str, Any]:
    return {"device_id": "m", "name": "M", "site": "kish", "area": "a", "protocol": "modbus_tcp",
            "connection": {"host": "10.0.0.4", "unit_id": 1}, "tags": list(tags)}


def test_states_rules() -> None:
    DEVICE_ADAPTER.validate_python(modbus(tag(data_type="uint16", register_type="holding",
                                              address=0, states={0: "stop", 1: "run"})))
    DEVICE_ADAPTER.validate_python(modbus(tag(data_type="bool", register_type="coil", address=0,
                                              states={0: "closed", 1: "open"})))
    cases = [
        (tag(data_type="float32", register_type="input", address=0, states={0: "a"}),
         "integer and bool"),
        (tag(data_type="bool", register_type="coil", address=0, states={2: "a"}), "keys 0"),
        (tag(data_type="uint16", register_type="input", address=0, scale=0.1,
             states={0: "a"}), "scale/offset"),
        (tag(data_type="uint16", register_type="input", address=0, states={0: False}),
         "quote words"),
    ]
    for t, message in cases:
        with pytest.raises(ValidationError, match=message):
            DEVICE_ADAPTER.validate_python(modbus(t))


def test_bit_fields_and_bcd() -> None:
    d = DEVICE_ADAPTER.validate_python(modbus(
        tag(name="mode", data_type="uint16", register_type="holding", address=5, bit=4,
            bit_count=3),
        tag(name="counter", data_type="uint32", register_type="holding", address=10,
            encoding="bcd", access="read_write", min=0, max=99_999_999)))
    assert d.tags[0].bit_count == 3 and d.tags[1].register_count == 2
    cases = [
        (tag(data_type="uint16", register_type="holding", address=0, bit_count=3),
         "needs bit"),
        (tag(data_type="uint16", register_type="holding", address=0, bit=14, bit_count=3),
         "exceed"),
        (tag(data_type="int32", register_type="holding", address=0, bit=0, bit_count=3),
         "uint16"),
        (tag(data_type="float32", register_type="holding", address=0, encoding="bcd"),
         "uint16 \\(4 digits\\)"),
        (tag(data_type="uint16", register_type="holding", address=0, encoding="bcd",
             access="read_write", min=0, max=20000), "does not fit"),
    ]
    for t, message in cases:
        with pytest.raises(ValidationError, match=message):
            DEVICE_ADAPTER.validate_python(modbus(t))


def test_bcd_and_bit_field_codec() -> None:
    assert decode_bcd([0x1234]) == 1234
    assert decode_bcd([0x0012, 0x3456]) == 123456
    assert decode_bcd([0x3456, 0x0012], "little") == 123456
    for value in (0, 7, 42, 9999, 1234_5678, 99_999_999):
        regs = 1 if value <= 9999 else 2
        assert decode_bcd(encode_bcd(value, regs), "big") == value
        assert decode_bcd(encode_bcd(value, regs, "little"), "little") == value
    with pytest.raises(CodecError, match="not BCD"):
        decode_bcd([0x12A4])
    with pytest.raises(CodecError):
        encode_bcd(10000, 1)
    with pytest.raises(CodecError):
        encode_bcd(1.5, 1)
    assert decode_bit_field(0b0000_0000_0111_0000, 4, 3) == 7
    assert decode_bit_field(0xFFFF, 0, 16) == 0xFFFF


# --- broker TLS, northbound, Modbus server --------------------------------------------------


def test_broker_tls_rules() -> None:
    Broker(broker_id="b", host="h", tls=True, ca_file_env="HUB_CA",
           client_certificate_env="HUB_C", client_private_key_env="HUB_K",
           tls_server_name="mqtt.kish.local")
    with pytest.raises(ValidationError, match="need tls"):
        Broker(broker_id="b", host="h", ca_file_env="HUB_CA")
    with pytest.raises(ValidationError, match="go together"):
        Broker(broker_id="b", host="h", tls=True, client_certificate_env="HUB_C")


def test_northbound_file() -> None:
    f = NorthboundFile.model_validate({"targets": [{"name": "ho", "broker_ref": "b",
                                                    "tags": ["kish.substation.*"]}]})
    assert f.targets[0].topic_prefix == "hub" and f.targets[0].heartbeat_s == 300
    for bad in ({"name": "ho", "broker_ref": "b", "topic_prefix": "a/#"},
                {"name": "ho", "broker_ref": "b", "tags": ["KISH.*"]},
                {"name": "ho", "broker_ref": "b", "qos": 2}):
        with pytest.raises(ValidationError):
            NorthboundFile.model_validate({"targets": [bad]})


def test_modbus_server_file() -> None:
    ok = {"points": [
        {"tag": "kish.a.m.v", "register_type": "input", "address": 0, "data_type": "float32"},
        {"tag": "kish.a.m.v", "register_type": "input", "address": 2, "source": "quality"},
        {"tag": "kish.a.m.sp", "register_type": "holding", "address": 10, "data_type": "uint16",
         "writable": True}],
        "writes": {"as_user": "scada_link", "allowed_clients": ["10.0.0.0/24"]}}
    ModbusServerFile.model_validate(ok)
    cases = [
        ({"points": [ok["points"][2]]}, "need a writes section"),
        ({"points": [{"tag": "kish.a.m.v", "register_type": "input", "address": 0,
                      "writable": True}], "writes": ok["writes"]}, "read-only in Modbus"),
        ({"points": [{"tag": "kish.a.m.v", "register_type": "coil", "address": 0,
                      "data_type": "uint16"}]}, "bool values"),
        ({"points": [{"tag": "kish.a.m.v", "register_type": "coil", "address": 0,
                      "source": "quality"}]}, "quality point"),
        ({"allowed_clients": ["not-an-ip"]}, "does not appear to be"),
    ]
    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            ModbusServerFile.model_validate(data)
