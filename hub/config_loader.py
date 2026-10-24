"""Load the configuration directory and validate it as a whole.

Layout::

    config/
      brokers.yaml          MQTT brokers (list under ``brokers:``)
      devices/<device_id>.yaml   one device per file
      alarms.yaml, control.yaml, notifications.yaml     (optional)
      northbound.yaml       publish values to another MQTT broker (optional)
      modbus_server.yaml    Modbus TCP server output for SCADA / HMI (optional)

Each file is validated with the models in ``hub.models``; then cross-object checks run
(duplicate ids, overlapping Modbus ranges, MQTT topic collisions, broker references, ...).
Problems are collected as :class:`Issue` objects instead of raising, so a single run reports
everything. The hub must refuse to start when :attr:`ConfigReport.has_errors` is true.
"""

from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from hub.models import (
    BIT_REGISTER_TYPES,
    DEVICE_ADAPTER,
    MODBUS_WIDTH,
    NUMERIC_TYPES,
    PROTOCOLS,
    Access,
    AlarmsFile,
    Broker,
    BrokersFile,
    ControlFile,
    DataType,
    EnipDevice,
    FinsDevice,
    HttpBasicAuth,
    HttpBearerAuth,
    HttpDevice,
    HttpHeaderAuth,
    Iec104Device,
    McDevice,
    ModbusDevice,
    ModbusRtuDevice,
    ModbusServerFile,
    ModbusTag,
    MqttDevice,
    NorthboundFile,
    NotificationsFile,
    OpcUaDevice,
    RegisterType,
    S7Device,
    SecurityPolicy,
    SnmpDevice,
    SnmpV2c,
    TagBase,
    UsernameAuth,
    topics_overlap,
)

log = logging.getLogger(__name__)

BROKERS_FILE = "brokers.yaml"
DEVICES_DIR = "devices"
ALARMS_FILE = "alarms.yaml"
CONTROL_FILE = "control.yaml"
NOTIFICATIONS_FILE = "notifications.yaml"
NORTHBOUND_FILE = "northbound.yaml"
MODBUS_SERVER_FILE = "modbus_server.yaml"

AnyDevice = (ModbusDevice | ModbusRtuDevice | MqttDevice | OpcUaDevice | S7Device | HttpDevice
             | SnmpDevice | FinsDevice | McDevice | EnipDevice | Iec104Device)


class Severity(StrEnum):
    WARNING = "WARNING"
    ERROR = "ERROR"


class Issue(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: Severity
    source: str  # config file, relative to the config directory, using '/'
    message: str
    location: str | None = None  # e.g. "tags[2] (motor_speed).address"


@dataclass
class LoadedDevice:
    device: AnyDevice
    source: str


@dataclass
class ConfigReport:
    config_dir: Path
    devices: list[LoadedDevice] = field(default_factory=list)
    brokers: dict[str, Broker] = field(default_factory=dict)
    alarms: AlarmsFile = field(default_factory=AlarmsFile)
    control: ControlFile = field(default_factory=ControlFile)
    notifications: NotificationsFile = field(default_factory=NotificationsFile)
    northbound: NorthboundFile = field(default_factory=NorthboundFile)
    modbus_server: ModbusServerFile = field(
        default_factory=lambda: ModbusServerFile(enabled=False))
    issues: list[Issue] = field(default_factory=list)
    # Every file that was read, mapped to its device_id (None if unknown or not a device file).
    sources: dict[str, str | None] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return any(i.severity is Severity.ERROR for i in self.issues)

    def tags_by_path(self) -> dict[str, tuple[AnyDevice, TagBase]]:
        """Every tag of every valid device, keyed by its full tag path."""
        out: dict[str, tuple[AnyDevice, TagBase]] = {}
        for loaded in self.devices:
            for tag in loaded.device.tags:
                out[loaded.device.tag_path(tag.name)] = (loaded.device, tag)
        return out

    def issues_for(self, source: str) -> list[Issue]:
        return [i for i in self.issues if i.source == source]

    def add(
        self, severity: Severity, source: str, message: str, location: str | None = None
    ) -> None:
        self.issues.append(
            Issue(severity=severity, source=source, message=message, location=location)
        )


# --------------------------------------------------------------------------------------------
# YAML reading
# --------------------------------------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last one)."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _read_yaml(path: Path, source: str, report: ConfigReport) -> Any | None:
    try:
        text = path.read_text(encoding="utf-8")
        return yaml.load(text, Loader=_UniqueKeyLoader)  # nosec B506  # SafeLoader subclass
    except (OSError, UnicodeDecodeError) as exc:
        report.add(Severity.ERROR, source, f"cannot read file: {exc}")
    except yaml.YAMLError as exc:
        report.add(Severity.ERROR, source, f"invalid YAML: {exc}")
    return None


def _format_location(loc: tuple[int | str, ...], raw: Any) -> str | None:
    """Turn a Pydantic error location into 'tags[2] (motor_speed).address'."""
    parts: list[str] = []
    node = raw
    for i, item in enumerate(loc):
        if i == 0 and item in PROTOCOLS:
            continue  # discriminated-union branch name, not a real field
        if isinstance(item, int):
            label = f"[{item}]"
            node = node[item] if isinstance(node, list) and item < len(node) else None
            if isinstance(node, dict) and isinstance(node.get("name"), str):
                label += f" ({node['name']})"
            parts.append(label)
        else:
            if item in ("anonymous", "username") and parts and parts[-1] == ".auth":
                continue  # auth union branch name
            parts.append(f".{item}")
            node = node.get(item) if isinstance(node, dict) else None
    return "".join(parts).lstrip(".") or None


def _add_validation_errors(
    report: ConfigReport, source: str, exc: ValidationError, raw: Any
) -> None:
    for err in exc.errors(include_url=False):
        msg = err["msg"].removeprefix("Value error, ")
        report.add(Severity.ERROR, source, msg, _format_location(err["loc"], raw))


# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------


def load_config(config_dir: Path | str, env: Mapping[str, str] | None = None) -> ConfigReport:
    """Load and validate everything under ``config_dir``.

    ``env`` is used to warn about referenced secret env vars that are not set; it defaults
    to ``os.environ``.
    """
    config_dir = Path(config_dir)
    report = ConfigReport(config_dir=config_dir)
    env = os.environ if env is None else env

    _load_brokers(report)
    _load_devices(report)

    _check_brokers(report, env)
    _check_device_ids(report)
    for loaded in report.devices:
        _check_device(report, loaded, env)
    _check_modbus_endpoints(report)
    _check_serial_lines(report)
    _check_mqtt_topics(report)
    _check_connection_budget(report)

    _load_optional(report, ALARMS_FILE, AlarmsFile, "alarms")
    _load_optional(report, CONTROL_FILE, ControlFile, "control")
    _load_optional(report, NOTIFICATIONS_FILE, NotificationsFile, "notifications")
    _load_optional(report, NORTHBOUND_FILE, NorthboundFile, "northbound")
    _load_optional(report, MODBUS_SERVER_FILE, ModbusServerFile, "modbus_server")
    _check_alarms(report)
    _check_control(report)
    _check_notifications(report, env)
    _check_northbound(report)
    _check_modbus_server(report)

    log.debug("config loaded: %d devices, %d issues", len(report.devices), len(report.issues))
    return report


def _load_brokers(report: ConfigReport) -> None:
    path = report.config_dir / BROKERS_FILE
    if not path.exists():
        return
    report.sources[BROKERS_FILE] = None
    raw = _read_yaml(path, BROKERS_FILE, report)
    if raw is None and not report.issues_for(BROKERS_FILE):
        raw = {}  # empty file
    if raw is None:
        return
    try:
        parsed = BrokersFile.model_validate(raw)
    except ValidationError as exc:
        _add_validation_errors(report, BROKERS_FILE, exc, raw)
        return
    for broker in parsed.brokers:
        if broker.broker_id in report.brokers:
            report.add(
                Severity.ERROR, BROKERS_FILE, f"duplicate broker_id {broker.broker_id!r}"
            )
            continue
        report.brokers[broker.broker_id] = broker


def _load_devices(report: ConfigReport) -> None:
    devices_dir = report.config_dir / DEVICES_DIR
    if not devices_dir.is_dir():
        report.add(Severity.WARNING, DEVICES_DIR, "no devices directory; no devices configured")
        return
    paths = sorted([*devices_dir.glob("*.yaml"), *devices_dir.glob("*.yml")])
    for path in paths:
        source = f"{DEVICES_DIR}/{path.name}"
        report.sources[source] = None
        raw = _read_yaml(path, source, report)
        if report.issues_for(source):
            continue
        if not isinstance(raw, dict):
            report.add(Severity.ERROR, source, "a device file must contain one YAML mapping")
            continue
        if isinstance(raw.get("device_id"), str):
            report.sources[source] = raw["device_id"]
        try:
            device = DEVICE_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            _add_validation_errors(report, source, exc, raw)
            continue
        report.devices.append(LoadedDevice(device=device, source=source))
        if path.stem != device.device_id:
            report.add(
                Severity.WARNING,
                source,
                f"file name should match device_id: rename to {device.device_id}.yaml",
            )


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------


def _warn_unset_env_vars(
    report: ConfigReport,
    source: str,
    refs: Iterable[tuple[str, str | None]],
    env: Mapping[str, str],
) -> None:
    """``refs`` are (location, env var name) pairs; a None name means 'not configured'."""
    for location, name in refs:
        if name and not env.get(name):
            report.add(Severity.WARNING, source, f"env var {name} is not set", location)


def _check_brokers(report: ConfigReport, env: Mapping[str, str]) -> None:
    for broker in report.brokers.values():
        where = f"brokers[{broker.broker_id}]"
        if broker.username_env and not broker.tls:
            report.add(
                Severity.WARNING,
                BROKERS_FILE,
                "credentials are sent without TLS; set tls: true",
                where,
            )
        _warn_unset_env_vars(
            report,
            BROKERS_FILE,
            [(f"{where}.username_env", broker.username_env),
             (f"{where}.password_env", broker.password_env),
             (f"{where}.ca_file_env", broker.ca_file_env),
             (f"{where}.client_certificate_env", broker.client_certificate_env),
             (f"{where}.client_private_key_env", broker.client_private_key_env)],
            env,
        )


def _check_device_ids(report: ConfigReport) -> None:
    by_id: dict[str, list[LoadedDevice]] = defaultdict(list)
    for loaded in report.devices:
        by_id[loaded.device.device_id].append(loaded)
    for device_id, group in by_id.items():
        for loaded in group:
            others = ", ".join(o.source for o in group if o is not loaded)
            if others:
                report.add(
                    Severity.ERROR,
                    loaded.source,
                    f"duplicate device_id {device_id!r} (also defined in {others})",
                    "device_id",
                )


def _tag_loc(index: int, name: str, field_name: str | None = None) -> str:
    loc = f"tags[{index}] ({name})"
    return f"{loc}.{field_name}" if field_name else loc


def _check_device(report: ConfigReport, loaded: LoadedDevice, env: Mapping[str, str]) -> None:
    device, source = loaded.device, loaded.source

    if not device.tags:
        report.add(Severity.WARNING, source, "device has no tags", "tags")

    seen: dict[str, int] = {}
    for i, tag in enumerate(device.tags):
        if tag.name in seen:
            report.add(
                Severity.ERROR,
                source,
                f"duplicate tag name {tag.name!r} (first used at tags[{seen[tag.name]}])",
                _tag_loc(i, tag.name, "name"),
            )
        else:
            seen[tag.name] = i

    if isinstance(device, ModbusDevice | ModbusRtuDevice):
        _check_modbus_device(report, device, source)
    elif isinstance(device, MqttDevice):
        _check_mqtt_device(report, device, source)
    elif isinstance(device, OpcUaDevice):
        _check_opcua_device(report, device, source, env)
    elif isinstance(device, S7Device):
        _warn_duplicate_addresses(report, source, [
            (t.name, f"{t.address.upper()}") for t in device.tags], "address")
    elif isinstance(device, HttpDevice):
        _check_http_device(report, device, source, env)
    elif isinstance(device, SnmpDevice):
        _check_snmp_device(report, device, source, env)
    elif isinstance(device, FinsDevice | McDevice):
        _check_word_plc(report, device, source)
    elif isinstance(device, EnipDevice):
        _warn_duplicate_addresses(report, source, [
            (t.name, f"{t.tag}.{t.bit}" if t.bit is not None else t.tag)
            for t in device.tags], "tag")
    elif isinstance(device, Iec104Device):
        _warn_duplicate_addresses(report, source, [
            (t.name, f"{t.type}:{t.ioa}") for t in device.tags], "ioa")


def _check_word_plc(report: ConfigReport, device: FinsDevice | McDevice, source: str) -> None:
    """Overlapping words of two numeric tags are an error (one of them is surely wrong);
    the same bit twice is a warning, as for Modbus."""
    spans: list[tuple[int, str, str, int, int]] = []
    bits: list[tuple[str, str]] = []
    for i, tag in enumerate(device.tags):
        a = tag.parsed
        if tag.data_type is DataType.BOOL:
            bits.append((tag.name, f"{a.area}{a.word}.{a.bit}"))
        else:
            spans.append((i, tag.name, a.area, a.word, a.word + tag.word_count - 1))
    _warn_duplicate_addresses(report, source, bits, "address")
    for (i, na, area_a, sa, ea), (j, nb, area_b, sb, eb) in combinations(spans, 2):
        if area_a == area_b and sa <= eb and sb <= ea:
            report.add(Severity.ERROR, source,
                       f"{area_b}{sb}..{area_b}{eb} overlaps tags[{i}] ({na}) at "
                       f"{area_a}{sa}..{area_a}{ea}", _tag_loc(j, nb, "address"))


def _check_http_device(report: ConfigReport, device: HttpDevice, source: str,
                       env: Mapping[str, str]) -> None:
    c = device.connection
    auth = c.auth
    refs: list[tuple[str, str | None]] = [("connection.ca_file_env", c.ca_file_env)]
    if isinstance(auth, HttpBasicAuth):
        refs += [("connection.auth.username_env", auth.username_env),
                 ("connection.auth.password_env", auth.password_env)]
    elif isinstance(auth, HttpBearerAuth):
        refs.append(("connection.auth.token_env", auth.token_env))
    elif isinstance(auth, HttpHeaderAuth):
        refs.append(("connection.auth.value_env", auth.value_env))
    _warn_unset_env_vars(report, source, refs, env)
    if auth.type != "none" and c.base_url.startswith("http://"):
        report.add(Severity.WARNING, source,
                   "credentials are sent unencrypted over http://; use https://",
                   "connection.base_url")
    if not c.verify_tls and c.base_url.startswith("https://"):
        report.add(Severity.WARNING, source,
                   "verify_tls: false accepts any certificate (no protection against a fake "
                   "device); prefer ca_file_env", "connection.verify_tls")
    _warn_duplicate_addresses(report, source, [
        (t.name, f"{t.path} {t.json_path}") for t in device.tags], "path/json_path")


def _check_snmp_device(report: ConfigReport, device: SnmpDevice, source: str,
                       env: Mapping[str, str]) -> None:
    sec = device.connection.security
    if isinstance(sec, SnmpV2c):
        _warn_unset_env_vars(report, source, [("connection.security.community_env",
                                               sec.community_env)], env)
        report.add(Severity.WARNING, source,
                   "SNMPv2c sends the community in clear text; prefer SNMPv3 with auth and priv",
                   "connection.security")
    else:
        _warn_unset_env_vars(report, source, [
            ("connection.security.auth_key_env", sec.auth_key_env),
            ("connection.security.priv_key_env", sec.priv_key_env)], env)
        if sec.priv_protocol is None:
            report.add(Severity.WARNING, source, "SNMPv3 without priv: values travel "
                       "unencrypted", "connection.security")
    _warn_duplicate_addresses(report, source, [(t.name, t.oid) for t in device.tags], "oid")


def _check_modbus_device(report: ConfigReport, device: ModbusDevice | ModbusRtuDevice,
                         source: str) -> None:
    limit = device.connection.max_registers_per_read
    indexed = list(enumerate(device.tags))
    for i, tag in indexed:
        if tag.register_count > limit:
            report.add(
                Severity.ERROR,
                source,
                f"tag needs {tag.register_count} registers but "
                f"connection.max_registers_per_read is {limit}",
                _tag_loc(i, tag.name, "count"),
            )
    for (i, a), (j, b) in combinations(indexed, 2):
        if a.register_type != b.register_type or not (
            a.address <= b.last_address and b.address <= a.last_address
        ):
            continue
        if a.bit is not None or b.bit is not None:
            # Bit tags may share a register with other bits and with the whole-word tag
            # (e.g. a raw status word next to its decoded flags); only overlapping bits of the
            # same register are a mistake.
            if (a.bit is not None and b.bit is not None and a.address == b.address
                    and _bits(a) & _bits(b)):
                report.add(
                    Severity.ERROR,
                    source,
                    f"bit {b.bit} of {b.register_type} {b.address} is already used by "
                    f"tags[{i}] ({a.name})",
                    _tag_loc(j, b.name, "bit"),
                )
            continue
        report.add(
            Severity.ERROR,
            source,
            f"{b.register_type} range {b.address}..{b.last_address} overlaps "
            f"tags[{i}] ({a.name}) at {a.address}..{a.last_address}",
            _tag_loc(j, b.name, "address"),
        )


def _bits(tag: ModbusTag) -> set[int]:
    start = tag.bit or 0
    return set(range(start, start + (tag.bit_count or 1)))


def _check_mqtt_device(report: ConfigReport, device: MqttDevice, source: str) -> None:
    if device.connection.broker_ref not in report.brokers:
        known = ", ".join(sorted(report.brokers)) or "none defined"
        report.add(
            Severity.ERROR,
            source,
            f"unknown broker_ref {device.connection.broker_ref!r} "
            f"(brokers in {BROKERS_FILE}: {known})",
            "connection.broker_ref",
        )
    if device.connection.status_topic is None and device.connection.stale_after_s is None:
        report.add(
            Severity.WARNING,
            source,
            "no status_topic (LWT) and no stale_after_s: if the device dies silently its last "
            "values stay 'good' forever; set stale_after_s",
            "connection.stale_after_s",
        )
    _warn_duplicate_addresses(report, source, [(t.name, t.json_path) for t in device.tags],
                              "json_path")


def _check_opcua_device(
    report: ConfigReport, device: OpcUaDevice, source: str, env: Mapping[str, str]
) -> None:
    conn = device.connection
    env_refs = [
        ("connection.client_certificate_env", conn.client_certificate_env),
        ("connection.client_private_key_env", conn.client_private_key_env),
    ]
    if isinstance(conn.auth, UsernameAuth):
        env_refs += [
            ("connection.auth.username_env", conn.auth.username_env),
            ("connection.auth.password_env", conn.auth.password_env),
        ]
        if conn.security_policy is SecurityPolicy.NONE:
            report.add(
                Severity.WARNING,
                source,
                "username/password is sent unencrypted with security_policy None",
                "connection.security_policy",
            )
    _warn_unset_env_vars(report, source, env_refs, env)
    _warn_duplicate_addresses(report, source, [(t.name, t.node_id) for t in device.tags],
                              "node_id")


def _warn_duplicate_addresses(
    report: ConfigReport, source: str, pairs: list[tuple[str, str]], field_name: str
) -> None:
    first: dict[str, tuple[int, str]] = {}
    for i, (name, address) in enumerate(pairs):
        if address in first:
            j, other = first[address]
            report.add(
                Severity.WARNING,
                source,
                f"same {field_name} as tags[{j}] ({other}); intentional?",
                _tag_loc(i, name, field_name),
            )
        else:
            first[address] = (i, name)


def _collision_severity(a: AnyDevice, b: AnyDevice) -> tuple[Severity, str]:
    """Collisions involving a disabled device are only warnings (e.g. a replaced device)."""
    if a.enabled and b.enabled:
        return Severity.ERROR, ""
    return Severity.WARNING, " (a disabled device is involved)"


def _check_modbus_endpoints(report: ConfigReport) -> None:
    by_endpoint: dict[tuple[str, int, int], list[LoadedDevice]] = defaultdict(list)
    for loaded in report.devices:
        if isinstance(loaded.device, ModbusDevice):
            c = loaded.device.connection
            by_endpoint[(c.host.lower(), c.port, c.unit_id)].append(loaded)
    for (host, port, unit_id), group in by_endpoint.items():
        for a, b in combinations(group, 2):
            severity, note = _collision_severity(a.device, b.device)
            for this, other in ((a, b), (b, a)):
                report.add(
                    severity,
                    this.source,
                    f"{host}:{port} unit_id {unit_id} is also used by "
                    f"{other.device.device_id!r} ({other.source}){note}",
                    "connection.unit_id",
                )


def _check_serial_lines(report: ConfigReport) -> None:
    """RTU devices on one serial port: unique unit ids, identical line settings."""
    by_port: dict[str, list[LoadedDevice]] = defaultdict(list)
    for loaded in report.devices:
        if isinstance(loaded.device, ModbusRtuDevice):
            by_port[loaded.device.connection.port.lower()].append(loaded)
    for port, group in by_port.items():
        settings = {g.device.connection.line_settings() for g in group}  # type: ignore[union-attr]
        if len(settings) > 1:
            for g in group:
                c = g.device.connection  # type: ignore[union-attr]
                report.add(Severity.ERROR, g.source,
                           f"serial port {port} is shared with other devices using different "
                           f"line settings (here {c.baudrate} {c.bytesize}{c.parity}{c.stopbits})",
                           "connection.baudrate")
        for a, b in combinations(group, 2):
            if a.device.connection.unit_id != b.device.connection.unit_id:  # type: ignore[union-attr]
                continue
            severity, note = _collision_severity(a.device, b.device)
            for this, other in ((a, b), (b, a)):
                report.add(severity, this.source,
                           f"{port} unit_id {this.device.connection.unit_id} is also used by "  # type: ignore[union-attr]
                           f"{other.device.device_id!r} ({other.source}){note}",
                           "connection.unit_id")


# On Windows the hub runs on the selector event loop (aiomqtt and psycopg need it), whose
# select() handles at most 512 sockets; past that the whole loop fails. Keep room for the
# database pool, API clients and the outputs (docs/backend.md section 26).
WINDOWS_SOCKET_LIMIT = 512
DEVICE_CONNECTIONS_MAX = 400
DEVICE_CONNECTIONS_WARN = 320


def device_connections(devices: Iterable[AnyDevice]) -> int:
    """Network connections the drivers open for the enabled devices.

    Modbus TCP devices on one host:port (a gateway) share one connection; every other
    device has its own (MQTT: one client per device). Serial ports are not sockets.
    """
    modbus: set[tuple[str, int, str]] = set()
    serial_sockets: set[str] = set()
    other = 0
    for device in devices:
        if not device.enabled:
            continue
        if isinstance(device, ModbusDevice):
            c = device.connection
            modbus.add((c.host.lower(), c.port, c.framer))
        elif isinstance(device, ModbusRtuDevice):
            port = device.connection.port.lower()
            if "://" in port:                      # socket:// or rfc2217:// serial servers
                serial_sockets.add(port)
        else:
            other += 1
    return len(modbus) + len(serial_sockets) + other


def _check_connection_budget(report: ConfigReport, platform: str = sys.platform) -> None:
    n = device_connections(d.device for d in report.devices)
    if platform != "win32" or n <= DEVICE_CONNECTIONS_WARN:
        return
    severity = Severity.ERROR if n > DEVICE_CONNECTIONS_MAX else Severity.WARNING
    report.add(severity, "devices",
               f"{n} device connections: on Windows one hub handles at most "
               f"{DEVICE_CONNECTIONS_MAX} (the event loop is limited to {WINDOWS_SOCKET_LIMIT} "
               "sockets). Put Modbus devices behind gateways, run the hub on Linux (Docker), "
               "or split the devices over two hubs")


def _check_mqtt_topics(report: ConfigReport) -> None:
    entries: list[tuple[LoadedDevice, str, str]] = []  # (device, role, topic)
    for loaded in report.devices:
        if isinstance(loaded.device, MqttDevice):
            c = loaded.device.connection
            for role in ("telemetry_topic", "command_topic", "status_topic"):
                topic = getattr(c, role)
                if topic is not None:
                    entries.append((loaded, role, topic))

    for (la, role_a, ta), (lb, role_b, tb) in combinations(entries, 2):
        ca, cb = la.device.connection, lb.device.connection
        if ca.broker_ref != cb.broker_ref or not topics_overlap(ta, tb):
            continue
        if la is lb:
            report.add(
                Severity.ERROR,
                la.source,
                f"{role_a} {ta!r} overlaps {role_b} {tb!r} of the same device",
                f"connection.{role_b}",
            )
            continue
        severity, note = _collision_severity(la.device, lb.device)
        for (this, role, topic), (other, other_role, other_topic) in (
            ((la, role_a, ta), (lb, role_b, tb)),
            ((lb, role_b, tb), (la, role_a, ta)),
        ):
            report.add(
                severity,
                this.source,
                f"{role} {topic!r} collides with {other_role} {other_topic!r} of "
                f"{other.device.device_id!r} ({other.source}) on broker "
                f"{ca.broker_ref!r}{note}",
                f"connection.{role}",
            )


# --------------------------------------------------------------------------------------------
# Alarms and control policy
# --------------------------------------------------------------------------------------------


def _load_optional(report: ConfigReport, name: str, model: type[BaseModel], attr: str) -> None:
    path = report.config_dir / name
    if not path.exists():
        return
    report.sources[name] = None
    raw = _read_yaml(path, name, report)
    if report.issues_for(name):
        return
    try:
        setattr(report, attr, model.model_validate(raw or {}))
    except ValidationError as exc:
        _add_validation_errors(report, name, exc, raw)


def _is_numeric(tag: TagBase) -> bool:
    return tag.data_type in NUMERIC_TYPES


def _check_alarms(report: ConfigReport) -> None:
    tags = report.tags_by_path()
    seen: set[str] = set()
    for i, rule in enumerate(report.alarms.rules):
        where = f"rules[{i}] ({rule.tag})"
        if rule.tag in seen:
            report.add(Severity.ERROR, ALARMS_FILE, "a tag can have only one alarm rule", where)
        seen.add(rule.tag)
        found = tags.get(rule.tag)
        if found is None:
            report.add(Severity.ERROR, ALARMS_FILE, f"unknown tag {rule.tag}", where)
            continue
        tag = found[1]
        if rule.equals is not None and tag.data_type is not DataType.BOOL:
            report.add(Severity.ERROR, ALARMS_FILE,
                       f"equals is for bool tags; {rule.tag} is {tag.data_type}", where)
        if rule.equals is None and not _is_numeric(tag):
            report.add(Severity.ERROR, ALARMS_FILE,
                       f"hi/lo limits need a numeric tag; {rule.tag} is {tag.data_type}", where)


def _check_control(report: ConfigReport) -> None:
    tags = report.tags_by_path()
    policies = [("defaults", None, report.control.defaults)]
    policies += [(f"tags[{p}]", p, pol) for p, pol in report.control.tags.items()]
    for where, path, policy in policies:
        if path is not None:
            found = tags.get(path)
            if found is None:
                report.add(Severity.ERROR, CONTROL_FILE, f"unknown tag {path}", where)
            elif found[1].access is not Access.READ_WRITE:
                report.add(Severity.ERROR, CONTROL_FILE,
                           f"{path} is not read_write; a control policy has no effect", where)
            elif policy.max_step is not None and not _is_numeric(found[1]):
                report.add(Severity.ERROR, CONTROL_FILE, "max_step only applies to numeric tags",
                           where)
        for j, lock in enumerate(policy.interlocks):
            loc = f"{where}.interlocks[{j}]"
            target = tags.get(lock.tag)
            if target is None:
                report.add(Severity.ERROR, CONTROL_FILE, f"unknown interlock tag {lock.tag}", loc)
                continue
            ttag = target[1]
            if ttag.data_type is DataType.BOOL and not isinstance(lock.value, bool):
                report.add(Severity.ERROR, CONTROL_FILE, f"{lock.tag} is bool; value must be "
                           "true or false", loc)
            elif _is_numeric(ttag) and isinstance(lock.value, bool | str):
                report.add(Severity.ERROR, CONTROL_FILE, f"{lock.tag} is numeric; value must be "
                           "a number", loc)
            elif lock.op in ("lt", "le", "gt", "ge") and not _is_numeric(ttag):
                report.add(Severity.ERROR, CONTROL_FILE, f"{lock.op} needs a numeric tag", loc)


def _check_northbound(report: ConfigReport) -> None:
    import fnmatch

    names: set[str] = set()
    paths = list(report.tags_by_path())
    for i, target in enumerate(report.northbound.targets):
        where = f"targets[{i}] ({target.name})"
        if target.name in names:
            report.add(Severity.ERROR, NORTHBOUND_FILE, f"duplicate target name {target.name!r}",
                       where)
        names.add(target.name)
        if target.broker_ref not in report.brokers:
            known = ", ".join(sorted(report.brokers)) or "none"
            report.add(Severity.ERROR, NORTHBOUND_FILE, f"unknown broker {target.broker_ref!r} "
                       f"(brokers in {BROKERS_FILE}: {known})", f"{where}.broker_ref")
        for pattern in target.tags:
            if not any(fnmatch.fnmatchcase(p, pattern) for p in paths):
                report.add(Severity.WARNING, NORTHBOUND_FILE,
                           f"pattern {pattern!r} matches no tag", f"{where}.tags")


def _check_modbus_server(report: ConfigReport) -> None:
    cfg = report.modbus_server
    tags = report.tags_by_path()
    used: dict[tuple[RegisterType, int], int] = {}
    for i, point in enumerate(cfg.points):
        where = f"points[{i}] ({point.tag})"
        found = tags.get(point.tag)
        if found is None:
            report.add(Severity.ERROR, MODBUS_SERVER_FILE, f"unknown tag {point.tag}", where)
            continue
        tag = found[1]
        if point.source == "value" and tag.data_type is DataType.STRING:
            report.add(Severity.ERROR, MODBUS_SERVER_FILE, "string tags cannot be served",
                       where)
            continue
        bit_area = point.register_type in BIT_REGISTER_TYPES
        if point.source == "value" and bit_area and tag.data_type is not DataType.BOOL:
            report.add(Severity.ERROR, MODBUS_SERVER_FILE,
                       f"{point.register_type} needs a bool tag; {point.tag} is "
                       f"{tag.data_type}", where)
        if point.writable:
            if tag.access is not Access.READ_WRITE:
                report.add(Severity.ERROR, MODBUS_SERVER_FILE,
                           f"{point.tag} is read-only in its device config; it cannot be "
                           "writable here", f"{where}.writable")
            if tag.data_type is DataType.BOOL and point.register_type is not RegisterType.COIL:
                report.add(Severity.ERROR, MODBUS_SERVER_FILE,
                           "a writable bool tag is served as a coil", f"{where}.register_type")
        if point.source == "quality" or bit_area:
            width = 1
        else:
            dtype = point.data_type or (DataType.FLOAT32 if tag.data_type is DataType.BOOL
                                        else tag.data_type)
            width = MODBUS_WIDTH[dtype]
        if point.address + width - 1 > 65535:
            report.add(Severity.ERROR, MODBUS_SERVER_FILE, "address range exceeds 65535",
                       f"{where}.address")
        for a in range(point.address, point.address + width):
            key = (point.register_type, a)
            if key in used:
                j = used[key]
                report.add(Severity.ERROR, MODBUS_SERVER_FILE,
                           f"{point.register_type} {a} is already used by points[{j}] "
                           f"({cfg.points[j].tag})", f"{where}.address")
                break
            used[key] = i
    if cfg.enabled and cfg.points and cfg.host not in ("127.0.0.1", "localhost", "::1") \
            and not cfg.allowed_clients:
        report.add(Severity.WARNING, MODBUS_SERVER_FILE,
                   "Modbus has no authentication: limit readers with allowed_clients "
                   "(e.g. the SCADA server's address)", "allowed_clients")


def _check_notifications(report: ConfigReport, env: Mapping[str, str]) -> None:
    n = report.notifications
    for i, rule in enumerate(n.rules):
        for name in rule.channels:
            if name not in n.channels:
                report.add(Severity.ERROR, NOTIFICATIONS_FILE, f"unknown channel {name!r}",
                           f"rules[{i}] ({rule.name}).channels")
    names = [r.name for r in n.rules]
    for name in {x for x in names if names.count(x) > 1}:
        report.add(Severity.ERROR, NOTIFICATIONS_FILE, f"duplicate rule name {name!r}", "rules")
    for name, channel in n.channels.items():
        refs = [(f"channels[{name}].{attr}", getattr(channel, attr, None))
                for attr in ("username_env", "password_env", "bot_token_env", "api_key_env",
                             "secret_env")]
        _warn_unset_env_vars(report, NOTIFICATIONS_FILE, refs, env)
