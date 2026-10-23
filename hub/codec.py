"""Value conversion shared by all drivers.

* Modbus register <-> raw value (16/32/64-bit ints, floats, strings, bits) honouring the device's
  ``byte_order`` / ``word_order`` (see the add-device skill's modbus-addressing reference):
  ``word_order`` = which register comes first, ``byte_order`` = byte order inside each register.
* raw <-> engineering value: ``engineering = raw * scale + offset``.
* Coercion of payload values (MQTT JSON, OPC UA variants) to a tag's ``data_type``.
* Quality from the tag's expected range.

Everything here is pure (no I/O) so it can be tested exhaustively.
"""

from __future__ import annotations

import math
import struct

from hub.models import (
    INTEGER_RANGES,
    MODBUS_WIDTH,
    NUMERIC_TYPES,
    DataType,
    Order,
    Quality,
    TagBase,
)

Value = bool | int | float | str

_STRUCT: dict[DataType, str] = {
    DataType.INT16: "h",
    DataType.UINT16: "H",
    DataType.INT32: "i",
    DataType.UINT32: "I",
    DataType.INT64: "q",
    DataType.UINT64: "Q",
    DataType.FLOAT32: "f",
    DataType.FLOAT64: "d",
}

INTEGER_TYPES = frozenset(INTEGER_RANGES)
FLOAT_TYPES = frozenset({DataType.FLOAT32, DataType.FLOAT64})


class CodecError(ValueError):
    """A value cannot be converted (wrong type, out of range, malformed registers)."""


# --------------------------------------------------------------------------------------------
# Modbus registers
# --------------------------------------------------------------------------------------------


def _swap16(word: int) -> int:
    return ((word & 0xFF) << 8) | (word >> 8)


def _registers_to_bytes(registers: list[int], byte_order: Order, word_order: Order) -> bytes:
    words = list(registers)
    if word_order == "little":
        words.reverse()
    if byte_order == "little":
        words = [_swap16(w) for w in words]
    return b"".join(w.to_bytes(2, "big") for w in words)


def _bytes_to_registers(data: bytes, byte_order: Order, word_order: Order) -> list[int]:
    words = [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]
    if byte_order == "little":
        words = [_swap16(w) for w in words]
    if word_order == "little":
        words.reverse()
    return words


def decode_registers(
    registers: list[int], data_type: DataType, byte_order: Order = "big", word_order: Order = "big"
) -> Value:
    """Decode the raw (unscaled) value of one tag from its registers."""
    if data_type is DataType.BOOL:
        raise CodecError("bool tags are read from coils/discrete inputs or with decode_bit()")
    if any(not 0 <= r <= 0xFFFF for r in registers):
        raise CodecError(f"register values must be 0..65535, got {registers}")
    if data_type is DataType.STRING:
        # Strings are not word-swapped: registers are read in address order.
        data = _registers_to_bytes(registers, byte_order, "big")
        return data.split(b"\x00", 1)[0].decode("ascii", errors="replace").rstrip()
    width = MODBUS_WIDTH[data_type]
    if len(registers) != width:
        raise CodecError(f"{data_type} needs {width} register(s), got {len(registers)}")
    data = _registers_to_bytes(registers, byte_order, word_order)
    return struct.unpack(">" + _STRUCT[data_type], data)[0]


def encode_registers(
    raw: Value, data_type: DataType, byte_order: Order = "big", word_order: Order = "big",
    count: int | None = None,
) -> list[int]:
    """Inverse of :func:`decode_registers` (used by writes and by the simulator)."""
    if data_type is DataType.BOOL:
        raise CodecError("bool tags are written as coils")
    if data_type is DataType.STRING:
        if count is None:
            raise CodecError("string encoding needs count")
        data = str(raw).encode("ascii", errors="replace")[: count * 2].ljust(count * 2, b"\x00")
        return _bytes_to_registers(data, byte_order, "big")
    raw = _check_raw(raw, data_type)
    try:
        data = struct.pack(">" + _STRUCT[data_type], raw)
    except struct.error as exc:
        raise CodecError(f"{raw!r} does not fit {data_type}: {exc}") from exc
    return _bytes_to_registers(data, byte_order, word_order)


def decode_bit(register: int, bit: int) -> bool:
    return bool((register >> bit) & 1)


# --------------------------------------------------------------------------------------------
# Scaling
# --------------------------------------------------------------------------------------------


def _check_raw(raw: Value, data_type: DataType) -> int | float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise CodecError(f"{data_type} needs a number, got {raw!r}")
    if data_type in INTEGER_TYPES:
        if isinstance(raw, float):
            if not raw.is_integer():
                raise CodecError(f"{data_type} needs an integer, got {raw!r}")
            raw = int(raw)
        lo, hi = INTEGER_RANGES[data_type]
        if not lo <= raw <= hi:
            raise CodecError(f"{raw} is outside the {data_type} range [{lo}, {hi}]")
    elif not math.isfinite(raw):
        raise CodecError(f"{raw!r} is not a finite number")
    return raw


def to_engineering(tag: TagBase, raw: Value) -> Value:
    """raw * scale + offset; integers stay integers when no scaling is configured."""
    if tag.data_type not in NUMERIC_TYPES:
        return raw
    if tag.scale == 1.0 and tag.offset == 0.0:
        return raw
    return raw * tag.scale + tag.offset


def to_raw(tag: TagBase, value: Value) -> Value:
    """Engineering value -> raw value for a write. Rounds to the nearest integer for int types."""
    if tag.data_type is DataType.BOOL:
        if not isinstance(value, bool):
            raise CodecError(f"{tag.name} is bool, got {value!r}")
        return value
    if tag.data_type is DataType.STRING:
        raise CodecError("string tags cannot be written")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise CodecError(f"{tag.name} needs a finite number, got {value!r}")
    raw: int | float = (value - tag.offset) / tag.scale
    if tag.data_type in INTEGER_TYPES:
        raw = round(raw)
    return _check_raw(raw, tag.data_type)


# --------------------------------------------------------------------------------------------
# Payload coercion (MQTT JSON / OPC UA)
# --------------------------------------------------------------------------------------------


def coerce(value: object, data_type: DataType) -> Value:
    """Convert a decoded payload value to the tag's data type, strictly.

    JSON numbers for numeric tags, true/false (or 0/1) for bool, anything scalar for string.
    """
    if data_type is DataType.BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float) and value in (0, 1):
            return bool(value)
        raise CodecError(f"expected a boolean, got {value!r}")
    if data_type is DataType.STRING:
        if isinstance(value, str | int | float | bool):
            return str(value)
        raise CodecError(f"expected a scalar for string, got {type(value).__name__}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CodecError(f"expected a number, got {value!r}")
    if data_type in FLOAT_TYPES:
        if not math.isfinite(value):
            raise CodecError(f"expected a finite number, got {value!r}")
        return float(value)
    return _check_raw(value, data_type)


def range_quality(tag: TagBase, value: Value) -> Quality:
    """``uncertain`` when a numeric value lies outside the tag's expected min/max."""
    if tag.data_type not in NUMERIC_TYPES or isinstance(value, bool | str):
        return Quality.GOOD
    if (tag.min is not None and value < tag.min) or (tag.max is not None and value > tag.max):
        return Quality.UNCERTAIN
    return Quality.GOOD


def values_equal(tag: TagBase, a: Value | None, b: Value | None) -> bool:
    """Read-back comparison: exact for bool/int/string, relative tolerance for floats/scaled."""
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str):
        return a == b
    tolerance = abs(tag.scale) / 2 if tag.data_type in INTEGER_TYPES else 0.0
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=max(tolerance, 1e-9))
