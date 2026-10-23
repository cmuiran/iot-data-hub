"""A small, self-contained Modbus TCP server for tests and local development.

Written from the Modbus spec (MBAP + function codes 1, 2, 3, 4, 5, 6, 15, 16) instead of using
pymodbus' server, so the hub's driver (pymodbus client) is checked against an independent
implementation. Several unit ids can share one port, like a serial gateway.

Memory is sparse: only addresses that a device config defines exist; anything else answers
exception 02 (illegal data address), as many real devices do.

Fault injection: ``offline`` (unit stops answering), ``delay_s`` (slow answers),
``exception_addresses`` (force exception 04 for a register).

``framer="rtu"`` speaks Modbus RTU frames (unit + PDU + CRC-16) over the TCP socket, i.e. what an
RS-485 line looks like through a transparent serial server. The hub's serial client reaches it
with the port ``socket://127.0.0.1:<port>``, which exercises the real RTU code path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import struct
from dataclasses import dataclass, field

from hub.codec import Value, decode_bit, encode_bcd, encode_registers, to_raw
from hub.models import DataType, ModbusDevice, ModbusTag, RegisterType

log = logging.getLogger(__name__)

ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
SERVER_DEVICE_FAILURE = 0x04

_AREA_BY_FC = {1: RegisterType.COIL, 2: RegisterType.DISCRETE_INPUT,
               3: RegisterType.HOLDING, 4: RegisterType.INPUT}


@dataclass
class UnitMemory:
    """One Modbus unit (slave)."""

    areas: dict[RegisterType, dict[int, int]] = field(
        default_factory=lambda: {t: {} for t in RegisterType}
    )
    offline: bool = False
    delay_s: float = 0.0
    exception_addresses: set[tuple[RegisterType, int]] = field(default_factory=set)
    writes: list[tuple[RegisterType, int, list[int]]] = field(default_factory=list)

    def read(self, area: RegisterType, address: int, count: int) -> list[int]:
        mem = self.areas[area]
        out = []
        for a in range(address, address + count):
            if (area, a) in self.exception_addresses:
                raise _ModbusException(SERVER_DEVICE_FAILURE)
            if a not in mem:
                raise _ModbusException(ILLEGAL_DATA_ADDRESS)
            out.append(mem[a])
        return out

    def write(self, area: RegisterType, address: int, values: list[int]) -> None:
        mem = self.areas[area]
        for a in range(address, address + len(values)):
            if (area, a) in self.exception_addresses:
                raise _ModbusException(SERVER_DEVICE_FAILURE)
            if a not in mem:
                raise _ModbusException(ILLEGAL_DATA_ADDRESS)
        for i, v in enumerate(values):
            mem[address + i] = v
        self.writes.append((area, address, list(values)))


class _ModbusException(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def crc16(data: bytes) -> int:
    """Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF); sent low byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class ModbusSimServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, framer: str = "socket") -> None:
        self.host = host
        self.port = port
        self.framer = framer
        self.crc_errors = 0
        self.units: dict[int, UnitMemory] = {}
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for writer in list(self._clients):
            writer.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None

    async def __aenter__(self) -> ModbusSimServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def drop_connections(self) -> None:
        """Simulate a network cut: close every client socket."""
        for writer in list(self._clients):
            writer.close()

    def unit(self, unit_id: int) -> UnitMemory:
        return self.units.setdefault(unit_id, UnitMemory())

    # --- protocol -----------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.framer == "rtu":
            await self._handle_rtu(reader, writer)
            return
        self._clients.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, unit_id = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                unit = self.units.get(unit_id)
                if pid != 0 or unit is None or unit.offline:
                    continue  # no answer: the client times out, like a dead slave behind a gateway
                if unit.delay_s:
                    await asyncio.sleep(unit.delay_s)
                response = self._process(unit, pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(response) + 1, unit_id) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _handle_rtu(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            while True:
                head = await reader.readexactly(2)
                unit_id, fc = head
                if fc in (15, 16):
                    rest = await reader.readexactly(5)
                    rest += await reader.readexactly(rest[4] + 2)
                else:
                    rest = await reader.readexactly(6)  # 4 data bytes + CRC
                frame = head + rest
                body, crc = frame[:-2], int.from_bytes(frame[-2:], "little")
                if crc16(body) != crc:
                    self.crc_errors += 1
                    continue  # a real slave ignores a corrupt frame
                unit = self.units.get(unit_id)
                if unit is None or unit.offline:
                    continue
                if unit.delay_s:
                    await asyncio.sleep(unit.delay_s)
                response = bytes([unit_id]) + self._process(unit, body[1:])
                writer.write(response + crc16(response).to_bytes(2, "little"))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    def _process(self, unit: UnitMemory, pdu: bytes) -> bytes:
        fc = pdu[0]
        try:
            return bytes([fc]) + self._dispatch(unit, fc, pdu[1:])
        except _ModbusException as exc:
            return bytes([fc | 0x80, exc.code])
        except (struct.error, IndexError):
            return bytes([fc | 0x80, ILLEGAL_DATA_VALUE])

    def _dispatch(self, unit: UnitMemory, fc: int, body: bytes) -> bytes:
        if fc in (1, 2):
            address, count = struct.unpack(">HH", body[:4])
            if not 1 <= count <= 2000:
                raise _ModbusException(ILLEGAL_DATA_VALUE)
            bits = unit.read(_AREA_BY_FC[fc], address, count)
            packed = bytearray((count + 7) // 8)
            for i, b in enumerate(bits):
                if b:
                    packed[i // 8] |= 1 << (i % 8)
            return bytes([len(packed)]) + bytes(packed)
        if fc in (3, 4):
            address, count = struct.unpack(">HH", body[:4])
            if not 1 <= count <= 125:
                raise _ModbusException(ILLEGAL_DATA_VALUE)
            regs = unit.read(_AREA_BY_FC[fc], address, count)
            return bytes([count * 2]) + struct.pack(f">{count}H", *regs)
        if fc == 5:
            address, raw = struct.unpack(">HH", body[:4])
            if raw not in (0x0000, 0xFF00):
                raise _ModbusException(ILLEGAL_DATA_VALUE)
            unit.write(RegisterType.COIL, address, [1 if raw else 0])
            return body[:4]
        if fc == 6:
            address, value = struct.unpack(">HH", body[:4])
            unit.write(RegisterType.HOLDING, address, [value])
            return body[:4]
        if fc == 15:
            address, count, nbytes = struct.unpack(">HHB", body[:5])
            data = body[5 : 5 + nbytes]
            bits = [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]
            unit.write(RegisterType.COIL, address, bits)
            return body[:4]
        if fc == 16:
            address, count, _nbytes = struct.unpack(">HHB", body[:5])
            if not 1 <= count <= 123:
                raise _ModbusException(ILLEGAL_DATA_VALUE)
            values = list(struct.unpack(f">{count}H", body[5 : 5 + count * 2]))
            unit.write(RegisterType.HOLDING, address, values)
            return body[:4]
        raise _ModbusException(ILLEGAL_FUNCTION)


# --------------------------------------------------------------------------------------------
# Devices built from config
# --------------------------------------------------------------------------------------------


class SimulatedModbusDevice:
    """Puts a device's tags into a unit of a :class:`ModbusSimServer` and animates them."""

    def __init__(self, server: ModbusSimServer, device: ModbusDevice) -> None:
        self.server = server
        self.device = device
        self.unit = server.unit(device.connection.unit_id)
        self._tags = {t.name: t for t in device.tags}
        self._t = 0.0
        for tag in device.tags:
            self._allocate(tag)
            self.set(tag.name, _initial_value(tag))

    def _allocate(self, tag: ModbusTag) -> None:
        mem = self.unit.areas[tag.register_type]
        for a in range(tag.address, tag.last_address + 1):
            mem.setdefault(a, 0)

    def set(self, name: str, value: Value) -> None:
        """Store an engineering value into the tag's registers (or bit)."""
        tag = self._tags[name]
        conn = self.device.connection
        mem = self.unit.areas[tag.register_type]
        if tag.data_type is DataType.BOOL:
            if tag.bit is None:
                mem[tag.address] = 1 if value else 0
            else:
                word = mem[tag.address] & ~(1 << tag.bit)
                mem[tag.address] = word | ((1 if value else 0) << tag.bit)
            return
        if tag.bit_count is not None and tag.bit is not None:
            mask = ((1 << tag.bit_count) - 1) << tag.bit
            field_value = (int(value) << tag.bit) & mask
            mem[tag.address] = (mem[tag.address] & ~mask) | field_value
            return
        raw = value if tag.data_type is DataType.STRING else to_raw(tag, value)
        if tag.encoding == "bcd":
            regs = encode_bcd(raw, tag.register_count, conn.word_order)
        else:
            regs = encode_registers(raw, tag.data_type, conn.byte_order, conn.word_order,
                                    count=tag.count)
        for i, r in enumerate(regs):
            mem[tag.address + i] = r

    def get_raw_bit(self, name: str) -> bool:
        tag = self._tags[name]
        word = self.unit.areas[tag.register_type][tag.address]
        return decode_bit(word, tag.bit) if tag.bit is not None else bool(word)

    def step(self, dt: float = 1.0) -> None:
        """Advance the simulated process: read-only numeric tags move, counters count up."""
        self._t += dt
        for tag in self.device.tags:
            if tag.register_type in (RegisterType.HOLDING, RegisterType.COIL):
                continue  # setpoints/commands keep what was written
            if tag.data_type in (DataType.BOOL, DataType.STRING):
                continue
            self.set(tag.name, _animated_value(tag, self._t))


def _span(tag: ModbusTag) -> tuple[float, float]:
    lo = tag.min if tag.min is not None else 0.0
    hi = tag.max if tag.max is not None else 100.0
    return lo, hi


def _initial_value(tag: ModbusTag) -> Value:
    if tag.data_type is DataType.BOOL:
        return False
    if tag.data_type is DataType.STRING:
        return tag.name.upper()[: (tag.count or 1) * 2]
    return _animated_value(tag, 0.0)


def _animated_value(tag: ModbusTag, t: float) -> float:
    lo, hi = _span(tag)
    if tag.data_type in (DataType.UINT32, DataType.UINT64, DataType.INT64) and tag.min is None:
        return round(1000 + t * 10)  # monotonic counter (energy, run hours)
    mid, amp = (lo + hi) / 2, (hi - lo) * 0.3
    value = mid + amp * math.sin(t / 10 + len(tag.name))
    if tag.data_type in (DataType.INT16, DataType.UINT16, DataType.INT32, DataType.UINT32,
                         DataType.INT64, DataType.UINT64):
        step = tag.scale
        value = round(value / step) * step
    return value
