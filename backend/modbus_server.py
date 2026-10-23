"""Modbus TCP server: tag values for SCADA, HMIs and PLCs that only speak Modbus.

config/modbus_server.yaml maps tags (or their quality) to registers and coils. Registers are
encoded from the hub's latest values at the moment of each request (no copy that could go
stale); a value the hub has never had reads as 0, a value that turned bad keeps its last good
reading — map ``source: quality`` next to it so the client can tell.

Modbus has no authentication, so this server is read-only unless ``writes`` is configured:

* only clients from ``writes.allowed_clients`` may write (others get exception 01);
* only ``writable`` points, as a whole (a multi-register value in one request);
* every write goes through ``ControlService.request_write`` as the hub user ``writes.as_user``
  (RBAC, enablement, range, rate, interlocks, audit), exactly like a dashboard command.

Answers: applied -> normal response; waiting for a second person -> exception 05
(acknowledge); refused or failed -> exception 04. Unknown addresses -> exception 02.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import struct
from collections import Counter
from dataclasses import dataclass
from typing import Any

from hub.codec import CodecError, Value, decode_registers, encode_registers
from hub.models import (
    BIT_REGISTER_TYPES,
    INTEGER_RANGES,
    MODBUS_WIDTH,
    QUALITY_CODES,
    DataType,
    ModbusServerFile,
    ModbusServerPoint,
    Quality,
    RegisterType,
)

log = logging.getLogger(__name__)

ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, ILLEGAL_VALUE, DEVICE_FAILURE, ACKNOWLEDGE = 1, 2, 3, 4, 5
_AREA_BY_FC = {1: RegisterType.COIL, 2: RegisterType.DISCRETE_INPUT,
               3: RegisterType.HOLDING, 4: RegisterType.INPUT}


class _Exception(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Slot:
    point: ModbusServerPoint
    data_type: DataType
    index: int          # register index inside the point (0 for bits and 16-bit values)
    width: int


def _networks(items: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(n, strict=False) for n in items]


def _allowed(addr: str | None, nets: list[Any]) -> bool:
    if not nets:
        return True
    if addr is None:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in n for n in nets)


class HubModbusServer:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.config: ModbusServerFile = services.report.modbus_server
        self.server: asyncio.base_events.Server | None = None
        self.port: int | None = None
        self.counters: Counter[str] = Counter()
        self._clients: set[asyncio.StreamWriter] = set()
        self._map: dict[tuple[RegisterType, int], _Slot] = {}
        self._last_good: dict[str, list[int]] = {}
        self._read_nets: list[Any] = []
        self._write_nets: list[Any] = []

    # --- address map --------------------------------------------------------------------------

    def _data_type(self, point: ModbusServerPoint) -> DataType:
        if point.source == "quality":
            return DataType.UINT16
        if point.data_type is not None:
            return point.data_type
        found = self.services.hub.tags.get(point.tag)
        tag_type = found[1].data_type if found else DataType.FLOAT32
        if point.register_type in BIT_REGISTER_TYPES:
            return DataType.BOOL
        return DataType.FLOAT32 if tag_type in (DataType.BOOL, DataType.STRING) else tag_type

    def build(self) -> None:
        self.config = self.services.report.modbus_server
        self._map = {}
        for point in self.config.points:
            dtype = self._data_type(point)
            width = 1 if dtype is DataType.BOOL else MODBUS_WIDTH[dtype]
            for i in range(width):
                self._map[(point.register_type, point.address + i)] = _Slot(point, dtype, i, width)
        self._read_nets = _networks(self.config.allowed_clients)
        self._write_nets = _networks(self.config.writes.allowed_clients) if self.config.writes \
            else []

    def _registers(self, slot: _Slot) -> list[int]:
        """The registers of a whole point, from the hub's latest value."""
        point = slot.point
        key = f"{point.tag}:{point.source}"
        sample = self.services.hub.latest.get(point.tag)
        if point.source == "quality":
            return [QUALITY_CODES[sample.quality if sample is not None else Quality.BAD]]
        value = sample.value if sample is not None else None
        if value is None or isinstance(value, str):
            return self._last_good.get(key, [0] * slot.width)
        if slot.data_type is DataType.BOOL:
            regs = [1 if value else 0]
        else:
            raw: float | int = (float(value) if not isinstance(value, bool) else int(value)) \
                / point.scale
            if slot.data_type in INTEGER_RANGES:
                lo, hi = INTEGER_RANGES[slot.data_type]
                raw = min(max(round(raw), lo), hi)   # clamp: a register cannot overflow
            try:
                regs = encode_registers(raw, slot.data_type, self.config.byte_order,
                                        self.config.word_order)
            except CodecError:
                return self._last_good.get(key, [0] * slot.width)
        self._last_good[key] = regs
        return regs

    def read(self, area: RegisterType, address: int, count: int) -> list[int]:
        out = []
        cache: dict[int, list[int]] = {}
        for a in range(address, address + count):
            slot = self._map.get((area, a))
            if slot is None:
                raise _Exception(ILLEGAL_ADDRESS)
            if id(slot.point) not in cache:
                cache[id(slot.point)] = self._registers(slot)
            out.append(cache[id(slot.point)][slot.index])
        return out

    # --- writes -------------------------------------------------------------------------------

    async def write(self, area: RegisterType, address: int, values: list[int],
                    client: str | None) -> None:
        slot = self._map.get((area, address))
        if slot is None:
            raise _Exception(ILLEGAL_ADDRESS)
        point = slot.point
        if not point.writable or self.config.writes is None:
            self.counters["refused"] += 1
            raise _Exception(ILLEGAL_ADDRESS if not point.writable else ILLEGAL_FUNCTION)
        if not _allowed(client, self._write_nets):
            self.counters["refused"] += 1
            log.warning("Modbus server: write from %s refused (not in writes.allowed_clients)",
                        client)
            raise _Exception(ILLEGAL_FUNCTION)
        if slot.index != 0 or len(values) != slot.width:
            raise _Exception(ILLEGAL_VALUE)   # a value is written as a whole
        if slot.data_type is DataType.BOOL:
            value: Value = bool(values[0])
        else:
            try:
                raw = decode_registers(values, slot.data_type, self.config.byte_order,
                                       self.config.word_order)
            except CodecError as exc:
                raise _Exception(ILLEGAL_VALUE) from exc
            value = raw * point.scale  # type: ignore[operator]
            tag = self.services.hub.tags.get(point.tag)
            if tag is not None and tag[1].data_type in INTEGER_RANGES:
                value = round(value)
            elif isinstance(value, float) and value.is_integer() and point.scale == 1.0 \
                    and slot.data_type in INTEGER_RANGES:
                value = int(value)
        principal = await self._principal()
        if principal is None:
            self.counters["refused"] += 1
            raise _Exception(DEVICE_FAILURE)
        result = await self.services.control.request_write(
            principal, point.tag, value, f"Modbus server write from {client}", client)
        log.info("Modbus server write %s=%r from %s -> %s", point.tag, value, client,
                 result.result)
        if result.status == "applied":
            self.counters["applied"] += 1
            return
        if result.status == "pending_confirmation":
            self.counters["pending"] += 1
            raise _Exception(ACKNOWLEDGE)
        self.counters["refused"] += 1
        raise _Exception(DEVICE_FAILURE)

    async def _principal(self) -> Any:
        from backend.rbac import Principal

        store = self.services.store
        assert self.config.writes is not None
        account = await store.get_user_by_username(self.config.writes.as_user)
        if account is None or not account.is_active:
            log.error("Modbus server: writes.as_user %s does not exist or is disabled",
                      self.config.writes.as_user)
            return None
        return Principal(user=account, session_id=None,
                         grants=tuple(await store.grants(account.user_id)),
                         role_permissions=await store.role_permissions())

    # --- protocol -----------------------------------------------------------------------------

    async def _dispatch(self, fc: int, body: bytes, client: str | None) -> bytes:
        if fc in (1, 2):
            address, count = struct.unpack(">HH", body[:4])
            if not 1 <= count <= 2000:
                raise _Exception(ILLEGAL_VALUE)
            bits = self.read(_AREA_BY_FC[fc], address, count)
            packed = bytearray((count + 7) // 8)
            for i, b in enumerate(bits):
                if b:
                    packed[i // 8] |= 1 << (i % 8)
            return bytes([len(packed)]) + bytes(packed)
        if fc in (3, 4):
            address, count = struct.unpack(">HH", body[:4])
            if not 1 <= count <= 125:
                raise _Exception(ILLEGAL_VALUE)
            regs = self.read(_AREA_BY_FC[fc], address, count)
            return bytes([count * 2]) + struct.pack(f">{count}H", *regs)
        if fc == 5:
            address, raw = struct.unpack(">HH", body[:4])
            if raw not in (0x0000, 0xFF00):
                raise _Exception(ILLEGAL_VALUE)
            await self.write(RegisterType.COIL, address, [1 if raw else 0], client)
            return body[:4]
        if fc == 6:
            address, value = struct.unpack(">HH", body[:4])
            await self.write(RegisterType.HOLDING, address, [value], client)
            return body[:4]
        if fc == 15:
            address, count, nbytes = struct.unpack(">HHB", body[:5])
            data = body[5 : 5 + nbytes]
            if count != 1:
                raise _Exception(ILLEGAL_VALUE)   # coils are separate commands
            await self.write(RegisterType.COIL, address, [data[0] & 1], client)
            return body[:4]
        if fc == 16:
            address, count, _ = struct.unpack(">HHB", body[:5])
            if not 1 <= count <= 123:
                raise _Exception(ILLEGAL_VALUE)
            values = list(struct.unpack(f">{count}H", body[5 : 5 + count * 2]))
            await self.write(RegisterType.HOLDING, address, values, client)
            return body[:4]
        raise _Exception(ILLEGAL_FUNCTION)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = peer[0] if peer else None
        if not _allowed(client, self._read_nets) or len(self._clients) >= self.config.max_clients:
            self.counters["connections_refused"] += 1
            writer.close()
            return
        self._clients.add(writer)
        units = {self.config.unit_id, 0, 255}
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                if not 2 <= length <= 254:
                    break
                pdu = await reader.readexactly(length - 1)
                if pid != 0 or unit not in units:
                    continue
                fc = pdu[0]
                try:
                    response = bytes([fc]) + await self._dispatch(fc, pdu[1:], client)
                    self.counters["requests"] += 1
                except _Exception as exc:
                    response = bytes([fc | 0x80, exc.code])
                    self.counters[f"exception_{exc.code}"] += 1
                except (struct.error, IndexError):
                    response = bytes([fc | 0x80, ILLEGAL_VALUE])
                writer.write(struct.pack(">HHHB", tid, 0, len(response) + 1, unit) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        self.build()
        if not self.config.enabled or not self.config.points:
            return
        self.server = await asyncio.start_server(self._handle, self.config.host, self.config.port)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("Modbus server on %s:%d (%d points, writes %s)", self.config.host, self.port,
                 len(self.config.points), "on" if self.config.writes else "off")

    async def stop(self) -> None:
        for writer in list(self._clients):
            writer.close()
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 2)
            self.server = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def describe(self) -> dict[str, Any]:
        return {"enabled": self.server is not None, "host": self.config.host, "port": self.port,
                "points": len(self.config.points), "writes": self.config.writes is not None,
                "clients": len(self._clients), "counters": dict(self.counters)}
