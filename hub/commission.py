"""Field commissioning toolkit. Read-only: it reads, browses and listens, it never writes.

    hub-commission read plc_01 meter_01 --seconds 20 --out sat/plc_01.md
    hub-commission modbus-probe --host 10.0.0.5 --unit 1 --address 100 --expect 230.4
    hub-commission modbus-scan --host 10.0.0.9 --units 1-32
    hub-commission opcua-browse --endpoint opc.tcp://10.0.0.7:4840 --yaml
    hub-commission mqtt-listen --broker plant_broker --topic "site/#" --seconds 15 --yaml
    hub-commission snmp-walk --host 10.0.0.20 --community-env UPS_COMMUNITY --oid 1.3.6.1.2.1.33
    hub-commission s7-dump --host 10.0.0.8 --db 1 --size 32 --expect 41.5
    hub-commission http-get --device inverter_01 --path /api/status --yaml

``read`` runs the real drivers on configured devices (also disabled ones, so a device can be
checked before it is enabled) and reports every tag with quality, updates, min/max and hints.
The other commands talk to a device that may not be configured yet and print what it holds, plus
tag suggestions (``--yaml``) to paste into config/devices/<device_id>.yaml.

``--device <id>`` takes host, port, security and credentials from that device's configuration;
credentials always come from environment variables (``--env-file`` loads a .env file).
Writing to a device is only possible through the control-safety layer of the API.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from hub.codec import CodecError, decode_registers
from hub.config_loader import ConfigReport, LoadedDevice, load_config
from hub.drivers.base import DriverError, utcnow
from hub.models import (
    DataType,
    HttpConnection,
    HttpDevice,
    ModbusDevice,
    ModbusRtuDevice,
    MqttDevice,
    OpcUaConnection,
    OpcUaDevice,
    Order,
    S7Device,
    SnmpConnection,
    SnmpDevice,
    parse_json_path,
)
from hub.runtime import Hub, run
from hub.validate_config import print_report

log = logging.getLogger(__name__)

EXIT_OK, EXIT_PROBLEM, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    """Wrong or missing arguments; printed without a traceback."""


# --------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------


def tag_name(text: str, taken: set[str] | None = None) -> str:
    """A valid tag name (lowercase ASCII snake_case) suggested from a label, unique in taken."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "value"
    if not s[0].isalpha():
        s = f"t_{s}"
    s = s[:60].rstrip("_")
    if taken is not None:
        base, n = s, 2
        while s in taken:
            s = f"{base}_{n}"
            n += 1
        taken.add(s)
    return s


def _json_key(key: str) -> str:
    try:
        parse_json_path(f"$.{key}")
        return f".{key}"
    except ValueError:
        return f"['{key}']"


def flatten(node: object, path: str = "$") -> Iterator[tuple[str, object]]:
    """Every leaf of a JSON document with its JSON path (the hub's path syntax)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if "'" in str(key) or "\\" in str(key):
                continue  # not addressable with the supported paths
            yield from flatten(value, path + _json_key(str(key)))
    elif isinstance(node, list):
        for i, value in enumerate(node[:50]):
            yield from flatten(value, f"{path}[{i}]")
    else:
        yield path, node


def json_data_type(value: object) -> DataType | None:
    if isinstance(value, bool):
        return DataType.BOOL
    if isinstance(value, int):
        return DataType.INT32 if -2**31 <= value < 2**31 else DataType.INT64
    if isinstance(value, float):
        return DataType.FLOAT64
    if isinstance(value, str):
        return DataType.STRING
    return None


def path_label(path: str) -> str:
    """$.data.temp -> data_temp (for tag name suggestions)."""
    return "_".join(str(p) for p in parse_json_path(path))


def dump_yaml(tags: list[dict[str, Any]]) -> str:
    return yaml.safe_dump({"tags": tags}, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)


def fmt(value: object) -> str:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return f"{value:.6g}"
    if isinstance(value, str) and len(value) > 60:
        return repr(value[:57] + "...")
    return repr(value) if isinstance(value, str) else str(value)


def scale_for(value: float, expected: float, tolerance: float) -> float | None:
    """The power of ten s with value * s ≈ expected (within tolerance, relative), or None."""
    if value == 0 or not math.isfinite(value):
        return None
    for k in range(-4, 5):
        s = 10.0 ** k
        if math.isclose(value * s, expected, rel_tol=tolerance, abs_tol=1e-9):
            return float(f"{s:g}")
    return None


def load_report(config_dir: Path) -> ConfigReport:
    report = load_config(config_dir)
    if report.has_errors:
        print_report(report, sys.stderr)
        raise UsageError(f"{config_dir}: the configuration has errors (see above)")
    return report


def configured_device(config_dir: Path, device_id: str, kind: tuple[type, ...]) -> Any:
    report = load_report(config_dir)
    for loaded in report.devices:
        if loaded.device.device_id == device_id:
            if not isinstance(loaded.device, kind):
                raise UsageError(f"{device_id} is a {loaded.device.protocol} device")
            return loaded.device
    raise UsageError(f"no device {device_id!r} in {config_dir}")


# --------------------------------------------------------------------------------------------
# read: configured devices through the real drivers
# --------------------------------------------------------------------------------------------


@dataclass
class TagCheck:
    name: str
    path: str
    unit: str | None
    value: Any = None
    quality: str | None = None
    updates: int = 0
    low: float | None = None
    high: float | None = None
    last_ts: datetime | None = None
    hints: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.quality == "good" and self.value is not None


@dataclass
class DeviceCheck:
    device_id: str
    protocol: str
    name: str
    state: str = "unknown"
    last_error: str | None = None
    tags: list[TagCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == "online" and all(t.ok for t in self.tags)


def _hints(device: Any, tag: Any, check: TagCheck) -> list[str]:
    hints: list[str] = []
    modbus = isinstance(device, ModbusDevice | ModbusRtuDevice)
    if check.updates == 0:
        hints.append("no value received: check address / node / topic / path")
    elif check.value is None:
        hints.append("read failed (BAD): address or data type wrong, or the point does not "
                     "exist")
    elif check.quality and check.quality != "good":
        hints.append(f"quality {check.quality}: outside the expected range or device reports "
                     "a problem")
    value = check.value
    if isinstance(value, float) and value is not None:
        if not math.isfinite(value) or abs(value) > 1e9 or 0 < abs(value) < 1e-9:
            hints.append("implausible number"
                         + (": check word_order / byte_order (hub-commission modbus-probe)"
                            if modbus else ": check the data type"))
    if isinstance(value, int | float) and not isinstance(value, bool):
        if tag.min is not None and value < tag.min or tag.max is not None and value > tag.max:
            hints.append(f"outside min/max ({tag.min} .. {tag.max})")
    return hints


async def read_devices(report: ConfigReport, device_ids: list[str], seconds: float
                       ) -> list[DeviceCheck]:
    wanted: list[LoadedDevice] = []
    known = {d.device.device_id: d for d in report.devices}
    for device_id in device_ids or sorted(known):
        if device_id not in known:
            raise UsageError(f"no device {device_id!r} in {report.config_dir}")
        loaded = known[device_id]
        if not loaded.device.enabled:   # checking before enabling is the point of commissioning
            loaded = LoadedDevice(loaded.device.model_copy(update={"enabled": True}),
                                  loaded.source)
        wanted.append(loaded)
    hub = Hub(dataclasses.replace(report, devices=wanted))
    checks: dict[str, TagCheck] = {}
    for loaded in wanted:
        for tag in loaded.device.tags:
            path = loaded.device.tag_path(tag.name)
            checks[path] = TagCheck(tag.name, path, tag.unit)
    loop = asyncio.get_running_loop()
    async with hub:
        end = loop.time() + seconds
        while loop.time() < end:
            for path, check in checks.items():
                sample = hub.latest.get(path)
                if sample is None or sample.timestamp == check.last_ts:
                    continue
                check.updates += 1
                check.last_ts = sample.timestamp
                check.value, check.quality = sample.value, str(sample.quality)
                v = sample.value
                if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v):
                    check.low = v if check.low is None else min(check.low, v)
                    check.high = v if check.high is None else max(check.high, v)
            await asyncio.sleep(0.2)
        statuses = dict(hub.statuses)
    out = []
    for loaded in wanted:
        d = loaded.device
        status = statuses[d.device_id]
        dc = DeviceCheck(d.device_id, d.protocol, d.name, str(status.state), status.last_error)
        for tag in d.tags:
            check = checks[d.tag_path(tag.name)]
            # an offline device's error says it all; no hint per tag
            check.hints = [] if check.updates == 0 and dc.state != "online" else \
                    _hints(d, tag, check)
            dc.tags.append(check)
        out.append(dc)
    return out


def render_text(results: list[DeviceCheck]) -> str:
    lines = []
    for dc in results:
        mark = "OK " if dc.ok else "!! "
        err = f"  ({dc.last_error})" if dc.last_error and dc.state != "online" else ""
        lines.append(f"{mark}{dc.device_id} [{dc.protocol}] {dc.state}{err}")
        for t in dc.tags:
            value = "-" if t.updates == 0 else ("BAD" if t.value is None else fmt(t.value))
            unit = f" {t.unit}" if t.unit and t.value is not None else ""
            span = f"  [{fmt(t.low)} .. {fmt(t.high)}]" if t.low is not None and \
                t.low != t.high else ""
            lines.append(f"    {'ok' if t.ok else '!!'} {t.name:<28} {value}{unit}  "
                         f"q={t.quality or '-'} n={t.updates}{span}")
            for hint in t.hints:
                lines.append(f"         -> {hint}")
    good = sum(dc.ok for dc in results)
    lines.append(f"{good}/{len(results)} device(s) passed")
    return "\n".join(lines)


def render_markdown(results: list[DeviceCheck], seconds: float) -> str:
    now = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Commissioning read test — {now}", "",
           f"Test duration: {seconds:g} s per run. Tool: `hub-commission read` (read-only).", ""]
    for dc in results:
        out += [f"## {dc.device_id} — {dc.name}", "",
                f"* Protocol: {dc.protocol}", f"* State: **{dc.state}**"
                + (f" ({dc.last_error})" if dc.last_error and dc.state != "online" else ""),
                f"* Result: **{'PASS' if dc.ok else 'CHECK'}**", "",
                "| Tag | Value | Unit | Quality | Updates | Min | Max | Notes | Verified on site |",
                "|---|---|---|---|---|---|---|---|---|"]
        for t in dc.tags:
            value = "-" if t.updates == 0 else ("BAD" if t.value is None else fmt(t.value))
            out.append(f"| {t.name} | {value} | {t.unit or ''} | {t.quality or '-'} | "
                       f"{t.updates} | {fmt(t.low) if t.low is not None else ''} | "
                       f"{fmt(t.high) if t.high is not None else ''} | "
                       f"{'; '.join(t.hints)} | ☐ |")
        out.append("")
    out += ["Signatures: commissioning engineer ______  operator ______  date ______", ""]
    return "\n".join(out)


def as_json(results: list[DeviceCheck]) -> str:
    def default(o: object) -> object:
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(type(o))
    doc = [dict(dataclasses.asdict(dc), ok=dc.ok) for dc in results]
    return json.dumps(doc, indent=2, default=default, ensure_ascii=False)


async def cmd_read(args: argparse.Namespace) -> int:
    report = load_report(args.config_dir)
    results = await read_devices(report, args.device, args.seconds)
    print(render_text(results))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        text = as_json(results) if args.out.suffix == ".json" else \
            render_markdown(results, args.seconds)
        args.out.write_text(text, encoding="utf-8")
        print(f"report written: {args.out}")
    return EXIT_OK if all(dc.ok for dc in results) else EXIT_PROBLEM


# --------------------------------------------------------------------------------------------
# Modbus
# --------------------------------------------------------------------------------------------

_ORDERS: list[tuple[Order, Order]] = [("big", "big"), ("big", "little"),
                                      ("little", "big"), ("little", "little")]
_REFS = {4: "holding", 3: "input", 1: "discrete_input", 0: "coil"}


def parse_ref(ref: int) -> tuple[str, int]:
    """A 5- or 6-digit datasheet reference (40001, 300101) as (register_type, 0-based address)."""
    text = str(ref)
    if len(text) not in (5, 6) or int(text[0]) not in _REFS:
        raise UsageError(f"--ref {ref}: expected 4xxxx/3xxxx/1xxxx/0xxxx (5 or 6 digits)")
    number = int(text[1:])
    if number < 1:
        raise UsageError(f"--ref {ref}: references count from 1")
    return _REFS[int(text[0])], number - 1


@dataclass
class Interpretation:
    address: int
    data_type: DataType
    byte_order: Order
    word_order: Order
    value: int | float


def interpretations(start: int, registers: list[int]) -> list[Interpretation]:
    out: list[Interpretation] = []
    for i, _ in enumerate(registers):
        for dt in (DataType.UINT16, DataType.INT16):
            out.append(Interpretation(start + i, dt, "big", "big",
                                      decode_registers(registers[i:i + 1], dt)))  # type: ignore[arg-type]
        for width, types in ((2, (DataType.FLOAT32, DataType.INT32, DataType.UINT32)),
                             (4, (DataType.FLOAT64,))):
            chunk = registers[i:i + width]
            if len(chunk) < width:
                continue
            for bo, wo in _ORDERS:
                for dt in types:
                    try:
                        v = decode_registers(chunk, dt, bo, wo)
                    except CodecError:
                        continue
                    out.append(Interpretation(start + i, dt, bo, wo, v))  # type: ignore[arg-type]
    return out


def plausible(value: float) -> bool:
    return math.isfinite(value) and (value == 0 or 1e-6 < abs(value) < 1e9)


async def _modbus_client(args: argparse.Namespace) -> tuple[Any, int, str, tuple[Order, Order]]:
    from pymodbus import FramerType
    from pymodbus.client import AsyncModbusTcpClient

    from hub.drivers.modbus_tcp import _make_client

    if args.device:
        device = configured_device(args.config_dir, args.device, (ModbusDevice, ModbusRtuDevice))
        c = device.connection
        where = c.port if isinstance(device, ModbusRtuDevice) else f"{c.host}:{c.port}"
        client = _make_client(device)
        unit, order = c.unit_id, (c.byte_order, c.word_order)
        if args.unit is not None:
            unit = args.unit
    else:
        if not args.host:
            raise UsageError("give --host (or --device)")
        framer = FramerType.RTU if args.framer == "rtu" else FramerType.SOCKET
        client = AsyncModbusTcpClient(args.host, port=args.port, framer=framer,
                                      timeout=args.timeout_ms / 1000, retries=0,
                                      reconnect_delay=0)
        where, unit = f"{args.host}:{args.port}", 1 if args.unit is None else args.unit
        order = ("big", "big")
    await client.connect()
    if not client.connected:
        client.close()
        raise DriverError(f"cannot connect to {where}")
    return client, unit, where, order


async def _modbus_read(client: Any, register: str, address: int, count: int, unit: int
                       ) -> list[int] | list[bool] | str:
    from pymodbus.exceptions import ModbusException

    call = {"holding": client.read_holding_registers, "input": client.read_input_registers,
            "coil": client.read_coils, "discrete_input": client.read_discrete_inputs}[register]
    try:
        response = await call(address, count=count, device_id=unit)
    except ModbusException as exc:
        return f"no answer ({exc})"
    if response.isError():
        code = getattr(response, "exception_code", None)
        meaning = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
                   4: "device failure", 10: "gateway path unavailable",
                   11: "gateway target did not respond"}.get(code or 0, "error")
        return f"exception {code} ({meaning})"
    if register in ("coil", "discrete_input"):
        return list(response.bits[:count])
    return list(response.registers)


async def cmd_modbus_probe(args: argparse.Namespace) -> int:
    register, address = args.register, args.address
    if args.ref is not None:
        register, address = parse_ref(args.ref)
    if address is None:
        raise UsageError("give --address (0-based) or --ref (datasheet number, e.g. 40101)")
    client, unit, where, (cfg_bo, cfg_wo) = await _modbus_client(args)
    try:
        # one register before the address shows a datasheet that counts from 1
        start = address - 1 if address > 0 else address
        count = args.count + (address - start)
        data = await _modbus_read(client, register, start, count, unit)
        if isinstance(data, str) and start != address:
            start, count = address, args.count
            data = await _modbus_read(client, register, start, count, unit)
    finally:
        client.close()
    print(f"{register} {start}..{start + count - 1}, unit {unit} at {where}")
    if isinstance(data, str):
        print(f"  {data}")
        if "illegal data address" in data:
            print("  -> the unit answers, but not at this address: try --address one lower/"
                  "higher, the other register type, or a smaller --count")
        elif "no answer" in data:
            print("  -> nothing answered: check IP/port, unit id (hub-commission modbus-scan), "
                  "firewall, and that no other master holds the only connection")
        return EXIT_PROBLEM
    if register in ("coil", "discrete_input"):
        for i, bit in enumerate(data):
            print(f"  {start + i:>5}{'*' if start + i < address else ' '}  {int(bit)}")
        return EXIT_OK
    regs: list[int] = data  # type: ignore[assignment]
    print("  addr    hex     uint16   int16")
    for i, r in enumerate(regs):
        flag = "*" if start + i < address else " "
        print(f"  {start + i:>5}{flag} 0x{r:04X}  {r:>7}  {r - 65536 if r > 32767 else r:>7}")
    if start < address:
        print("  * = address-1: use it if the datasheet numbers registers from 1")
    print("\n  32/64-bit values (byte_order/word_order; <= your connection's setting):")
    found = interpretations(start, regs)
    for addr in range(start, start + len(regs)):
        rows = [x for x in found if x.address == addr and x.data_type in
                (DataType.FLOAT32, DataType.INT32, DataType.UINT32, DataType.FLOAT64)]
        if not rows:
            continue
        for bo, wo in _ORDERS:
            cells = [f"{x.data_type}={fmt(x.value)}" for x in rows
                     if (x.byte_order, x.word_order) == (bo, wo)]
            mark = "  <=" if (bo, wo) == (cfg_bo, cfg_wo) else ""
            print(f"  {addr:>5}  {bo:>6}/{wo:<6}  {'  '.join(cells)}{mark}")
    if args.expect is None:
        return EXIT_OK
    matches = []
    for x in found:
        s = scale_for(float(x.value), args.expect, args.tolerance / 100)
        if s is not None and plausible(float(x.value)):
            matches.append((x, s))
    matches.sort(key=lambda m: ((m[0].byte_order, m[0].word_order) != (cfg_bo, cfg_wo)
                                and m[0].data_type not in (DataType.UINT16, DataType.INT16),
                                m[1] != 1, m[0].address != address))
    if not matches:
        print(f"\n  no interpretation gives {args.expect} (+/-{args.tolerance}%): read the "
              "display again, widen --count, or try the other register type")
        return EXIT_PROBLEM
    print(f"\n  interpretations giving {args.expect} (+/-{args.tolerance}%):")
    for x, s in matches[:8]:
        shift = "  (datasheet counts from 1)" if x.address == address - 1 else ""
        print(f"    address {x.address} {x.data_type} byte_order={x.byte_order} "
              f"word_order={x.word_order} scale={s:g}  raw={fmt(x.value)}{shift}")
    best, s = matches[0]
    tag: dict[str, Any] = {"name": "value", "register_type": register, "address": best.address,
                           "data_type": str(best.data_type)}
    if s != 1:
        tag["scale"] = s
    print("\n  suggested tag (rename it; byte_order/word_order belong on the connection):")
    print("    " + dump_yaml([tag]).replace("\n", "\n    ").rstrip())
    if best.data_type not in (DataType.UINT16, DataType.INT16):
        print(f"    connection: byte_order: {best.byte_order}, word_order: {best.word_order}")
    return EXIT_OK


def parse_units(text: str) -> list[int]:
    units: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            units += range(a, b + 1)
        elif part:
            units.append(int(part))
    if not units or any(not 0 <= u <= 255 for u in units):
        raise UsageError(f"--units {text!r}: give unit ids 0..255, e.g. 1-32 or 1,2,10")
    return units


async def cmd_modbus_scan(args: argparse.Namespace) -> int:
    client, _, where, _ = await _modbus_client(args)
    found = []
    try:
        print(f"scanning unit ids at {where} ({args.register} {args.address}) ...")
        for unit in parse_units(args.units):
            data = await _modbus_read(client, args.register, args.address, 1, unit)
            if isinstance(data, str):
                if data.startswith("exception") and "gateway" not in data:
                    found.append(unit)
                    print(f"  unit {unit:>3}: answers, {data} (the unit exists)")
                continue
            found.append(unit)
            print(f"  unit {unit:>3}: answers, value {data[0]}")
    finally:
        client.close()
    print(f"{len(found)} unit(s) answered: {', '.join(map(str, found)) or 'none'}")
    return EXIT_OK if found else EXIT_PROBLEM


# --------------------------------------------------------------------------------------------
# OPC UA
# --------------------------------------------------------------------------------------------


def _opcua_type(variant: Any) -> DataType | None:
    from asyncua import ua

    v = ua.VariantType
    return {v.Boolean: DataType.BOOL, v.SByte: DataType.INT16, v.Int16: DataType.INT16,
            v.Byte: DataType.UINT16, v.UInt16: DataType.UINT16, v.Int32: DataType.INT32,
            v.UInt32: DataType.UINT32, v.Int64: DataType.INT64, v.UInt64: DataType.UINT64,
            v.Float: DataType.FLOAT32, v.Double: DataType.FLOAT64, v.String: DataType.STRING,
            v.LocalizedText: DataType.STRING}.get(variant)


async def cmd_opcua_browse(args: argparse.Namespace) -> int:
    from asyncua import ua

    from hub.drivers.opcua import open_client

    if args.device:
        conn = configured_device(args.config_dir, args.device, (OpcUaDevice,)).connection
    elif args.endpoint:
        conn = OpcUaConnection.model_validate({"endpoint_url": args.endpoint,
                                               "security_policy": "None",
                                               "security_mode": "None"})
    else:
        raise UsageError("give --endpoint (anonymous, no security) or --device")
    client = await open_client(conn)
    suggestions: list[dict[str, Any]] = []
    taken: set[str] = set()
    seen = 0
    try:
        root = client.get_node(args.node)

        async def walk(node: Any, depth: int, trail: str) -> None:
            nonlocal seen
            if seen >= args.max:
                return
            for child in await node.get_children():
                if seen >= args.max:
                    print(f"... stopped after {args.max} nodes (--max)")
                    return
                seen += 1
                nid = child.nodeid
                if nid.NamespaceIndex == 0 and not args.all:
                    continue  # the server's own objects
                name = (await child.read_browse_name()).Name
                cls = await child.read_node_class()
                indent = "  " * depth
                if cls == ua.NodeClass.Variable:
                    try:
                        vt = await child.read_data_type_as_variant_type()
                        value = await child.read_value()
                        access = await child.get_access_level()
                    except Exception as exc:  # noqa: BLE001 - shown, the browse goes on
                        print(f"{indent}{name}  {nid.to_string()}  (unreadable: {exc})")
                        continue
                    rw = "rw" if ua.AccessLevel.CurrentWrite in access else "r"
                    print(f"{indent}{name}  {nid.to_string()}  {vt.name} = {fmt(value)}  {rw}")
                    dt = _opcua_type(vt)
                    if dt is not None:
                        suggestions.append({"name": tag_name(f"{trail}_{name}".strip("_")
                                                             if args.prefix else name, taken),
                                            "node_id": nid.to_string(), "data_type": str(dt)})
                elif cls == ua.NodeClass.Object:
                    print(f"{indent}{name}/  {nid.to_string()}")
                    if depth + 1 < args.depth:
                        await walk(child, depth + 1, f"{trail}_{name}")

        await walk(root, 0, "")
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    print(f"{len(suggestions)} variable(s) with a supported data type")
    if args.yaml and suggestions:
        print("\n# paste under the device's tags (check names, units, scale):")
        print(dump_yaml(suggestions))
    return EXIT_OK if suggestions else EXIT_PROBLEM


# --------------------------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------------------------


async def cmd_mqtt_listen(args: argparse.Namespace) -> int:
    import aiomqtt

    from hub.drivers.mqtt_json import client_kwargs
    from hub.models import Broker

    topic = args.topic
    if args.device or args.broker:
        report = load_report(args.config_dir)
        if args.device:
            device = configured_device(args.config_dir, args.device, (MqttDevice,))
            broker = report.brokers[device.connection.broker_ref]
            topic = topic or device.connection.telemetry_topic
        else:
            if args.broker not in report.brokers:
                raise UsageError(f"no broker {args.broker!r} in brokers.yaml")
            broker = report.brokers[args.broker]
    elif args.host:
        broker = Broker(broker_id="commission", host=args.host, port=args.port,
                        username_env=args.username_env, password_env=args.password_env)
    else:
        raise UsageError("give --broker, --device or --host")
    topic = topic or "#"
    kwargs = client_kwargs(broker, f"hub-commission-{os.getpid()}")
    messages: dict[str, tuple[int, bytes]] = {}
    print(f"listening on {broker.host}:{broker.port} to {topic!r} for {args.seconds:g} s ...")
    try:
        async with aiomqtt.Client(**kwargs) as client:  # type: ignore[arg-type]
            await client.subscribe(topic, qos=0)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(args.seconds):
                    async for message in client.messages:
                        name = str(message.topic)
                        n = messages.get(name, (0, b""))[0]
                        payload = message.payload if isinstance(message.payload, bytes) else \
                            str(message.payload).encode()
                        messages[name] = (n + 1, payload)
    except aiomqtt.MqttError as exc:
        raise DriverError(f"broker {broker.host}:{broker.port}: {exc}") from exc
    if not messages:
        print("no messages: check the topic (case matters), the broker account's ACL, and that "
              "the device publishes (some publish only on change)")
        return EXIT_PROBLEM
    for name, (count, payload) in sorted(messages.items()):
        rate = count / args.seconds
        print(f"\n{name}  ({count} message(s), {rate:.2g}/s)")
        try:
            doc = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            print(f"  not JSON: {payload[:80]!r}")
            continue
        taken: set[str] = set()
        tags = []
        for path, value in flatten(doc):
            print(f"  {path:<40} {fmt(value)}")
            dt = json_data_type(value)
            if dt is not None and dt is not DataType.STRING:
                tags.append({"name": tag_name(path_label(path), taken), "json_path": path,
                             "data_type": str(dt)})
        if args.yaml and tags:
            print(f"\n# telemetry_topic: {name}")
            print(dump_yaml(tags))
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# SNMP
# --------------------------------------------------------------------------------------------

# A few well-known OIDs, for readable suggestions (MIB files are not loaded)
_KNOWN_OIDS = {
    "1.3.6.1.2.1.1.1.0": "sys_descr", "1.3.6.1.2.1.1.3.0": "sys_uptime",
    "1.3.6.1.2.1.1.5.0": "sys_name", "1.3.6.1.2.1.1.6.0": "sys_location",
    "1.3.6.1.2.1.33.1.2.1.0": "battery_status", "1.3.6.1.2.1.33.1.2.2.0": "seconds_on_battery",
    "1.3.6.1.2.1.33.1.2.3.0": "minutes_remaining", "1.3.6.1.2.1.33.1.2.4.0": "charge_remaining",
    "1.3.6.1.2.1.33.1.2.5.0": "battery_voltage", "1.3.6.1.2.1.33.1.2.7.0": "battery_temperature",
    "1.3.6.1.2.1.33.1.3.3.1.3.1": "input_voltage", "1.3.6.1.2.1.33.1.4.1.0": "output_source",
    "1.3.6.1.2.1.33.1.4.4.1.2.1": "output_voltage", "1.3.6.1.2.1.33.1.4.4.1.5.1": "output_load",
}
_SNMP_TYPES = {"Integer": DataType.INT32, "Integer32": DataType.INT32,
               "Counter32": DataType.UINT32, "Gauge32": DataType.UINT32,
               "Unsigned32": DataType.UINT32, "TimeTicks": DataType.UINT32,
               "Counter64": DataType.UINT64, "OctetString": DataType.STRING}


def _oid_key(oid: str) -> tuple[int, ...]:
    return tuple(int(p) for p in oid.split(".") if p.isdigit())


async def cmd_snmp_walk(args: argparse.Namespace) -> int:
    from pysnmp.hlapi.v3arch import asyncio as snmp

    from hub.drivers.snmp import credentials

    if args.device:
        conn: SnmpConnection = configured_device(args.config_dir, args.device,
                                                 (SnmpDevice,)).connection
    elif args.host and args.community_env:
        conn = SnmpConnection.model_validate({
            "host": args.host, "port": args.port,
            "security": {"version": "2c", "community_env": args.community_env}})
    else:
        raise UsageError("give --device, or --host with --community-env (the name of the "
                         "environment variable holding the community)")
    engine = snmp.SnmpEngine()
    rows: list[tuple[str, str, Any]] = []
    try:
        target = await snmp.UdpTransportTarget.create((conn.host, conn.port),
                                                      timeout=conn.timeout_ms / 1000,
                                                      retries=conn.retries)
        walker = snmp.walk_cmd(engine, credentials(conn.security), target, snmp.ContextData(),
                               snmp.ObjectType(snmp.ObjectIdentity(args.oid)),
                               lexicographicMode=False, lookupMib=False)
        async for err_ind, err_status, _, var_binds in walker:
            if err_ind:
                raise DriverError(f"{conn.host}:{conn.port}: {err_ind}")
            if err_status:
                raise DriverError(f"{conn.host}:{conn.port}: {err_status.prettyPrint()}")
            done = False
            for oid, value in var_binds:
                kind = type(value).__name__
                # end of the subtree; an agent that does not advance would loop forever
                if kind in ("EndOfMibView", "NoSuchObject", "NoSuchInstance") or \
                        rows and _oid_key(str(oid)) <= _oid_key(rows[-1][0]):
                    done = True
                    break
                rows.append((str(oid), kind, value))
            if done:
                break
            if len(rows) >= args.max:
                print(f"... stopped after {args.max} OIDs (--max)")
                break
    finally:
        engine.close_dispatcher()
    if not rows:
        print(f"nothing under {args.oid}: wrong community/user, an ACL on the agent, or the "
              "OID subtree is not implemented")
        return EXIT_PROBLEM
    taken: set[str] = set()
    tags = []
    for oid, kind, value in rows:
        text = value.prettyPrint()
        print(f"  {oid:<36} {kind:<12} {text[:60]}")
        dt = _SNMP_TYPES.get(kind)
        if dt is not None:
            label = _KNOWN_OIDS.get(oid) or "oid_" + "_".join(oid.split(".")[-3:])
            tags.append({"name": tag_name(label, taken), "oid": oid, "data_type": str(dt)})
    print(f"{len(rows)} OID(s)")
    if args.yaml and tags:
        print("\n# paste under the device's tags (rename, add units/scale):")
        print(dump_yaml(tags))
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# Siemens S7
# --------------------------------------------------------------------------------------------

def s7_address(area: str, db: int, offset: int, size: str) -> str:
    if area == "DB":
        return f"DB{db}.DB{size}{offset}"
    return f"{area}{size}{offset}"


def s7_interpretations(data: bytes, start: int) -> list[tuple[int, str, DataType, float]]:
    out: list[tuple[int, str, DataType, float]] = []
    for i in range(0, len(data) - 1, 2):
        (w,) = struct.unpack_from(">H", data, i)
        (iw,) = struct.unpack_from(">h", data, i)
        out += [(start + i, "W", DataType.UINT16, w), (start + i, "W", DataType.INT16, iw)]
        if i + 4 <= len(data):
            (f,) = struct.unpack_from(">f", data, i)
            (d,) = struct.unpack_from(">i", data, i)
            out += [(start + i, "D", DataType.FLOAT32, f), (start + i, "D", DataType.INT32, d)]
    return out


async def cmd_s7_dump(args: argparse.Namespace) -> int:
    from snap7.async_client import AsyncClient

    from hub.drivers.s7 import AREAS
    from hub.models import S7Area

    if args.device:
        c = configured_device(args.config_dir, args.device, (S7Device,)).connection
        host, port, rack, slot = c.host, c.port, c.rack, c.slot
    elif args.host:
        host, port, rack, slot = args.host, args.port, args.rack, args.slot
    else:
        raise UsageError("give --host (or --device)")
    area = args.area.upper()
    s7_area = {"DB": S7Area.DB, "M": S7Area.MERKER, "I": S7Area.INPUT, "Q": S7Area.OUTPUT}[area]
    client = AsyncClient()
    try:
        await asyncio.wait_for(client.connect(host, rack, slot, tcp_port=port),
                               args.timeout_ms / 1000)
        data = bytes(await asyncio.wait_for(
            client.read_area(AREAS[s7_area], args.db if area == "DB" else 0, args.start,
                             args.size), args.timeout_ms / 1000))
    except Exception as exc:  # noqa: BLE001 - every failure gets the same advice
        print(f"{host}:{port} rack {rack} slot {slot}: {exc}")
        print("  -> check IP, rack/slot (S7-1200/1500: 0/1, S7-300: 0/2), PUT/GET allowed, "
              "DB exists with 'optimized block access' off, and the DB is long enough")
        return EXIT_PROBLEM
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    where = f"DB{args.db}" if area == "DB" else area
    print(f"{where} bytes {args.start}..{args.start + len(data) - 1} at {host} "
          f"(rack {rack}, slot {slot})")
    for row in range(0, len(data), 16):
        chunk = data[row:row + 16]
        hexes = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {args.start + row:>5}  {hexes:<47}  {text}")
    found = s7_interpretations(data, args.start)
    print("\n  offset   INT      WORD    DINT          REAL")
    for off in range(args.start, args.start + len(data) - 1, 2):
        vals = {(sz, dt): v for o, sz, dt, v in found if o == off}
        dint = vals.get(("D", DataType.INT32))
        real = vals.get(("D", DataType.FLOAT32))
        print(f"  {off:>5}  {vals[('W', DataType.INT16)]:>7}  {vals[('W', DataType.UINT16)]:>6}"
              f"  {'' if dint is None else dint:>12}  {'' if real is None else fmt(real)}")
    if args.expect is None:
        return EXIT_OK
    matches = []
    for off, size, dt, v in found:
        s = scale_for(float(v), args.expect, args.tolerance / 100)
        if s is not None and plausible(float(v)):
            matches.append((off, size, dt, v, s))
    matches.sort(key=lambda m: (m[4] != 1, m[2] is not DataType.FLOAT32))
    if not matches:
        print(f"\n  no value gives {args.expect} (+/-{args.tolerance}%)")
        return EXIT_PROBLEM
    print(f"\n  values giving {args.expect} (+/-{args.tolerance}%):")
    for off, size, dt, v, s in matches[:8]:
        print(f"    {s7_address(area, args.db, off, size)} {dt} scale={s:g} raw={fmt(v)}")
    off, size, dt, _, s = matches[0]
    tag: dict[str, Any] = {"name": "value", "address": s7_address(area, args.db, off, size),
                           "data_type": str(dt)}
    if s != 1:
        tag["scale"] = s
    print("\n  suggested tag (rename it):")
    print("    " + dump_yaml([tag]).replace("\n", "\n    ").rstrip())
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# HTTP / REST JSON
# --------------------------------------------------------------------------------------------


async def cmd_http_get(args: argparse.Namespace) -> int:
    from hub.drivers.http_json import build_client

    if args.device:
        device = configured_device(args.config_dir, args.device, (HttpDevice,))
        conn: HttpConnection = device.connection
        # without --path: every endpoint the device's tags read
        paths = [args.path] if args.path else sorted({t.path for t in device.tags}) or [""]
    elif args.url:
        parts = urlsplit(args.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise UsageError(f"--url {args.url!r}: give http(s)://host[:port]/path")
        auth: dict[str, Any] = {"type": "none"}
        if args.bearer_env:
            auth = {"type": "bearer", "token_env": args.bearer_env}
        conn = HttpConnection.model_validate({
            "base_url": f"{parts.scheme}://{parts.netloc}", "auth": auth,
            "verify_tls": not args.insecure})
        paths = [args.path or (parts.path or "/") + (f"?{parts.query}" if parts.query else "")]
    else:
        raise UsageError("give --url or --device")
    taken: set[str] = set()
    tags: list[dict[str, Any]] = []
    worst = EXIT_OK
    async with build_client(conn) as client:
        for path in paths:
            worst = max(worst, await _http_one(client, conn, path, taken, tags))
    if args.yaml and tags:
        print("\n# paste under the device's tags:")
        print(dump_yaml(tags))
    return worst


async def _http_one(client: Any, conn: HttpConnection, path: str, taken: set[str],
                    tags: list[dict[str, Any]]) -> int:
    import httpx

    try:
        response = await client.get(path or "/")
    except httpx.HTTPError as exc:
        print(f"{conn.base_url}{path}: {exc}")
        return EXIT_PROBLEM
    print(f"GET {conn.base_url}{path or '/'} -> {response.status_code} "
          f"{response.headers.get('content-type', '')}")
    if response.status_code in (401, 403):
        print("  -> the device wants credentials: check auth and the *_env variables")
        return EXIT_PROBLEM
    if response.status_code >= 400:
        return EXIT_PROBLEM
    try:
        doc = response.json()
    except ValueError:
        print(f"  not JSON: {response.text[:120]!r}")
        return EXIT_PROBLEM
    for json_path, value in flatten(doc):
        print(f"  {json_path:<40} {fmt(value)}")
        dt = json_data_type(value)
        if dt is not None and dt is not DataType.STRING:
            tag: dict[str, Any] = {"name": tag_name(path_label(json_path), taken)}
            if path and path != "/":
                tag["path"] = path.split("?")[0]
            tag |= {"json_path": json_path, "data_type": str(dt)}
            tags.append(tag)
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hub-commission", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Every command is read-only. Exit code 0 = ok, "
                                       "1 = problem found, 2 = usage or configuration error.")
    p.add_argument("--config-dir", type=Path, default=Path("config"))
    p.add_argument("--env-file", type=Path, action="append", default=[],
                   help="load KEY=value lines (credentials) before running; repeatable")
    p.add_argument("--log-level", default="error",
                   help="driver messages (default: errors only; results are printed anyway)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("read", help="read configured devices through the hub's drivers")
    s.add_argument("device", nargs="*", help="device ids (default: all)")
    s.add_argument("--seconds", type=float, default=10.0)
    s.add_argument("--out", type=Path, help="write a report: .md (SAT sheet) or .json")
    s.set_defaults(func=cmd_read)

    def modbus_target(s: argparse.ArgumentParser) -> None:
        s.add_argument("--device", help="take host/port/unit/orders from a configured device")
        s.add_argument("--host")
        s.add_argument("--port", type=int, default=502)
        s.add_argument("--unit", type=int, help="unit id (slave id); default 1")
        s.add_argument("--framer", choices=["socket", "rtu"], default="socket",
                       help="rtu = RTU frames over TCP (serial gateways in transparent mode)")
        s.add_argument("--timeout-ms", type=int, default=1000)

    s = sub.add_parser("modbus-probe", help="show registers in every data type and word order")
    modbus_target(s)
    s.add_argument("--register", choices=["holding", "input", "coil", "discrete_input"],
                   default="holding")
    s.add_argument("--address", type=int, help="0-based protocol address")
    s.add_argument("--ref", type=int, help="datasheet reference instead, e.g. 40101 or 30001")
    s.add_argument("--count", type=int, default=4)
    s.add_argument("--expect", type=float, help="the value the device's display shows now")
    s.add_argument("--tolerance", type=float, default=1.0, help="percent (default 1)")
    s.set_defaults(func=cmd_modbus_probe)

    s = sub.add_parser("modbus-scan", help="find the unit ids that answer (gateways, RS-485)")
    modbus_target(s)
    s.add_argument("--units", default="1-247")
    s.add_argument("--register", choices=["holding", "input", "coil", "discrete_input"],
                   default="holding")
    s.add_argument("--address", type=int, default=0)
    s.set_defaults(func=cmd_modbus_scan, timeout_ms=300)

    s = sub.add_parser("opcua-browse", help="list an OPC UA server's variables")
    s.add_argument("--device")
    s.add_argument("--endpoint", help="opc.tcp://host:4840 (anonymous, no security)")
    s.add_argument("--node", default="i=85", help="start node (default: Objects)")
    s.add_argument("--depth", type=int, default=4)
    s.add_argument("--max", type=int, default=500)
    s.add_argument("--all", action="store_true", help="include namespace 0 (server objects)")
    s.add_argument("--prefix", action="store_true",
                   help="suggest names with the folder path (pump1_speed instead of speed)")
    s.add_argument("--yaml", action="store_true", help="print tag suggestions")
    s.set_defaults(func=cmd_opcua_browse)

    s = sub.add_parser("mqtt-listen", help="show topics and JSON fields on a broker")
    s.add_argument("--device")
    s.add_argument("--broker", help="broker_id from brokers.yaml")
    s.add_argument("--host")
    s.add_argument("--port", type=int, default=1883)
    s.add_argument("--username-env")
    s.add_argument("--password-env")
    s.add_argument("--topic", help="default: the device's telemetry_topic, else #")
    s.add_argument("--seconds", type=float, default=10.0)
    s.add_argument("--yaml", action="store_true")
    s.set_defaults(func=cmd_mqtt_listen)

    s = sub.add_parser("snmp-walk", help="walk an SNMP subtree")
    s.add_argument("--device")
    s.add_argument("--host")
    s.add_argument("--port", type=int, default=161)
    s.add_argument("--community-env", help="name of the variable holding the v2c community")
    s.add_argument("--oid", default="1.3.6.1.2.1.1", help="subtree (default: system group)")
    s.add_argument("--max", type=int, default=300)
    s.add_argument("--yaml", action="store_true")
    s.set_defaults(func=cmd_snmp_walk)

    s = sub.add_parser("s7-dump", help="hex dump and values of an S7 data block / area")
    s.add_argument("--device")
    s.add_argument("--host")
    s.add_argument("--port", type=int, default=102)
    s.add_argument("--rack", type=int, default=0)
    s.add_argument("--slot", type=int, default=1)
    s.add_argument("--area", choices=["DB", "M", "I", "Q", "db", "m", "i", "q"], default="DB")
    s.add_argument("--db", type=int, default=1)
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--size", type=int, default=32)
    s.add_argument("--expect", type=float)
    s.add_argument("--tolerance", type=float, default=1.0)
    s.add_argument("--timeout-ms", type=int, default=3000)
    s.set_defaults(func=cmd_s7_dump)

    s = sub.add_parser("http-get", help="fetch a JSON endpoint and list its fields")
    s.add_argument("--device")
    s.add_argument("--url")
    s.add_argument("--path", help="path under the device's base_url, e.g. /api/status")
    s.add_argument("--bearer-env", help="variable holding a bearer token (with --url)")
    s.add_argument("--insecure", action="store_true",
                   help="do not verify the TLS certificate (with --url; testing only)")
    s.add_argument("--yaml", action="store_true")
    s.set_defaults(func=cmd_http_get)
    return p


def _load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # Windows consoles: never fail on a character
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for name in ("asyncua", "asyncua.client.client"):
        logging.getLogger(name).setLevel(max(logging.ERROR, logging.getLogger().level))
    if args.command == "s7-dump" and args.size > 512:
        print("--size: at most 512 bytes", file=sys.stderr)
        return EXIT_USAGE
    for env_file in args.env_file:
        if not env_file.is_file():
            print(f"--env-file {env_file}: no such file", file=sys.stderr)
            return EXIT_USAGE
        _load_env(env_file)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            return run(args.func(args))
        return EXIT_PROBLEM
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (DriverError, OSError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PROBLEM


if __name__ == "__main__":
    sys.exit(main())
