"""Mitsubishi MELSEC driver: MC protocol, QnA-compatible 3E frame, binary code, TCP or UDP.

Request:  50 00 | network | PC | module I/O (2) | station | length (2) | monitoring timer (2) |
          command (2) | subcommand (2) | data        (all multi-byte fields little-endian)
Response: D0 00 | network | PC | module I/O (2) | station | length (2) | end code (2) | data

Batch read 0401 / batch write 1401, subcommand 0000 = word units, 0001 = bit units; device =
head number (3 bytes) + device code (1 byte) + number of points (2). Word data is
little-endian per word; bit units pack two points per byte, the first in the high nibble.
32/64-bit values are lowest word first (``word_order``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct

from hub.codec import CodecError, Value
from hub.drivers.base import DriverError
from hub.drivers.word_plc import PlcError, WordPlcDriver, value_to_words, words_to_value
from hub.models import MC_DEVICES, DataType, McConnection, McDevice, McTag, TagBase

log = logging.getLogger(__name__)

READ, WRITE = 0x0401, 0x1401
WORD_UNITS, BIT_UNITS = 0x0000, 0x0001
REQUEST_HEADER, RESPONSE_HEADER = b"\x50\x00", b"\xd0\x00"
END_CODES = {0xC050: "ASCII data in binary mode", 0xC051: "too many points",
             0xC056: "address out of range", 0xC058: "length mismatch",
             0xC059: "command/subcommand not supported", 0xC05B: "device cannot be accessed",
             0xC05C: "wrong request", 0xC061: "length mismatch", 0x4031: "device out of range"}


def device_field(device: str, number: int, points: int) -> bytes:
    code = MC_DEVICES[device][0]
    return number.to_bytes(3, "little") + bytes([code]) + struct.pack("<H", points)


def build_request(c: McConnection, command: int, subcommand: int, data: bytes,
                  timer: int) -> bytes:
    body = struct.pack("<HHH", timer, command, subcommand) + data
    return (REQUEST_HEADER + bytes([c.network, c.pc]) + struct.pack("<H", c.module_io)
            + bytes([c.station]) + struct.pack("<H", len(body)) + body)


def pack_bits(points: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(points), 2):
        hi = 1 if points[i] else 0
        lo = 1 if i + 1 < len(points) and points[i + 1] else 0
        out.append((hi << 4) | lo)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> list[int]:
    points = []
    for byte in data:
        points += [(byte >> 4) & 1, byte & 1]
    return points[:count]


async def read_response(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(9)
    if head[:2] != RESPONSE_HEADER:
        raise DriverError(f"not an MC 3E binary response ({head[:2].hex()})")
    (length,) = struct.unpack("<H", head[7:9])
    return await reader.readexactly(length)


def check_end_code(payload: bytes) -> bytes:
    if len(payload) < 2:
        raise DriverError("MC response without end code")
    (code,) = struct.unpack("<H", payload[:2])
    if code != 0:
        raise PlcError(f"MC end code {code:04X} ({END_CODES.get(code, 'error')})")
    return payload[2:]


class McClient:
    def __init__(self, c: McConnection) -> None:
        self.c = c
        self._timeout = c.timeout_ms / 1000
        self._timer = max(1, min(0xFFFF, c.timeout_ms // 250))   # unit: 250 ms
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._udp_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def connect(self) -> None:
        c = self.c
        try:
            if c.transport == "tcp":
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(c.host, c.port), self._timeout)
            else:
                queue = self._udp_queue

                class _Proto(asyncio.DatagramProtocol):
                    def datagram_received(self, data: bytes, addr: object) -> None:
                        queue.put_nowait(data)

                self._udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    _Proto, remote_addr=(c.host, c.port))
        except (OSError, TimeoutError) as exc:
            raise DriverError(f"cannot connect to {c.host}:{c.port}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        udp, self._udp = self._udp, None
        if udp is not None:
            udp.close()

    async def request(self, command: int, subcommand: int, data: bytes) -> bytes:
        frame = build_request(self.c, command, subcommand, data, self._timer)
        async with self._lock:
            try:
                payload = await asyncio.wait_for(self._exchange(frame), self._timeout + 0.5)
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise DriverError(f"MC {command:04X}: {exc}") from exc
            except TimeoutError as exc:
                raise DriverError(f"MC {command:04X}: no answer in {self._timeout:g} s") from exc
        return check_end_code(payload)

    async def _exchange(self, frame: bytes) -> bytes:
        if self._writer is not None and self._reader is not None:
            self._writer.write(frame)
            await self._writer.drain()
            return await read_response(self._reader)
        if self._udp is None:
            raise DriverError("not connected")
        self._udp.sendto(frame)
        data = await self._udp_queue.get()
        if data[:2] != RESPONSE_HEADER or len(data) < 11:
            raise DriverError("not an MC 3E binary response")
        return data[9:]

    async def read_words(self, device: str, number: int, count: int) -> list[int]:
        data = await self.request(READ, WORD_UNITS, device_field(device, number, count))
        if len(data) != 2 * count:
            raise DriverError(f"MC read {device}{number}: {len(data)} bytes for {count} words")
        return list(struct.unpack(f"<{count}H", data))

    async def read_bits(self, device: str, number: int, count: int) -> list[int]:
        data = await self.request(READ, BIT_UNITS, device_field(device, number, count))
        return unpack_bits(data, count)

    async def write_words(self, device: str, number: int, words: list[int]) -> None:
        await self.request(WRITE, WORD_UNITS, device_field(device, number, len(words))
                           + struct.pack(f"<{len(words)}H", *words))

    async def write_bits(self, device: str, number: int, points: list[int]) -> None:
        await self.request(WRITE, BIT_UNITS, device_field(device, number, len(points))
                           + pack_bits(points))


class McDriver(WordPlcDriver[McDevice]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        c = self.device.connection
        self._setup_spans(c.max_points_per_read, c.max_gap_points)
        self._client: McClient | None = None

    def _location(self, tag: McTag) -> tuple[str, int, int]:  # type: ignore[override]
        a = tag.parsed
        return a.area, a.word, tag.word_count

    def _decode(self, tag: McTag, units: list[int]) -> Value:  # type: ignore[override]
        if tag.is_bit_device:
            return bool(units[0])
        return words_to_value(tag, units, tag.parsed.bit, self.device.connection.word_order)

    async def _connect(self) -> None:
        client = McClient(self.device.connection)
        await client.connect()
        self._client = client

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _require(self) -> McClient:
        if self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        return self._client

    async def _read_units(self, area: str, start: int, count: int) -> list[int]:
        client = self._require()
        if MC_DEVICES[area][1]:
            return await client.read_words(area, start, count)
        return await client.read_bits(area, start, count)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        assert isinstance(tag, McTag)
        a = tag.parsed
        client = self._require()
        try:
            if tag.is_bit_device:
                await client.write_bits(a.area, a.word, [1 if raw else 0])
            elif tag.data_type is DataType.BOOL:
                raise DriverError("bits of word registers are read-only")
            else:
                words = value_to_words(tag, raw, self.device.connection.word_order)
                await client.write_words(a.area, a.word, words)
        except CodecError as exc:
            raise DriverError(str(exc)) from exc
