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

    @model_validator(mode="after")
    def _check_value_semantics(self) -> TagBase:
        numeric = self.data_type in NUMERIC_TYPES
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


class ModbusTag(TagBase):
    """A Modbus point.

    ``bit`` selects one bit of a 16-bit holding/input register (0 = least significant bit),
    for status words such as "bit 3 = fault". Such tags are bool and read-only: writing one
    bit needs a read-modify-write of the whole register, which the safety layer does not
    support yet.
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
        if self.bit is not None:
            if bit_area:
                raise ValueError(
                    f"bit only applies to holding/input registers; a {self.register_type} "
                    "is already a single bit"
                )
            if self.data_type is not DataType.BOOL:
                raise ValueError("a tag with bit must have data_type bool")
            if self.access is Access.READ_WRITE:
                raise ValueError(
                    "writing a single register bit is not supported (needs read-modify-write); "
                    "use access: read, or a coil / whole holding register for control"
                )
        elif not bit_area and self.data_type is DataType.BOOL:
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


AnyModbusDevice = ModbusDevice | ModbusRtuDevice
Device = Annotated[ModbusDevice | ModbusRtuDevice | MqttDevice | OpcUaDevice,
                   Field(discriminator="protocol")]
DEVICE_ADAPTER: TypeAdapter[ModbusDevice | ModbusRtuDevice | MqttDevice | OpcUaDevice] = (
    TypeAdapter(Device))
PROTOCOLS = ("modbus_tcp", "modbus_rtu", "mqtt", "opcua")


# --------------------------------------------------------------------------------------------
# Brokers (config/brokers.yaml)
# --------------------------------------------------------------------------------------------


class Broker(_Strict):
    broker_id: Segment
    host: Host
    port: int = Field(default=1883, ge=1, le=65535)
    tls: bool = False
    keepalive_s: int = Field(default=60, ge=5, le=3600)
    username_env: EnvVarName | None = None
    password_env: EnvVarName | None = None

    @model_validator(mode="after")
    def _check_credentials(self) -> Broker:
        if self.password_env and not self.username_env:
            raise ValueError("password_env requires username_env")
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
