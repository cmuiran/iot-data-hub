"""Unified data model and device configuration schema for the Data Hub.

Every structure that crosses a boundary (YAML config, API, driver output) is a Pydantic model
defined here. This module also owns the single tag-name validator (CLAUDE.md section 4); other
modules must import it instead of re-implementing it.

Models in this module validate one object in isolation. Checks that relate several objects
(duplicate ids, overlapping registers, topic collisions, broker references) live in
``hub.config_loader``.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------------------------

SEGMENT_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_SEGMENT_RE = re.compile(SEGMENT_PATTERN)
_TAG_PATH_RE = re.compile(rf"{SEGMENT_PATTERN}(?:\.{SEGMENT_PATTERN}){{3}}")
_ENV_VAR_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


def validate_segment(value: str) -> str:
    """Validate one segment of a tag path (site, area, device_id or tag name)."""
    if not _SEGMENT_RE.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a valid name: use lowercase ASCII snake_case "
            "([a-z0-9_], starting with a letter, at most 64 characters)"
        )
    return value


def validate_tag_path(value: str) -> str:
    """Validate a full tag path ``<site>.<area>.<device_id>.<tag>``."""
    if not _TAG_PATH_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not a valid tag path <site>.<area>.<device_id>.<tag>")
    return value


def make_tag_path(site: str, area: str, device_id: str, tag: str) -> str:
    return validate_tag_path(f"{site}.{area}.{device_id}.{tag}")


def validate_env_var_name(value: str) -> str:
    """Secrets are referenced by environment variable name only, never by value."""
    if not _ENV_VAR_RE.fullmatch(value):
        raise ValueError(
            f"{value!r} is not an environment variable name (expected UPPER_SNAKE_CASE); "
            "put the secret itself in the environment / .env, never in config"
        )
    return value


Segment = Annotated[str, AfterValidator(validate_segment)]
TagPath = Annotated[str, AfterValidator(validate_tag_path)]
EnvVarName = Annotated[str, AfterValidator(validate_env_var_name)]


class _Strict(BaseModel):
    """Base for config models: unknown keys are errors so typos never pass silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------------------------


class DataType(StrEnum):
    BOOL = "bool"
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    INT64 = "int64"
    UINT64 = "uint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"


NUMERIC_TYPES = frozenset(DataType) - {DataType.BOOL, DataType.STRING}

INTEGER_RANGES: dict[DataType, tuple[int, int]] = {
    DataType.INT16: (-(2**15), 2**15 - 1),
    DataType.UINT16: (0, 2**16 - 1),
    DataType.INT32: (-(2**31), 2**31 - 1),
    DataType.UINT32: (0, 2**32 - 1),
    DataType.INT64: (-(2**63), 2**63 - 1),
    DataType.UINT64: (0, 2**64 - 1),
}


class Access(StrEnum):
    READ = "read"
    READ_WRITE = "read_write"


class TagBase(_Strict):
    """Protocol-independent part of a tag.

    Engineering value = raw * scale + offset. ``min``/``max`` are in engineering units:
    for read_write tags they bound what the control-safety layer will accept; for read tags
    they are the expected range (samples outside it may be flagged ``uncertain``).
    """

    name: Segment
    description: str | None = None
    unit: str | None = None
    data_type: DataType
    access: Access = Access.READ
    scale: float = 1.0
    offset: float = 0.0
    min: float | None = None
    max: float | None = None
    history: bool = Field(default=True, description="store samples in the database")
    history_deadband: float | None = Field(
        default=None, ge=0,
        description="store only when the value moved by more than this (engineering units)",
    )
    states: dict[int, Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=64,
        description="labels for the values of an integer or bool tag, "
                    "e.g. {0: stopped, 1: running}",
    )

    @field_validator("states", mode="before")
    @classmethod
    def _states_are_text(cls, value: object) -> object:
        if isinstance(value, dict):
            for key, label in value.items():
                if isinstance(label, bool):
                    raise ValueError(f"label of {key} was read as {label}: quote words like "
                                     "on, off, yes, no in YAML (e.g. 0: \"off\")")
        return value

    @model_validator(mode="after")
    def _check_value_semantics(self) -> TagBase:
        numeric = self.data_type in NUMERIC_TYPES
        if self.states is not None:
            if self.data_type is DataType.BOOL:
                if not set(self.states) <= {0, 1}:
                    raise ValueError("states of a bool tag use the keys 0 (false) and 1 (true)")
            elif self.data_type not in INTEGER_RANGES:
                raise ValueError("states (value labels) apply to integer and bool tags only")
            elif self.scale != 1.0 or self.offset != 0.0:
                raise ValueError("states label raw integer values; "
                                 "do not combine with scale/offset")
        if not numeric and self.history_deadband is not None:
            raise ValueError("history_deadband only applies to numeric tags")
        if not numeric:
            if self.scale != 1.0 or self.offset != 0.0:
                raise ValueError(f"scale/offset only apply to numeric tags, not {self.data_type}")
            if self.min is not None or self.max is not None:
                raise ValueError(f"min/max only apply to numeric tags, not {self.data_type}")
        if self.scale == 0 or not math.isfinite(self.scale) or not math.isfinite(self.offset):
            raise ValueError("scale must be finite and non-zero, offset must be finite")
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError(f"min ({self.min:g}) must be less than max ({self.max:g})")

        if self.access is Access.READ_WRITE:
            if self.data_type is DataType.STRING:
                raise ValueError("read_write string tags are not supported; use access: read")
            if numeric and (self.min is None or self.max is None):
                raise ValueError(
                    "read_write tags must define both min and max (engineering units); "
                    "the control-safety layer rejects writes outside this range"
                )

        if self.data_type in INTEGER_RANGES:
            lo, hi = INTEGER_RANGES[self.data_type]
            for label, value in (("min", self.min), ("max", self.max)):
                if value is None:
                    continue
                raw = (value - self.offset) / self.scale
                if not lo <= raw <= hi:
                    raise ValueError(
                        f"{label}={value:g} corresponds to raw value {raw:g}, outside the "
                        f"{self.data_type} range [{lo}, {hi}] (raw = (value - offset) / scale)"
                    )
        return self


# --- Modbus ---------------------------------------------------------------------------------


class RegisterType(StrEnum):
    COIL = "coil"
    DISCRETE_INPUT = "discrete_input"
    HOLDING = "holding"
    INPUT = "input"


BIT_REGISTER_TYPES = frozenset({RegisterType.COIL, RegisterType.DISCRETE_INPUT})
READ_ONLY_REGISTER_TYPES = frozenset({RegisterType.DISCRETE_INPUT, RegisterType.INPUT})

# Number of 16-bit registers (or bits, for coils/discrete inputs) a value occupies.
MODBUS_WIDTH: dict[DataType, int] = {
    DataType.BOOL: 1,
    DataType.INT16: 1,
    DataType.UINT16: 1,
    DataType.INT32: 2,
    DataType.UINT32: 2,
    DataType.INT64: 4,
    DataType.UINT64: 4,
    DataType.FLOAT32: 2,
    DataType.FLOAT64: 4,
}


BCD_MAX: dict[DataType, int] = {DataType.UINT16: 9999, DataType.UINT32: 99_999_999}


class ModbusTag(TagBase):
    """A Modbus point.

    ``bit`` selects one bit of a 16-bit holding/input register (0 = least significant bit),
    for status words such as "bit 3 = fault". Such tags are bool and read-only: writing one
    bit needs a read-modify-write of the whole register, which the safety layer does not
    support yet.

    ``bit`` + ``bit_count`` select a field of several bits (e.g. bits 4..6 = operating mode)
    as an unsigned integer (data_type uint16, read-only); combine with ``states`` for labels.

    ``encoding: bcd`` reads packed decimal digits (one per nibble), used by older meters and
    drives: uint16 holds 0..9999, uint32 (two registers) 0..99999999.
    """

    register_type: RegisterType
    address: int = Field(ge=0, le=65535, description="0-based protocol address")
    bit: int | None = Field(
        default=None,
        ge=0,
        le=15,
        description="Bit inside a holding/input register (0 = LSB); bool tags only",
    )
    count: int | None = Field(
        default=None,
        ge=1,
        le=125,
        description="Registers to read. Derived from data_type; required only for string.",
    )
    bit_count: int | None = Field(
        default=None, ge=2, le=16,
        description="with bit: width of a multi-bit field starting at bit (uint16, read-only)",
    )
    encoding: Literal["binary", "bcd"] = "binary"

    @property
    def register_count(self) -> int:
        return self.count if self.count is not None else MODBUS_WIDTH[self.data_type]

    @property
    def last_address(self) -> int:
        return self.address + self.register_count - 1

    @model_validator(mode="after")
    def _check_modbus_address(self) -> ModbusTag:
        bit_area = self.register_type in BIT_REGISTER_TYPES
        if bit_area and self.data_type is not DataType.BOOL:
            raise ValueError(f"{self.register_type} tags must have data_type bool")
        if self.bit_count is not None:
            if self.bit is None:
                raise ValueError("bit_count needs bit (the lowest bit of the field)")
            if self.bit + self.bit_count > 16:
                raise ValueError(f"bits {self.bit}..{self.bit + self.bit_count - 1} exceed the "
                                 "16-bit register")
            if self.data_type is not DataType.UINT16:
                raise ValueError("a multi-bit field (bit_count) has data_type uint16")
            if self.access is Access.READ_WRITE:
                raise ValueError("writing part of a register is not supported; use access: read")
        if self.encoding == "bcd":
            if self.data_type not in BCD_MAX:
                raise ValueError("encoding bcd applies to uint16 (4 digits) and uint32 (8 digits)")
            if bit_area or self.bit is not None:
                raise ValueError("encoding bcd applies to whole holding/input registers")
            limit = BCD_MAX[self.data_type]
            for label, value in (("min", self.min), ("max", self.max)):
                if value is not None and not 0 <= (value - self.offset) / self.scale <= limit:
                    raise ValueError(f"{label}={value:g} does not fit {self.data_type} BCD "
                                     f"(raw 0..{limit})")
        if self.bit is not None and self.bit_count is None:
            if bit_area:
                raise ValueError(
                    f"bit only applies to holding/input registers; a {self.register_type} "
                    "is already a single bit"
                )
            if self.data_type is not DataType.BOOL:
                raise ValueError("a tag with bit must have data_type bool (use bit_count for a "
                                 "multi-bit field)")
            if self.access is Access.READ_WRITE:
                raise ValueError(
                    "writing a single register bit is not supported (needs read-modify-write); "
                    "use access: read, or a coil / whole holding register for control"
                )
        elif self.bit is None and not bit_area and self.data_type is DataType.BOOL:
            raise ValueError(
                "bool tags must use register_type coil or discrete_input, "
                "or set bit (0-15) to read one bit of a holding/input register"
            )
        if self.access is Access.READ_WRITE and self.register_type in READ_ONLY_REGISTER_TYPES:
            raise ValueError(f"{self.register_type} is read-only in Modbus; access must be read")

        if self.data_type is DataType.STRING:
            if self.count is None:
                raise ValueError("string tags need count (number of registers, 2 bytes each)")
        elif self.count is not None and self.count != MODBUS_WIDTH[self.data_type]:
            raise ValueError(
                f"count={self.count} does not match {self.data_type}, which occupies "
                f"{MODBUS_WIDTH[self.data_type]} register(s); omit count for non-string tags"
            )
        if self.last_address > 65535:
            raise ValueError(f"address range {self.address}..{self.last_address} exceeds 65535")
        return self


# --- MQTT -----------------------------------------------------------------------------------

_JSON_PATH_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d+)\]|\['([^'\\]+)'\]")


def parse_json_path(path: str) -> tuple[str | int, ...]:
    """Parse the supported JSON path subset: ``$.a.b``, ``$['key with space']``, ``$.list[0]``.

    Returns the sequence of object keys (str) and array indices (int).
    """
    if not path.startswith("$"):
        raise ValueError(f"JSON path {path!r} must start with '$'")
    parts: list[str | int] = []
    pos = 1
    while pos < len(path):
        match = _JSON_PATH_TOKEN_RE.match(path, pos)
        if match is None:
            raise ValueError(
                f"invalid JSON path {path!r} at position {pos}; "
                "supported forms: $.key, $['key'], $.list[0]"
            )
        key, index, quoted = match.groups()
        parts.append(int(index) if index is not None else (key if key is not None else quoted))
        pos = match.end()
    if not parts:
        raise ValueError("JSON path must select a field inside the payload, not '$' itself")
    return tuple(parts)


class MqttTag(TagBase):
    json_path: str

    @field_validator("json_path")
    @classmethod
    def _check_json_path(cls, value: str) -> str:
        parse_json_path(value)
        return value


def validate_topic_filter(topic: str) -> str:
    """Validate an MQTT topic filter (subscriptions; wildcards allowed)."""
    if not topic or len(topic.encode()) > 65535 or "\x00" in topic:
        raise ValueError("MQTT topic must be non-empty, without NUL characters")
    levels = topic.split("/")
    for i, level in enumerate(levels):
        if "#" in level and (level != "#" or i != len(levels) - 1):
            raise ValueError(f"{topic!r}: '#' must be the whole last level")
        if "+" in level and level != "+":
            raise ValueError(f"{topic!r}: '+' must occupy a whole level")
    return topic


def validate_topic_name(topic: str) -> str:
    """Validate an MQTT topic name (publishing; no wildcards)."""
    validate_topic_filter(topic)
    if "+" in topic or "#" in topic:
        raise ValueError(f"{topic!r}: wildcards are not allowed in a topic you publish to")
    return topic


def topics_overlap(a: str, b: str) -> bool:
    """True if some concrete topic would match both topic filters ``a`` and ``b``."""
    la, lb = a.split("/"), b.split("/")
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else None
        y = lb[i] if i < len(lb) else None
        if x == "#" or y == "#":
            return True
        if x is None or y is None:
            return False
        if x != y and x != "+" and y != "+":
            return False
    return True


# --- OPC UA ---------------------------------------------------------------------------------

_GUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_NODE_ID_RE = re.compile(
    rf"(?:(?:ns=\d+|nsu=[^;]+);)?(?:i=\d+|s=.+|g={_GUID}|b=[A-Za-z0-9+/]+=*)"
)


class OpcUaTag(TagBase):
    node_id: str = Field(description="e.g. 'ns=2;s=Pump1.Speed' or 's=Pump1.Speed'")

    @field_validator("node_id")
    @classmethod
    def _check_node_id(cls, value: str) -> str:
        if not _NODE_ID_RE.fullmatch(value):
            raise ValueError(
                f"{value!r} is not an OPC UA NodeId "
                "(expected [ns=<n>;|nsu=<uri>;] followed by i=, s=, g= or b=)"
            )
        return value


# --- Siemens S7 (S7comm) -------------------------------------------------------------------


class S7Area(StrEnum):
    DB = "DB"
    MERKER = "M"
    INPUT = "I"
    OUTPUT = "Q"


# DB1.DBX6.2  DB1.DBB6  DB1.DBW6  DB1.DBD6 | M10.0 MB10 MW10 MD10 | I0.1 IW64 | Q0.0 QD4
# German mnemonics E (Eingang) = I and A (Ausgang) = Q are accepted too.
_S7_ADDRESS_RE = re.compile(
    r"DB(?P<db>\d{1,5})\.DB(?P<dsize>[XBWD])(?P<dbyte>\d{1,5})(?:\.(?P<dbit>[0-7]))?"
    r"|(?P<area>[MIEQA])(?P<size>[BWD]?)(?P<byte>\d{1,5})(?:\.(?P<bit>[0-7]))?"
)
S7_SIZE_BYTES = {"X": 1, "B": 1, "W": 2, "D": 4}


class S7Address(BaseModel):
    """A parsed S7 address. ``size`` X = bit, B = byte, W = word (2), D = double word (4)."""

    model_config = ConfigDict(frozen=True)

    area: S7Area
    db: int = 0
    byte: int
    bit: int | None = None
    size: Literal["X", "B", "W", "D"]


def parse_s7_address(text: str) -> S7Address:
    m = _S7_ADDRESS_RE.fullmatch(text.strip().upper())
    if m is None:
        raise ValueError(
            f"{text!r} is not an S7 address; examples: DB1.DBD0, DB1.DBW4, DB1.DBX6.2, M10.0, "
            "MW20, MD30, I0.1, IW64, Q0.0, QW2"
        )
    if m["db"] is not None:
        size, bit = m["dsize"], m["dbit"]
        if (size == "X") != (bit is not None):
            raise ValueError(f"{text!r}: DBX needs a bit (DB1.DBX6.2); other sizes take none")
        db = int(m["db"])
        if db < 1:
            raise ValueError(f"{text!r}: DB numbers start at 1")
        return S7Address(area=S7Area.DB, db=db, byte=int(m["dbyte"]),
                         bit=int(bit) if bit else None, size=size)
    letter = {"E": "I", "A": "Q"}.get(m["area"], m["area"])
    bit = m["bit"]
    if m["size"] and bit is not None:
        raise ValueError(f"{text!r}: a bit address has no size letter (use M10.0, not MB10.0)")
    if not m["size"] and bit is None:
        raise ValueError(f"{text!r}: give a bit (M10.0) or a size (MB10, MW10, MD10)")
    return S7Address(area=S7Area(letter), byte=int(m["byte"]),
                     bit=int(bit) if bit else None, size=m["size"] or "X")


# The S7 sizes each data type may be read with (S7 types in brackets)
_S7_TYPES: dict[str, frozenset[DataType]] = {
    "X": frozenset({DataType.BOOL}),
    "B": frozenset({DataType.UINT16, DataType.INT16,                 # BYTE/USINT, SINT
                    DataType.INT64, DataType.UINT64, DataType.FLOAT64,  # 8 bytes from here
                    DataType.STRING}),
    "W": frozenset({DataType.INT16, DataType.UINT16}),                 # INT, WORD/UINT
    "D": frozenset({DataType.INT32, DataType.UINT32, DataType.FLOAT32}),  # DINT, DWORD, REAL
}
_S7_WIDE = frozenset({DataType.INT64, DataType.UINT64, DataType.FLOAT64})


class S7Tag(TagBase):
    """A Siemens S7 variable. Values are big-endian, as in the PLC.

    * ``DBn.DBBx`` with int16/uint16 reads one byte (SINT / BYTE, USINT); with int64, uint64 or
      float64 it reads 8 bytes from that byte (LINT, ULINT, LREAL); with string an S7 STRING
      (2 header bytes + ``string_length`` characters).
    * S7-1200/1500: the data block must have "optimized block access" switched off and the
      CPU must allow PUT/GET communication.
    """

    address: str
    string_length: int | None = Field(default=None, ge=1, le=254,
                                      description="declared length of an S7 STRING")

    @property
    def parsed(self) -> S7Address:
        return parse_s7_address(self.address)

    @property
    def byte_length(self) -> int:
        a = self.parsed
        if self.data_type is DataType.STRING:
            return 2 + (self.string_length or 0)
        if a.size == "B" and self.data_type in _S7_WIDE:
            return 8
        return S7_SIZE_BYTES[a.size]

    @model_validator(mode="after")
    def _check_s7(self) -> S7Tag:
        a = parse_s7_address(self.address)
        if self.data_type not in _S7_TYPES[a.size]:
            allowed = ", ".join(sorted(str(x) for x in _S7_TYPES[a.size]))
            raise ValueError(f"{self.address} ({a.size}) cannot hold {self.data_type}; "
                             f"use one of: {allowed}")
        if (self.data_type is DataType.STRING) != (self.string_length is not None):
            raise ValueError("string tags need string_length (and only they take it)")
        if self.access is Access.READ_WRITE and a.area is S7Area.INPUT:
            raise ValueError("process inputs (I/E) are read-only; access must be read")
        return self


# --- HTTP / REST ----------------------------------------------------------------------------


class HttpTag(TagBase):
    """A value taken from a JSON response: ``path`` is appended to the device's base_url
    (requests to the same path are made once per poll), ``json_path`` selects the value."""

    path: str = Field(default="", max_length=512)
    json_path: str

    @field_validator("json_path")
    @classmethod
    def _check_json_path(cls, value: str) -> str:
        parse_json_path(value)
        return value

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if value and (not value.startswith("/") or any(c.isspace() for c in value)
                      or "://" in value):
            raise ValueError(f"{value!r}: path is relative to base_url and starts with '/'")
        return value

    @model_validator(mode="after")
    def _read_only(self) -> HttpTag:
        if self.access is Access.READ_WRITE:
            raise ValueError("HTTP devices are read-only in the hub; access must be read")
        return self


# --- SNMP -----------------------------------------------------------------------------------

_OID_RE = re.compile(r"[0-2](?:\.(?:0|[1-9]\d{0,9})){2,127}")


def validate_oid(value: str) -> str:
    value = value.strip().removeprefix(".")
    if not _OID_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not a numeric OID (e.g. 1.3.6.1.2.1.1.3.0); "
                         "MIB names are not resolved")
    return value


class SnmpTag(TagBase):
    """One SNMP object, by numeric OID (e.g. 1.3.6.1.2.1.33.1.2.4.0 = UPS battery charge).
    Counters (Counter32/64) arrive as integers; use history and reports (delta) for rates."""

    oid: Annotated[str, AfterValidator(validate_oid)]

    @model_validator(mode="after")
    def _read_only(self) -> SnmpTag:
        if self.access is Access.READ_WRITE:
            raise ValueError("SNMP devices are read-only in the hub (no SET); access must be read")
        return self


# --------------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------------

Order = Literal["big", "little"]


def _validate_host(value: str) -> str:
    if not value or any(c.isspace() for c in value) or "/" in value:
        raise ValueError(f"{value!r} is not a host name or IP address (no scheme, path or spaces)")
    return value


Host = Annotated[str, AfterValidator(_validate_host)]


class ModbusConnection(_Strict):
    """Modbus over TCP. ``framer: rtu`` = RTU frames over a TCP socket ("RTU over TCP"), used by
    serial servers / gateways in transparent mode; the default is standard Modbus TCP (MBAP)."""

    host: Host
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(ge=0, le=255)
    framer: Literal["socket", "rtu"] = "socket"
    timeout_ms: int = Field(default=1000, ge=50, le=60_000)
    retries: int = Field(default=3, ge=0, le=10)
    byte_order: Order = "big"
    word_order: Order = "big"
    max_registers_per_read: int = Field(default=125, ge=1, le=125)


_SERIAL_PORT_RE = re.compile(
    r"(?:COM[1-9][0-9]{0,2}|/dev/[A-Za-z0-9_./-]+|(?:socket|rfc2217)://[^\s/:]+:\d{1,5})"
)


def _validate_serial_port(value: str) -> str:
    if not _SERIAL_PORT_RE.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a serial port: use COM3, /dev/ttyUSB0, or rfc2217://host:port "
            "(serial device server) / socket://host:port"
        )
    return value


class ModbusRtuConnection(_Strict):
    """Modbus RTU on a serial line (RS-485/RS-232), a USB adapter or an RFC 2217 serial server.
    Devices on the same line share ``port`` and must use the same line settings."""

    port: Annotated[str, AfterValidator(_validate_serial_port)]
    unit_id: int = Field(ge=1, le=247)
    baudrate: Literal[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200] = 9600
    bytesize: Literal[7, 8] = 8
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    timeout_ms: int = Field(default=1000, ge=50, le=60_000)
    retries: int = Field(default=2, ge=0, le=10)
    byte_order: Order = "big"
    word_order: Order = "big"
    max_registers_per_read: int = Field(default=60, ge=1, le=125)

    def line_settings(self) -> tuple[int, int, str, int]:
        return (self.baudrate, self.bytesize, self.parity, self.stopbits)


class TimestampFormat(StrEnum):
    UNIX_S = "unix_s"
    UNIX_MS = "unix_ms"
    ISO8601 = "iso8601"


def parse_device_timestamp(value: object, fmt: TimestampFormat) -> datetime:
    """Turn a timestamp taken from a payload into an aware UTC datetime.

    ISO 8601 strings must carry a UTC offset (``Z`` or ``+03:30``): a naive device clock
    cannot be placed in time reliably, so it is rejected instead of guessed.
    """
    if fmt is TimestampFormat.ISO8601:
        if not isinstance(value, str):
            raise ValueError(f"expected an ISO 8601 string, got {value!r}")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"ISO 8601 timestamp {value!r} has no UTC offset")
        return parsed.astimezone(UTC)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"expected a number for {fmt}, got {value!r}")
    seconds = value / 1000 if fmt is TimestampFormat.UNIX_MS else value
    return datetime.fromtimestamp(seconds, UTC)


class MqttConnection(_Strict):
    """``timestamp_path`` points at the device's own timestamp in the payload. Without it,
    samples are stamped with the hub's receive time (wrong for buffered or late messages)."""

    broker_ref: Segment = Field(description="broker_id from config/brokers.yaml")
    telemetry_topic: Annotated[str, AfterValidator(validate_topic_filter)]
    command_topic: Annotated[str, AfterValidator(validate_topic_name)] | None = None
    status_topic: Annotated[str, AfterValidator(validate_topic_filter)] | None = None
    payload_format: Literal["json"] = "json"
    qos: Literal[0, 1, 2] = 1
    timestamp_path: str | None = None
    timestamp_format: TimestampFormat | None = None
    stale_after_s: float | None = Field(
        default=None, gt=0, le=86_400,
        description="no telemetry for this long -> device offline and its values bad",
    )

    @field_validator("timestamp_path")
    @classmethod
    def _check_timestamp_path(cls, value: str | None) -> str | None:
        if value is not None:
            parse_json_path(value)
        return value

    @model_validator(mode="after")
    def _check_timestamp(self) -> MqttConnection:
        if (self.timestamp_path is None) != (self.timestamp_format is None):
            raise ValueError(
                "timestamp_path and timestamp_format go together (unix_s, unix_ms or iso8601)"
            )
        return self


class SecurityPolicy(StrEnum):
    NONE = "None"
    BASIC256SHA256 = "Basic256Sha256"
    AES128_SHA256_RSAOAEP = "Aes128_Sha256_RsaOaep"
    AES256_SHA256_RSAPSS = "Aes256_Sha256_RsaPss"


class SecurityMode(StrEnum):
    NONE = "None"
    SIGN = "Sign"
    SIGN_AND_ENCRYPT = "SignAndEncrypt"


class AnonymousAuth(_Strict):
    type: Literal["anonymous"]


class UsernameAuth(_Strict):
    type: Literal["username"]
    username_env: EnvVarName
    password_env: EnvVarName


OpcUaAuth = Annotated[AnonymousAuth | UsernameAuth, Field(discriminator="type")]


class OpcUaConnection(_Strict):
    endpoint_url: str
    security_policy: SecurityPolicy
    security_mode: SecurityMode
    auth: OpcUaAuth = AnonymousAuth(type="anonymous")
    namespace: str | None = Field(
        default=None,
        description="Namespace URI used for node_ids written without ns=/nsu=",
    )
    client_certificate_env: EnvVarName | None = None
    client_private_key_env: EnvVarName | None = None
    timeout_ms: int = Field(default=5000, ge=100, le=60_000)

    @field_validator("endpoint_url")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        if not re.fullmatch(r"opc\.tcp://[^\s/:]+(?::\d{1,5})?(?:/\S*)?", value):
            raise ValueError(f"{value!r} is not an opc.tcp://host[:port][/path] endpoint URL")
        return value

    @model_validator(mode="after")
    def _check_security(self) -> OpcUaConnection:
        no_policy = self.security_policy is SecurityPolicy.NONE
        no_mode = self.security_mode is SecurityMode.NONE
        if no_policy != no_mode:
            raise ValueError(
                "security_policy and security_mode must both be None or both be set "
                f"(got {self.security_policy} / {self.security_mode})"
            )
        if not no_policy and not (self.client_certificate_env and self.client_private_key_env):
            raise ValueError(
                "a secure policy needs client_certificate_env and client_private_key_env "
                "(names of env vars holding the certificate and key file paths)"
            )
        return self


class S7Connection(_Strict):
    """S7 over ISO-on-TCP (port 102). Rack/slot: S7-300 0/2, S7-400 see the hardware config,
    S7-1200/1500 0/1 (or 0/0). LOGO! and S7-200 need other TSAP settings (not supported)."""

    host: Host
    port: int = Field(default=102, ge=1, le=65535)
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    timeout_ms: int = Field(default=2000, ge=100, le=60_000)
    max_bytes_per_read: int = Field(default=200, ge=16, le=940,
                                    description="largest block read at once (PDU size limit)")
    max_gap_bytes: int = Field(default=16, ge=0, le=256,
                               description="read across gaps up to this size to save requests")


class HttpNoAuth(_Strict):
    type: Literal["none"]


class HttpBasicAuth(_Strict):
    type: Literal["basic"]
    username_env: EnvVarName
    password_env: EnvVarName


class HttpBearerAuth(_Strict):
    type: Literal["bearer"]
    token_env: EnvVarName


class HttpHeaderAuth(_Strict):
    """An API key sent in a header, e.g. ``X-Api-Key``."""

    type: Literal["header"]
    header: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    value_env: EnvVarName


HttpAuth = Annotated[HttpNoAuth | HttpBasicAuth | HttpBearerAuth | HttpHeaderAuth,
                     Field(discriminator="type")]
_HTTP_BASE_RE = re.compile(r"https?://[^\s/:?#]+(?::\d{1,5})?(?:/[^\s?#]*)?")
HeaderName = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")]


class HttpConnection(_Strict):
    base_url: str
    auth: HttpAuth = HttpNoAuth(type="none")
    headers: dict[HeaderName, Annotated[str, Field(max_length=256)]] = Field(
        default_factory=dict, description="extra, non-secret headers (secrets go in auth)")
    timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    verify_tls: bool = True
    ca_file_env: EnvVarName | None = Field(default=None,
                                           description="env var with the path of a CA bundle")
    timestamp_path: str | None = None
    timestamp_format: TimestampFormat | None = None

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not _HTTP_BASE_RE.fullmatch(value):
            raise ValueError(f"{value!r} is not an http(s)://host[:port][/path] URL "
                             "(no query string; put the path of each value in the tag)")
        return value.rstrip("/")

    @field_validator("timestamp_path")
    @classmethod
    def _check_timestamp_path(cls, value: str | None) -> str | None:
        if value is not None:
            parse_json_path(value)
        return value

    @model_validator(mode="after")
    def _check(self) -> HttpConnection:
        if (self.timestamp_path is None) != (self.timestamp_format is None):
            raise ValueError("timestamp_path and timestamp_format go together")
        if {h.lower() for h in self.headers} & {"authorization", "cookie", "x-api-key"}:
            raise ValueError("credentials do not belong in headers; use auth with an env var")
        if not self.verify_tls and self.ca_file_env:
            raise ValueError("ca_file_env is only used when verify_tls is true")
        return self


class SnmpV2c(_Strict):
    version: Literal["2c"]
    community_env: EnvVarName = Field(description="env var holding the community string")


AuthProtocol = Literal["md5", "sha", "sha224", "sha256", "sha384", "sha512"]
PrivProtocol = Literal["des", "aes128", "aes192", "aes256"]


class SnmpV3(_Strict):
    version: Literal["3"]
    username: str = Field(min_length=1, max_length=32)
    auth_protocol: AuthProtocol | None = None
    auth_key_env: EnvVarName | None = None
    priv_protocol: PrivProtocol | None = None
    priv_key_env: EnvVarName | None = None

    @model_validator(mode="after")
    def _check(self) -> SnmpV3:
        if (self.auth_protocol is None) != (self.auth_key_env is None):
            raise ValueError("auth_protocol and auth_key_env go together")
        if (self.priv_protocol is None) != (self.priv_key_env is None):
            raise ValueError("priv_protocol and priv_key_env go together")
        if self.priv_protocol and not self.auth_protocol:
            raise ValueError("encryption (priv) needs authentication (auth) in SNMPv3")
        return self


class SnmpConnection(_Strict):
    host: Host
    port: int = Field(default=161, ge=1, le=65535)
    security: Annotated[SnmpV2c | SnmpV3, Field(discriminator="version")]
    timeout_ms: int = Field(default=2000, ge=100, le=60_000)
    retries: int = Field(default=1, ge=0, le=5)
    max_oids_per_request: int = Field(default=20, ge=1, le=60)


# --------------------------------------------------------------------------------------------
# Phase 7 protocols: Omron FINS, Mitsubishi MC (MELSEC), EtherNet/IP (Logix), IEC 60870-5-104.
# Tags and connections together, per protocol.
# --------------------------------------------------------------------------------------------

# Data types a group of 16-bit PLC words can hold (FINS, MC), with their width in words.
WORD_TYPES: dict[DataType, int] = {
    DataType.INT16: 1, DataType.UINT16: 1,
    DataType.INT32: 2, DataType.UINT32: 2, DataType.FLOAT32: 2,
    DataType.INT64: 4, DataType.UINT64: 4, DataType.FLOAT64: 4,
}


class WordAddress(BaseModel):
    """A parsed word-memory address: area, word number and optional bit."""

    model_config = ConfigDict(frozen=True)

    area: str
    word: int
    bit: int | None = None


def _word_tag_checks(tag: TagBase, a: WordAddress, max_bit: int = 15) -> None:
    if tag.data_type is DataType.BOOL:
        if a.bit is None:
            raise ValueError("bool tags need a bit address (e.g. D100.05 / D100.5)")
        if a.bit > max_bit:
            raise ValueError(f"bit {a.bit} is outside the word (0..{max_bit})")
    else:
        if a.bit is not None:
            raise ValueError("a bit address holds a bool; use data_type bool")
        if tag.data_type not in WORD_TYPES:
            raise ValueError(f"{tag.data_type} cannot be stored in PLC words; use "
                             + ", ".join(str(t) for t in WORD_TYPES) + " or bool")


# --- Omron FINS -----------------------------------------------------------------------------

FINS_AREAS = {"CIO": "CIO", "W": "W", "WR": "W", "H": "H", "HR": "H", "A": "A", "AR": "A",
              "D": "D", "DM": "D"}
_FINS_RE = re.compile(r"(?P<area>CIO|DM|WR|HR|AR|[DWHA])(?P<word>\d{1,5})(?:\.(?P<bit>\d{1,2}))?")


def parse_fins_address(text: str) -> WordAddress:
    m = _FINS_RE.fullmatch(text.strip().upper())
    if m is None:
        raise ValueError(f"{text!r} is not a FINS address; examples: D100, DM100, CIO10.05, "
                         "W20, H5.03, A100")
    word = int(m["word"])
    if word > 32767:
        raise ValueError(f"{text!r}: word {word} is beyond the area (0..32767)")
    return WordAddress(area=FINS_AREAS[m["area"]], word=word,
                       bit=int(m["bit"]) if m["bit"] is not None else None)


class FinsTag(TagBase):
    """An Omron CS/CJ/NJ/NX memory point: ``D100`` (DM word), ``CIO10.05`` (bit 5 of CIO 10),
    ``W20``, ``H5``, ``A100``. 32/64-bit values span 2/4 words, lowest word first by default
    (``connection.word_order``)."""

    address: str

    @property
    def parsed(self) -> WordAddress:
        return parse_fins_address(self.address)

    @property
    def word_count(self) -> int:
        return 1 if self.data_type is DataType.BOOL else WORD_TYPES[self.data_type]

    @model_validator(mode="after")
    def _check_fins(self) -> FinsTag:
        a = parse_fins_address(self.address)
        _word_tag_checks(self, a)
        if self.access is Access.READ_WRITE and a.area == "A":
            raise ValueError("auxiliary area (A) words are system words; access must be read")
        return self


class FinsConnection(_Strict):
    """FINS/TCP (port 9600, node addresses negotiated) or FINS/UDP (give the PLC's node,
    usually the last octet of its IP address, and this hub's node)."""

    host: Host
    port: int = Field(default=9600, ge=1, le=65535)
    transport: Literal["tcp", "udp"] = "tcp"
    dest_network: int = Field(default=0, ge=0, le=127)
    dest_node: int = Field(default=0, ge=0, le=254, description="0 = from the TCP handshake")
    dest_unit: int = Field(default=0, ge=0, le=255, description="0 = the CPU unit")
    src_node: int = Field(default=0, ge=0, le=254, description="UDP: this hub's node")
    timeout_ms: int = Field(default=2000, ge=100, le=60_000)
    word_order: Order = "little"
    max_words_per_read: int = Field(default=500, ge=1, le=990)
    max_gap_words: int = Field(default=16, ge=0, le=500)

    @model_validator(mode="after")
    def _check_udp(self) -> FinsConnection:
        if self.transport == "udp" and (self.dest_node == 0 or self.src_node == 0):
            raise ValueError("FINS/UDP needs dest_node (the PLC) and src_node (this hub)")
        return self


# --- Mitsubishi MC protocol (MELSEC, 3E binary frame) ---------------------------------------

# device -> (binary device code, word device?, hexadecimal numbering?)
MC_DEVICES: dict[str, tuple[int, bool, bool]] = {
    "D": (0xA8, True, False), "W": (0xB4, True, True), "R": (0xAF, True, False),
    "ZR": (0xB0, True, False), "SD": (0xA9, True, False), "SW": (0xB5, True, True),
    "TN": (0xC2, True, False), "CN": (0xC5, True, False),
    "X": (0x9C, False, True), "Y": (0x9D, False, True), "M": (0x90, False, False),
    "L": (0x92, False, False), "B": (0xA0, False, True), "SM": (0x91, False, False),
    "F": (0x93, False, False), "SB": (0xA1, False, True),
}
_MC_RE = re.compile(r"(?P<dev>ZR|SD|SW|TN|CN|SM|SB|[DWRXYMLBF])(?P<num>[0-9A-F]{1,7})"
                    r"(?:\.(?P<bit>[0-9A-F]))?")


def parse_mc_address(text: str) -> WordAddress:
    m = _MC_RE.fullmatch(text.strip().upper())
    if m is None:
        raise ValueError(f"{text!r} is not a MELSEC address; examples: D100, D100.5, W1A, "
                         "R2000, M10, X1F, Y20, B3")
    dev = m["dev"]
    _, is_word, is_hex = MC_DEVICES[dev]
    try:
        number = int(m["num"], 16 if is_hex else 10)
    except ValueError:
        raise ValueError(f"{text!r}: {dev} numbers are decimal") from None
    if number > 0xFFFFFF:
        raise ValueError(f"{text!r}: device number too large")
    bit = m["bit"]
    if bit is not None and not is_word:
        raise ValueError(f"{text!r}: {dev} is a bit device; a bit suffix is for word devices")
    return WordAddress(area=dev, word=number, bit=int(bit, 16) if bit is not None else None)


class McTag(TagBase):
    """A MELSEC device: word devices ``D100``, ``W1A`` (hex), ``R2000``, ``ZR10000``, a bit of a
    word ``D100.5``; bit devices ``M10``, ``X1F`` (hex), ``Y20`` (hex), ``B3`` (hex), ``L5``.
    32/64-bit values span 2/4 words, lowest word first (``connection.word_order``)."""

    address: str

    @property
    def parsed(self) -> WordAddress:
        return parse_mc_address(self.address)

    @property
    def is_bit_device(self) -> bool:
        return not MC_DEVICES[self.parsed.area][1]

    @property
    def word_count(self) -> int:
        return 1 if self.data_type is DataType.BOOL else WORD_TYPES[self.data_type]

    @model_validator(mode="after")
    def _check_mc(self) -> McTag:
        a = parse_mc_address(self.address)
        if not MC_DEVICES[a.area][1]:                      # bit device: one point = one bool
            if self.data_type is not DataType.BOOL:
                raise ValueError(f"{a.area} is a bit device; data_type must be bool")
        else:
            _word_tag_checks(self, a)
            if a.bit is not None and self.access is Access.READ_WRITE:
                raise ValueError("a bit of a word register (D100.5) is read-only: writing it "
                                 "would need a read-modify-write of the whole word")
        if self.access is Access.READ_WRITE and a.area in ("X", "SM", "SD"):
            raise ValueError(f"{a.area} (inputs / special relays and registers) is read-only")
        return self


class McConnection(_Strict):
    """MC protocol, QnA-compatible 3E frame, binary code (Q/L/iQ-R/iQ-F with an Ethernet port
    set to "MC protocol", binary, the port number below)."""

    host: Host
    port: int = Field(ge=1, le=65535, description="the MC port opened in GX Works (e.g. 5007)")
    transport: Literal["tcp", "udp"] = "tcp"
    network: int = Field(default=0, ge=0, le=239)
    pc: int = Field(default=0xFF, ge=0, le=255)
    module_io: int = Field(default=0x03FF, ge=0, le=0xFFFF)
    station: int = Field(default=0, ge=0, le=255)
    timeout_ms: int = Field(default=2000, ge=250, le=60_000)
    word_order: Order = "little"
    max_points_per_read: int = Field(default=480, ge=1, le=960)
    max_gap_points: int = Field(default=16, ge=0, le=480)


# --- EtherNet/IP (CIP, Allen-Bradley Logix tags) --------------------------------------------

_LOGIX_SEGMENT = r"[A-Za-z_][A-Za-z0-9_]{0,39}(?:\[\d{1,6}(?:,\d{1,6}){0,2}\])?"
_LOGIX_TAG_RE = re.compile(rf"(?:Program:[A-Za-z_][A-Za-z0-9_]{{0,39}}\.)?{_LOGIX_SEGMENT}"
                           rf"(?:\.{_LOGIX_SEGMENT})*")


class EnipTag(TagBase):
    """A Logix (ControlLogix / CompactLogix / Micro800) tag by name: ``Speed``,
    ``Program:Main.Level``, ``Motors[3].Current``. ``bit`` reads one bit of an integer tag
    (read-only). Types: BOOL bool, SINT/INT int16, UINT uint16, DINT int32, UDINT uint32,
    LINT int64, ULINT uint64, REAL float32, LREAL float64, STRING string (read-only)."""

    tag: str = Field(max_length=200)
    bit: int | None = Field(default=None, ge=0, le=63)

    @model_validator(mode="after")
    def _check_enip(self) -> EnipTag:
        if not _LOGIX_TAG_RE.fullmatch(self.tag):
            raise ValueError(f"{self.tag!r} is not a Logix tag name (e.g. Speed, "
                             "Program:Main.Level, Motors[3].Current)")
        if self.bit is not None:
            if self.data_type is not DataType.BOOL:
                raise ValueError("bit selects a bool out of an integer tag; data_type bool")
            if self.access is Access.READ_WRITE:
                raise ValueError("bits of an integer are read-only; write a BOOL tag instead")
        if self.data_type is DataType.STRING and self.access is Access.READ_WRITE:
            raise ValueError("STRING tags are read-only")
        return self


class EnipConnection(_Strict):
    """EtherNet/IP explicit messaging (TCP 44818). ``slot`` routes through the backplane to
    the CPU (ControlLogix: the CPU slot; CompactLogix: 0); ``null`` sends straight to the
    adapter (Micro800, embedded ports)."""

    host: Host
    port: int = Field(default=44818, ge=1, le=65535)
    slot: int | None = Field(default=0, ge=0, le=31)
    timeout_ms: int = Field(default=3000, ge=100, le=60_000)
    max_tags_per_request: int = Field(default=20, ge=1, le=100)


# --- IEC 60870-5-104 ------------------------------------------------------------------------

IEC104_TYPES: dict[str, frozenset[DataType]] = {
    "single": frozenset({DataType.BOOL}),
    "double": frozenset({DataType.BOOL, DataType.UINT16}),
    "step": frozenset({DataType.INT16}),
    "bitstring": frozenset({DataType.UINT32}),
    "normalized": frozenset({DataType.FLOAT32, DataType.FLOAT64}),
    "scaled": frozenset({DataType.INT16, DataType.INT32}),
    "float": frozenset({DataType.FLOAT32, DataType.FLOAT64}),
    "integrated": frozenset({DataType.INT32, DataType.INT64}),
}
IEC104_COMMANDS = frozenset({"single", "double", "normalized", "scaled", "float"})


class Iec104Tag(TagBase):
    """An information object of an IEC 60870-5-104 outstation (RTU, bay controller, gateway).

    ``type`` is the monitoring type the outstation sends (with or without time tag):
    single (M_SP), double (M_DP; bool = ON, or uint16 = the raw DPI 0..3), step (M_ST),
    bitstring (M_BO), normalized (M_ME_NA, -1..1), scaled (M_ME_NB), float (M_ME_NC),
    integrated (M_IT, counters). Writable: single, double (C_SC / C_DC) and the three setpoint
    types (C_SE_NA/NB/NC), sent to ``command_ioa`` (default: the same IOA)."""

    ioa: int = Field(ge=1, le=16_777_215, description="information object address")
    type: Literal["single", "double", "step", "bitstring", "normalized", "scaled", "float",
                  "integrated"]
    command_ioa: int | None = Field(default=None, ge=1, le=16_777_215)
    select_before_operate: bool = False

    @model_validator(mode="after")
    def _check_iec104(self) -> Iec104Tag:
        if self.data_type not in IEC104_TYPES[self.type]:
            allowed = ", ".join(sorted(str(t) for t in IEC104_TYPES[self.type]))
            raise ValueError(f"{self.type} points hold {allowed}")
        if self.access is Access.READ_WRITE and self.type not in IEC104_COMMANDS:
            raise ValueError(f"{self.type} points cannot be commanded; access must be read")
        if self.command_ioa is not None and self.access is not Access.READ_WRITE:
            raise ValueError("command_ioa is for writable tags")
        return self


class Iec104Connection(_Strict):
    """IEC 60870-5-104 controlling station (client): TCP 2404, 2-byte COT, 3-byte IOA."""

    host: Host
    port: int = Field(default=2404, ge=1, le=65535)
    common_address: int = Field(ge=1, le=65534, description="ASDU address of the station")
    originator_address: int = Field(default=0, ge=0, le=255)
    t0_s: int = Field(default=30, ge=1, le=255, description="connection establishment")
    t1_s: int = Field(default=15, ge=1, le=255, description="wait for an acknowledgement")
    t2_s: int = Field(default=10, ge=1, le=255, description="acknowledge received frames")
    t3_s: int = Field(default=20, ge=1, le=172_800, description="test frame when idle")
    k: int = Field(default=12, ge=1, le=32767)
    w: int = Field(default=8, ge=1, le=32767)
    interrogation_interval_s: int = Field(default=900, ge=0, le=86_400,
                                          description="general interrogation (0 = at start only)")
    counter_interrogation_interval_s: int = Field(default=900, ge=0, le=86_400)
    stale_after_s: float | None = Field(default=None, ge=1, le=86_400,
                                        description="no frame for this long = offline")

    @model_validator(mode="after")
    def _check_timers(self) -> Iec104Connection:
        if not self.t2_s < self.t1_s:
            raise ValueError("t2 must be shorter than t1 (IEC 60870-5-104)")
        if self.w > self.k * 2 // 3 + 1:
            raise ValueError("w should not exceed two thirds of k")
        return self


# --------------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------------

PollInterval = Annotated[int, Field(ge=100, le=86_400_000)]


class DeviceBase(_Strict):
    device_id: Segment
    name: str = Field(min_length=1, max_length=128, description="Human-readable display name")
    site: Segment
    area: Segment
    enabled: bool = True
    description: str | None = None
    history_heartbeat_s: int = Field(
        default=300, ge=1, le=86_400,
        description="store a sample at least this often even if the value did not change",
    )

    def tag_path(self, tag_name: str) -> str:
        return make_tag_path(self.site, self.area, self.device_id, tag_name)


class ModbusDevice(DeviceBase):
    protocol: Literal["modbus_tcp"]
    connection: ModbusConnection
    poll_interval_ms: PollInterval = 1000
    tags: list[ModbusTag] = Field(default_factory=list)


class ModbusRtuDevice(DeviceBase):
    protocol: Literal["modbus_rtu"]
    connection: ModbusRtuConnection
    poll_interval_ms: PollInterval = 1000
    tags: list[ModbusTag] = Field(default_factory=list)


class MqttDevice(DeviceBase):
    """MQTT devices push data, so there is no poll_interval_ms."""

    protocol: Literal["mqtt"]
    connection: MqttConnection
    tags: list[MqttTag] = Field(default_factory=list)


class OpcUaDevice(DeviceBase):
    protocol: Literal["opcua"]
    connection: OpcUaConnection
    poll_interval_ms: PollInterval = 1000
    tags: list[OpcUaTag] = Field(default_factory=list)


class S7Device(DeviceBase):
    protocol: Literal["s7"]
    connection: S7Connection
    poll_interval_ms: PollInterval = 1000
    tags: list[S7Tag] = Field(default_factory=list)


class HttpDevice(DeviceBase):
    protocol: Literal["http"]
    connection: HttpConnection
    poll_interval_ms: PollInterval = 5000
    tags: list[HttpTag] = Field(default_factory=list)


class SnmpDevice(DeviceBase):
    protocol: Literal["snmp"]
    connection: SnmpConnection
    poll_interval_ms: PollInterval = 10_000
    tags: list[SnmpTag] = Field(default_factory=list)


class FinsDevice(DeviceBase):
    protocol: Literal["fins"]
    connection: FinsConnection
    poll_interval_ms: PollInterval = 1000
    tags: list[FinsTag] = Field(default_factory=list)


class McDevice(DeviceBase):
    protocol: Literal["mc"]
    connection: McConnection
    poll_interval_ms: PollInterval = 1000
    tags: list[McTag] = Field(default_factory=list)


class EnipDevice(DeviceBase):
    protocol: Literal["enip"]
    connection: EnipConnection
    poll_interval_ms: PollInterval = 1000
    tags: list[EnipTag] = Field(default_factory=list)


class Iec104Device(DeviceBase):
    """IEC 104 outstations push data (spontaneous, periodic, interrogation): no poll interval."""

    protocol: Literal["iec104"]
    connection: Iec104Connection
    tags: list[Iec104Tag] = Field(default_factory=list)


AnyModbusDevice = ModbusDevice | ModbusRtuDevice
AnyDeviceModel = (ModbusDevice | ModbusRtuDevice | MqttDevice | OpcUaDevice | S7Device
                  | HttpDevice | SnmpDevice | FinsDevice | McDevice | EnipDevice | Iec104Device)
Device = Annotated[AnyDeviceModel, Field(discriminator="protocol")]
DEVICE_ADAPTER: TypeAdapter[AnyDeviceModel] = TypeAdapter(Device)
PROTOCOLS = ("modbus_tcp", "modbus_rtu", "mqtt", "opcua", "s7", "http", "snmp", "fins", "mc",
             "enip", "iec104")


# --------------------------------------------------------------------------------------------
# Brokers (config/brokers.yaml)
# --------------------------------------------------------------------------------------------


class Broker(_Strict):
    """An MQTT broker. With ``tls`` the broker certificate is checked against the system CAs, or
    against ``ca_file_env`` (your own CA); ``client_certificate_env`` + ``client_private_key_env``
    add a client certificate (mutual TLS). The ``*_env`` fields name environment variables that
    hold file paths, so certificates and keys stay out of config."""

    broker_id: Segment
    host: Host
    port: int = Field(default=1883, ge=1, le=65535)
    tls: bool = False
    keepalive_s: int = Field(default=60, ge=5, le=3600)
    username_env: EnvVarName | None = None
    password_env: EnvVarName | None = None
    ca_file_env: EnvVarName | None = None
    client_certificate_env: EnvVarName | None = None
    client_private_key_env: EnvVarName | None = None
    tls_server_name: Host | None = Field(
        default=None, description="name in the broker certificate when it differs from host")

    @model_validator(mode="after")
    def _check_credentials(self) -> Broker:
        if self.password_env and not self.username_env:
            raise ValueError("password_env requires username_env")
        if (self.client_certificate_env is None) != (self.client_private_key_env is None):
            raise ValueError("client_certificate_env and client_private_key_env go together")
        uses_tls = (self.ca_file_env, self.client_certificate_env, self.tls_server_name)
        if not self.tls and any(uses_tls):
            raise ValueError("ca_file_env, client certificates and tls_server_name need tls: true")
        return self


class BrokersFile(_Strict):
    brokers: list[Broker] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Alarms (config/alarms.yaml)
# --------------------------------------------------------------------------------------------


class AlarmSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlarmRule(_Strict):
    """Limit alarm on one tag. Numeric tags use hi/hihi/lo/lolo; bool tags use ``equals``.

    An alarm becomes active when the condition holds for ``delay_s`` and clears when the value
    is back inside the limit by more than ``deadband`` (hysteresis).
    """

    tag: TagPath
    hihi: float | None = None
    hi: float | None = None
    lo: float | None = None
    lolo: float | None = None
    equals: bool | None = None
    deadband: float = Field(default=0.0, ge=0)
    delay_s: float = Field(default=0.0, ge=0, le=3600)
    severity: AlarmSeverity = AlarmSeverity.MEDIUM
    message: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_limits(self) -> AlarmRule:
        limits = [self.hihi, self.hi, self.lo, self.lolo]
        if self.equals is None and all(v is None for v in limits):
            raise ValueError("an alarm rule needs at least one of hihi, hi, lo, lolo or equals")
        if self.equals is not None and any(v is not None for v in limits):
            raise ValueError("equals (bool tags) cannot be combined with hi/lo limits")
        ordered = [v for v in (self.lolo, self.lo, self.hi, self.hihi) if v is not None]
        if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
            raise ValueError("limits must be strictly increasing: lolo < lo < hi < hihi")
        return self


class AlarmsFile(_Strict):
    offline_alarm: bool = True
    offline_severity: AlarmSeverity = AlarmSeverity.HIGH
    offline_delay_s: float = Field(default=30.0, ge=0, le=3600)
    rules: list[AlarmRule] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Notifications (config/notifications.yaml)
# --------------------------------------------------------------------------------------------

_HTTP_URL_RE = re.compile(r"https?://[^\s]+")


def _validate_http_url(value: str) -> str:
    if not _HTTP_URL_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not an http(s) URL")
    return value


HttpUrl = Annotated[str, AfterValidator(_validate_http_url)]


class EmailChannel(_Strict):
    type: Literal["email"]
    host: Host
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username_env: EnvVarName | None = None
    password_env: EnvVarName | None = None
    sender: str = Field(min_length=3, max_length=200)
    recipients: list[str] = Field(min_length=1, max_length=50)


class TelegramChannel(_Strict):
    type: Literal["telegram"]
    bot_token_env: EnvVarName
    chat_ids: list[str] = Field(min_length=1, max_length=50)
    api_base: HttpUrl = "https://api.telegram.org"


class SmsChannel(_Strict):
    """Generic HTTP SMS gateway. ``{message}``, ``{recipient}`` and ``{api_key}`` are
    substituted in ``url``, ``params`` and ``headers`` (one request per recipient)."""

    type: Literal["sms"]
    url: HttpUrl
    method: Literal["GET", "POST"] = "POST"
    params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    api_key_env: EnvVarName | None = None
    recipients: list[str] = Field(min_length=1, max_length=50)


class WebhookChannel(_Strict):
    """POSTs the alarm event as JSON; with ``secret_env`` an ``X-Hub-Signature-256:
    sha256=<hex>`` HMAC of the body is added."""

    type: Literal["webhook"]
    url: HttpUrl
    headers: dict[str, str] = Field(default_factory=dict)
    secret_env: EnvVarName | None = None


Channel = Annotated[EmailChannel | TelegramChannel | SmsChannel | WebhookChannel,
                    Field(discriminator="type")]


class NotificationRule(_Strict):
    name: Segment
    channels: list[Segment] = Field(min_length=1)
    min_severity: AlarmSeverity = AlarmSeverity.HIGH
    events: list[Literal["raised", "cleared"]] = Field(default_factory=lambda: ["raised"],
                                                       min_length=1)
    sites: list[Segment] | None = None
    areas: list[Segment] | None = None
    include_offline: bool = True


class NotificationsFile(_Strict):
    language: Literal["fa", "en"] = "fa"
    max_per_minute: int = Field(default=20, ge=1, le=1000, description="per channel")
    channels: dict[Segment, Channel] = Field(default_factory=dict)
    rules: list[NotificationRule] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Directory sign-in (config/ldap.yaml)
# --------------------------------------------------------------------------------------------

HubRole = Literal["admin", "engineer", "operator", "viewer"]


class LdapRoleMapping(_Strict):
    """Members of ``group`` (its distinguished name) get ``role``, everywhere or limited to a
    site / a site + area, like a role granted on the Users page."""

    group: str = Field(min_length=3, max_length=500)
    role: HubRole
    site: Segment | None = None
    area: Segment | None = None

    @model_validator(mode="after")
    def _check_scope(self) -> LdapRoleMapping:
        if self.area is not None and self.site is None:
            raise ValueError("area needs a site")
        if "=" not in self.group:
            raise ValueError(f"{self.group!r} is not a distinguished name (CN=...,OU=...,DC=...)")
        return self


class LdapFile(_Strict):
    """Sign-in with Active Directory / LDAP. The hub looks the user up with a service account,
    checks the password with a bind as that user, and takes the roles from the user's groups
    (``mappings``; no mapped group = no access). Only encrypted connections: ``ldaps://`` or
    ``ldap://`` with ``start_tls``. Local accounts keep working (e.g. when the directory is
    down)."""

    enabled: bool = True
    url: str = Field(description="ldaps://dc1.kish.local:636 or ldap://dc1.kish.local:389")
    start_tls: bool = False
    ca_file_env: EnvVarName | None = Field(
        default=None, description="variable holding the path of the company CA (PEM)")
    bind_dn: str = Field(min_length=3, max_length=500, description="service account DN")
    bind_password_env: EnvVarName
    user_base: str = Field(min_length=3, max_length=500)
    user_filter: str = Field(
        default="(&(objectClass=user)(sAMAccountName={username}))", max_length=500)
    group_base: str | None = Field(default=None, max_length=500)
    nested_groups: bool = Field(default=True,
                                description="Active Directory: count groups inside groups")
    display_name_attribute: str = Field(default="displayName", max_length=64)
    timeout_s: float = Field(default=5, ge=1, le=60)
    mappings: list[LdapRoleMapping] = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not re.fullmatch(r"ldaps?://[A-Za-z0-9.\-]+(:\d{1,5})?/?", value):
            raise ValueError(f"{value!r}: use ldaps://host[:port] or ldap://host[:port]")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_security(self) -> LdapFile:
        if self.url.startswith("ldap://") and not self.start_tls:
            raise ValueError("ldap:// without start_tls would send passwords in clear text; "
                             "use ldaps:// or start_tls: true")
        if self.url.startswith("ldaps://") and self.start_tls:
            raise ValueError("start_tls is for ldap:// URLs; ldaps:// is already encrypted")
        if "{username}" not in self.user_filter:
            raise ValueError("user_filter must contain {username}")
        return self


# --------------------------------------------------------------------------------------------
# Control policy (config/control.yaml)
# --------------------------------------------------------------------------------------------

CompareOp = Literal["eq", "ne", "lt", "le", "gt", "ge"]


class Interlock(_Strict):
    """A write is refused unless ``<tag> <op> <value>`` holds on a fresh, good value."""

    tag: TagPath
    op: CompareOp
    value: bool | float | str
    max_age_s: float = Field(default=10.0, gt=0, le=3600)
    message: str = Field(min_length=1, max_length=200)


class ControlPolicy(_Strict):
    confirm: bool = Field(default=False, description="a second user must confirm the write")
    min_interval_s: float = Field(default=2.0, ge=0, le=3600)
    max_step: float | None = Field(default=None, gt=0,
                                   description="largest change per write, engineering units")
    readback_timeout_s: float = Field(default=5.0, gt=0, le=120)
    interlocks: list[Interlock] = Field(default_factory=list)


class ControlFile(_Strict):
    defaults: ControlPolicy = ControlPolicy()
    tags: dict[TagPath, ControlPolicy] = Field(default_factory=dict)

    def policy_for(self, tag_path: str) -> ControlPolicy:
        """Per-tag entries override only the fields they set; the rest come from defaults."""
        override = self.tags.get(tag_path)
        if override is None:
            return self.defaults
        merged = self.defaults.model_dump()
        merged.update(override.model_dump(include=override.model_fields_set))
        return ControlPolicy.model_validate(merged)


# --------------------------------------------------------------------------------------------
# Northbound MQTT (config/northbound.yaml): publish tag values to another broker
# --------------------------------------------------------------------------------------------


class _PublishTarget(_Strict):
    """What every output target shares: which tags, how often, and store & forward."""

    name: Segment
    tags: list[str] = Field(default_factory=lambda: ["*"], min_length=1,
                            description="tag path patterns (* and ? wildcards)")
    heartbeat_s: int = Field(default=300, ge=5, le=86_400)
    min_interval_s: float = Field(default=0.0, ge=0, le=3600,
                                  description="at most one message per tag in this interval")
    queue_max: int = Field(default=10_000, ge=10, le=1_000_000,
                           description="messages kept in memory while the target is unreachable")
    store_and_forward: bool = Field(
        default=True, description="beyond the memory queue, keep messages on disk (survives "
        "restarts) and send them in order after the reconnect (needs HUB_SPOOL_DIR)")
    spool_max_mb: int = Field(default=512, ge=1, le=1_000_000)

    @field_validator("tags")
    @classmethod
    def _check_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not re.fullmatch(r"[a-z0-9_.*?]+", pattern):
                raise ValueError(f"{pattern!r}: use tag path characters and * ? wildcards")
        return value


class NorthboundTarget(_PublishTarget):
    """Publishes ``<topic_prefix>/<site>/<area>/<device_id>/<tag>`` with
    ``{"v": value, "ts": "...Z", "q": "good", "u": unit}`` when a value or its quality changes,
    and at least every ``heartbeat_s``. ``<topic_prefix>/status`` (retained) says ``online`` /
    ``offline`` (the broker sends ``offline`` as last will if the hub disappears).
    Output only: nothing received on the broker is written to a device."""

    broker_ref: Segment
    topic_prefix: Annotated[str, AfterValidator(validate_topic_name)] = "hub"
    qos: Literal[0, 1] = 1
    retain: bool = False


_KAFKA_SERVER_RE = re.compile(r"[A-Za-z0-9.\-]+:\d{1,5}")


class KafkaTarget(_PublishTarget):
    """Publishes tag values to an Apache Kafka topic: key = tag path (one tag stays in one
    partition, in order), value = ``{"tag", "v", "ts", "q", "u", "node"}`` as JSON.
    ``acks: all`` uses an idempotent producer (no duplicates on retries). Encrypted by
    default (SASL_SSL or SSL); PLAINTEXT only with ``allow_plaintext``.
    Output only: the hub never reads from Kafka."""

    bootstrap_servers: list[str] = Field(min_length=1, max_length=20)
    topic: str = Field(default="hub.samples", pattern=r"^[A-Za-z0-9._\-]{1,249}$")
    security_protocol: Literal["SASL_SSL", "SSL", "PLAINTEXT"] = "SASL_SSL"
    sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = "SCRAM-SHA-512"
    username_env: EnvVarName | None = None
    password_env: EnvVarName | None = None
    ca_file_env: EnvVarName | None = None
    client_certificate_env: EnvVarName | None = None
    client_private_key_env: EnvVarName | None = None
    allow_plaintext: bool = False
    acks: Literal["all", 1] = "all"
    compression: Literal["gzip", "none"] = "gzip"
    linger_ms: int = Field(default=50, ge=0, le=10_000)

    @field_validator("bootstrap_servers")
    @classmethod
    def _check_servers(cls, value: list[str]) -> list[str]:
        for server in value:
            if not _KAFKA_SERVER_RE.fullmatch(server):
                raise ValueError(f"{server!r}: use host:port (e.g. kafka1.kish.local:9093)")
        return value

    @model_validator(mode="after")
    def _check_security(self) -> KafkaTarget:
        if self.security_protocol == "PLAINTEXT" and not self.allow_plaintext:
            raise ValueError("PLAINTEXT sends values and credentials unencrypted; use SSL / "
                             "SASL_SSL, or set allow_plaintext: true for a closed lab network")
        if self.security_protocol == "SASL_SSL" and not (self.username_env
                                                         and self.password_env):
            raise ValueError("SASL_SSL needs username_env and password_env")
        if (self.client_certificate_env is None) != (self.client_private_key_env is None):
            raise ValueError("client_certificate_env and client_private_key_env go together")
        return self


class NorthboundFile(_Strict):
    targets: list[NorthboundTarget] = Field(default_factory=list)
    kafka: list[KafkaTarget] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Modbus TCP server (config/modbus_server.yaml): tag values for SCADA / HMI without OPC UA
# --------------------------------------------------------------------------------------------


class ModbusServerPoint(_Strict):
    """One tag (or its quality) at a Modbus address of the hub's server.

    ``source: quality`` publishes the quality code (0 good, 1 uncertain, 2 bad) in a register.
    ``writable`` lets clients write the tag (holding registers and coils only); every write
    goes through the control-safety layer as ``writes.as_user``."""

    tag: TagPath
    register_type: RegisterType
    address: int = Field(ge=0, le=65535)
    data_type: DataType | None = Field(default=None,
                                       description="register format; default: the tag's type")
    scale: float = Field(default=1.0, description="register = value / scale")
    source: Literal["value", "quality"] = "value"
    writable: bool = False

    @property
    def register_data_type(self) -> DataType:
        if self.source == "quality":
            return DataType.UINT16
        return self.data_type or DataType.FLOAT32

    @model_validator(mode="after")
    def _check(self) -> ModbusServerPoint:
        bit_area = self.register_type in BIT_REGISTER_TYPES
        if self.source == "quality":
            if bit_area or self.writable or self.data_type not in (None, DataType.UINT16):
                raise ValueError("a quality point is a read-only uint16 holding/input register")
        if self.data_type is DataType.STRING:
            raise ValueError("string points are not supported")
        if bit_area and self.data_type not in (None, DataType.BOOL):
            raise ValueError(f"{self.register_type} points carry bool values")
        if not bit_area and self.data_type is DataType.BOOL:
            raise ValueError("bool points use register_type coil or discrete_input")
        if self.writable and self.register_type in READ_ONLY_REGISTER_TYPES:
            raise ValueError(f"{self.register_type} is read-only in Modbus; writable needs a "
                             "holding register or a coil")
        if self.scale == 0 or not math.isfinite(self.scale):
            raise ValueError("scale must be finite and non-zero")
        return self


class ModbusServerWrites(_Strict):
    """Modbus has no authentication: writes are accepted only from ``allowed_clients`` and are
    made as the hub user ``as_user`` (whose roles and the control policy decide)."""

    as_user: str = Field(min_length=3, max_length=64)
    allowed_clients: list[str] = Field(min_length=1, description="IP addresses or networks")

    @field_validator("allowed_clients")
    @classmethod
    def _check_networks(cls, value: list[str]) -> list[str]:
        import ipaddress

        for net in value:
            ipaddress.ip_network(net, strict=False)
        return value


class ModbusServerFile(_Strict):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247, description="clients may also use 0 or 255")
    byte_order: Order = "big"
    word_order: Order = "big"
    allowed_clients: list[str] = Field(default_factory=list,
                                       description="empty = any address may read")
    max_clients: int = Field(default=16, ge=1, le=256)
    points: list[ModbusServerPoint] = Field(default_factory=list)
    writes: ModbusServerWrites | None = None

    @field_validator("allowed_clients")
    @classmethod
    def _check_networks(cls, value: list[str]) -> list[str]:
        import ipaddress

        for net in value:
            ipaddress.ip_network(net, strict=False)
        return value

    @model_validator(mode="after")
    def _check_writes(self) -> ModbusServerFile:
        if any(p.writable for p in self.points) and self.writes is None:
            raise ValueError("writable points need a writes section (as_user, allowed_clients)")
        return self


# --------------------------------------------------------------------------------------------
# Runtime samples (driver output)
# --------------------------------------------------------------------------------------------


class Quality(StrEnum):
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


# Storage codes for Quality (db/schema.sql stores a smallint). The only place this mapping lives.
QUALITY_CODES: dict[Quality, int] = {Quality.GOOD: 0, Quality.UNCERTAIN: 1, Quality.BAD: 2}


class Sample(_Strict):
    """One normalized reading. Drivers emit these, never raw protocol objects."""

    tag: TagPath
    value: bool | int | float | str | None
    timestamp: AwareDatetime
    quality: Quality = Quality.GOOD

    @field_validator("timestamp")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)
