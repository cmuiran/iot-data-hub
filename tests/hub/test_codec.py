from __future__ import annotations

import math

import pytest

from hub.codec import (
    CodecError,
    coerce,
    decode_bit,
    decode_registers,
    encode_registers,
    range_quality,
    to_engineering,
    to_raw,
    values_equal,
)
from hub.models import DataType, ModbusTag, MqttTag, Quality

# 123.456 as float32 = 0x42F6E979 (see the skill's modbus-addressing reference, section 7)
FLOAT_ORDERS = [
    ("big", "big", [0x42F6, 0xE979]),        # ABCD
    ("big", "little", [0xE979, 0x42F6]),     # CDAB
    ("little", "big", [0xF642, 0x79E9]),     # BADC
    ("little", "little", [0x79E9, 0xF642]),  # DCBA
]


@pytest.mark.parametrize("byte_order,word_order,registers", FLOAT_ORDERS)
def test_float32_word_orders(byte_order: str, word_order: str, registers: list[int]) -> None:
    value = decode_registers(registers, DataType.FLOAT32, byte_order, word_order)
    assert math.isclose(value, 123.456, rel_tol=1e-6)
    assert encode_registers(value, DataType.FLOAT32, byte_order, word_order) == registers


def test_int64_low_word_first() -> None:
    value = 0x0102030405060708
    regs = encode_registers(value, DataType.INT64, "big", "little")
    assert regs == [0x0708, 0x0506, 0x0304, 0x0102]
    assert decode_registers(regs, DataType.INT64, "big", "little") == value


@pytest.mark.parametrize(
    "data_type,value",
    [(DataType.INT16, -2), (DataType.UINT16, 65535), (DataType.INT32, -70000),
     (DataType.UINT32, 4_000_000_000), (DataType.INT64, -(2**40)), (DataType.UINT64, 2**63 + 5),
     (DataType.FLOAT64, -1.25e-3)],
)
@pytest.mark.parametrize("byte_order", ["big", "little"])
@pytest.mark.parametrize("word_order", ["big", "little"])
def test_round_trip(data_type: DataType, value: float, byte_order: str, word_order: str) -> None:
    regs = encode_registers(value, data_type, byte_order, word_order)
    assert decode_registers(regs, data_type, byte_order, word_order) == value


def test_int16_signed() -> None:
    assert decode_registers([0xFFFE], DataType.INT16) == -2
    assert decode_registers([0xFFFE], DataType.UINT16) == 65534


def test_string() -> None:
    regs = encode_registers("ACS580", DataType.STRING, count=4)
    assert regs == [0x4143, 0x5335, 0x3830, 0x0000]
    assert decode_registers(regs, DataType.STRING) == "ACS580"


def test_wrong_register_count() -> None:
    with pytest.raises(CodecError, match="needs 2 register"):
        decode_registers([1], DataType.FLOAT32)


def test_encode_out_of_range() -> None:
    with pytest.raises(CodecError, match="outside the uint16 range"):
        encode_registers(70000, DataType.UINT16)


def test_decode_bit() -> None:
    assert decode_bit(0b1000, 3) is True
    assert decode_bit(0b1000, 0) is False
    assert decode_bit(0x8000, 15) is True


def _tag(**kw: object) -> ModbusTag:
    base = {"name": "t", "data_type": "uint16", "register_type": "holding", "address": 0}
    return ModbusTag.model_validate(base | kw)


def test_scaling_round_trip() -> None:
    tag = _tag(scale=0.01, offset=-10)
    assert to_engineering(tag, 5000) == pytest.approx(40.0)
    assert to_raw(tag, 40.0) == 5000
    assert to_engineering(_tag(), 7) == 7 and isinstance(to_engineering(_tag(), 7), int)


def test_to_raw_rejects_out_of_range_and_bad_types() -> None:
    tag = _tag(scale=0.1)
    with pytest.raises(CodecError, match="outside the uint16 range"):
        to_raw(tag, 7000)
    with pytest.raises(CodecError, match="finite number"):
        to_raw(tag, "5")
    with pytest.raises(CodecError, match="finite number"):
        to_raw(tag, True)
    with pytest.raises(CodecError, match="is bool"):
        to_raw(_tag(data_type="bool", register_type="coil"), 1)


@pytest.mark.parametrize(
    "value,data_type,expected",
    [(True, DataType.BOOL, True), (1, DataType.BOOL, True), (24, DataType.FLOAT32, 24.0),
     (87, DataType.UINT16, 87), (87.0, DataType.UINT16, 87), (12, DataType.STRING, "12")],
)
def test_coerce(value: object, data_type: DataType, expected: object) -> None:
    result = coerce(value, data_type)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize(
    "value,data_type",
    [("on", DataType.BOOL), (2, DataType.BOOL), ("24", DataType.FLOAT32), (True, DataType.INT32),
     (1.5, DataType.UINT16), (-1, DataType.UINT16), (float("inf"), DataType.FLOAT64),
     ({"a": 1}, DataType.STRING)],
)
def test_coerce_rejects(value: object, data_type: DataType) -> None:
    with pytest.raises(CodecError):
        coerce(value, data_type)


def test_range_quality() -> None:
    tag = MqttTag.model_validate({"name": "t", "data_type": "float32", "json_path": "$.t",
                                  "min": -20, "max": 60})
    assert range_quality(tag, 20.0) is Quality.GOOD
    assert range_quality(tag, 61.0) is Quality.UNCERTAIN
    assert range_quality(tag, -21.0) is Quality.UNCERTAIN


def test_values_equal_uses_scale_tolerance() -> None:
    tag = _tag(scale=0.1)
    assert values_equal(tag, 12.3, 12.30000001)
    assert values_equal(tag, 12.3, 12.34)       # within half a raw step
    assert not values_equal(tag, 12.3, 12.4)
    assert not values_equal(tag, None, 1)
    assert values_equal(_tag(data_type="bool", register_type="coil"), True, True)
