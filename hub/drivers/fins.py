"""Omron FINS driver (CS/CJ/CP/NJ/NX): memory area read/write over FINS/TCP or FINS/UDP.

Frames (FINS command reference, W227):

* FINS/TCP: every message starts with ``"FINS"``, length, command, error code. Command 0 is
  the node-address handshake (the PLC assigns the client node), command 2 carries a FINS frame.
* FINS frame: ICF RSV GCT DNA DA1 DA2 SNA SA1 SA2 SID, then MRC SRC and the parameters.
  Memory area read = 01 01 (area code, word, bit, count), write = 01 02 (+ data).
  Responses carry MRC SRC and a two-byte end code (0000 = normal).

Words are big-endian; 32/64-bit values are lowest word first in Omron CPUs (``word_order``).
Bits are written with the bit area codes, so only the addressed bit changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct

from hub.codec import CodecError, Value
from hub.drivers.base import DriverError
from hub.drivers.word_plc import PlcError, WordPlcDriver, value_to_words, words_to_value
from hub.models import DataType, FinsConnection, FinsDevice, FinsTag, TagBase

log = logging.getLogger(__name__)

WORD_AREAS = {"CIO": 0xB0, "W": 0xB1, "H": 0xB2, "A": 0xB3, "D": 0x82}
BIT_AREAS = {"CIO": 0x30, "W": 0x31, "H": 0x32, "A": 0x33, "D": 0x02}
AREA_OF_CODE = {code: name for name, code in WORD_AREAS.items()} | {
    code: name for name, code in BIT_AREAS.items()}
TCP_MAGIC = b"FINS"
TCP_HANDSHAKE, TCP_FRAME = 0, 2

END_CODES = {
    0x0101: "local node not in network", 0x0105: "node address setting error",
    0x0201: "destination node not in network", 0x0401: "service not supported",
    0x1001: "command too long", 0x1002: "command too short", 0x1101: "area classification "
    "missing", 0x1103: "address range exceeded", 0x1104: "address range exceeded",
    0x110B: "response too long", 0x2002: "protected", 0x2108: "data cannot be changed "
    "(CPU mode)", 0x2201: "not possible in this CPU mode (RUN)", 0x2202: "not possible in "
    "this CPU mode (MONITOR)",
}


def build_frame(dna: int, da1: int, da2: int, sna: int, sa1: int, sa2: int, sid: int,
                mrc: int, src: int, params: bytes, *, response: bool = False) -> bytes:
    icf = 0xC0 if response else 0x80
    return bytes([icf, 0x00, 0x02, dna, da1, da2, sna, sa1, sa2, sid, mrc, src]) + params


def parse_frame(data: bytes) -> tuple[int, int, int, bytes]:
    """(sid, mrc, src, rest) of a FINS frame."""
    if len(data) < 12:
        raise DriverError(f"FINS frame too short ({len(data)} bytes)")
    return data[9], data[10], data[11], data[12:]


def check_end_code(rest: bytes) -> bytes:
    if len(rest) < 2:
        raise DriverError("FINS response without end code")
    main, sub = rest[0] & 0x7F, rest[1] & 0x3F            # relay / fatal flags are not errors
    code = (main << 8) | sub
    if code != 0:
        meaning = END_CODES.get(code, "error")
        raise PlcError(f"FINS end code {code:04X} ({meaning})")
    return rest[2:]


def tcp_message(command: int, payload: bytes, error: int = 0) -> bytes:
    return TCP_MAGIC + struct.pack(">III", 8 + len(payload), command, error) + payload


async def read_tcp_message(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    head = await reader.readexactly(16)
    if head[:4] != TCP_MAGIC:
        raise DriverError("not a FINS/TCP message")
    length, command, error = struct.unpack(">III", head[4:16])
    if length < 8 or length > 4096:
        raise DriverError(f"bad FINS/TCP length {length}")
    return command, error, await reader.readexactly(length - 8)


def read_params(area_code: int, word: int, bit: int, count: int) -> bytes:
    return struct.pack(">BHBH", area_code, word, bit, count)


class FinsClient:
    """One FINS connection: request/response matched by SID, one request at a time."""

    def __init__(self, c: FinsConnection) -> None:
        self.c = c
        self._timeout = c.timeout_ms / 1000
        self._sid = 0
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._udp_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.dest_node = c.dest_node
        self.src_node = c.src_node

    async def connect(self) -> None:
        c = self.c
        try:
            if c.transport == "tcp":
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(c.host, c.port), self._timeout)
                self._writer.write(tcp_message(TCP_HANDSHAKE, struct.pack(">I", 0)))
                await self._writer.drain()
                command, error, payload = await asyncio.wait_for(
                    read_tcp_message(self._reader), self._timeout)
                if command != 1 or error or len(payload) < 8:
                    raise DriverError(f"FINS/TCP handshake refused (error {error:#x})")
                client_node, server_node = struct.unpack(">II", payload[:8])
                self.src_node = client_node
                self.dest_node = c.dest_node or server_node
            else:
                loop = asyncio.get_running_loop()
                queue = self._udp_queue

                class _Proto(asyncio.DatagramProtocol):
                    def datagram_received(self, data: bytes, addr: object) -> None:
                        queue.put_nowait(data)

                self._udp, _ = await loop.create_datagram_endpoint(
                    _Proto, remote_addr=(c.host, c.port))
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            await self.close()
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

    async def request(self, mrc: int, src: int, params: bytes) -> bytes:
        async with self._lock:
            self._sid = (self._sid + 1) % 256
            sid = self._sid
            frame = build_frame(self.c.dest_network, self.dest_node, self.c.dest_unit, 0,
                                self.src_node, 0, sid, mrc, src, params)
            try:
                rest = await asyncio.wait_for(self._exchange(frame, sid), self._timeout)
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise DriverError(f"FINS {mrc:02X}{src:02X}: {exc}") from exc
            except TimeoutError as exc:
                raise DriverError(f"FINS {mrc:02X}{src:02X}: no answer in "
                                  f"{self._timeout:g} s") from exc
            return check_end_code(rest)

    async def _exchange(self, frame: bytes, sid: int) -> bytes:
        if self._writer is not None and self._reader is not None:
            self._writer.write(tcp_message(TCP_FRAME, frame))
            await self._writer.drain()
            while True:
                command, error, payload = await read_tcp_message(self._reader)
                if error:
                    raise DriverError(f"FINS/TCP error {error:#x}")
                if command != TCP_FRAME:
                    continue
                got, _, _, rest = parse_frame(payload)
                if got == sid:
                    return rest
        if self._udp is None:
            raise DriverError("not connected")
        self._udp.sendto(frame)
        while True:
            got, _, _, rest = parse_frame(await self._udp_queue.get())
            if got == sid:
                return rest

    async def read_words(self, area: str, word: int, count: int) -> list[int]:
        data = await self.request(0x01, 0x01, read_params(WORD_AREAS[area], word, 0, count))
        if len(data) != 2 * count:
            raise DriverError(f"FINS read {area}{word}: {len(data)} bytes for {count} words")
        return list(struct.unpack(f">{count}H", data))

    async def write_words(self, area: str, word: int, words: list[int]) -> None:
        params = read_params(WORD_AREAS[area], word, 0, len(words))
        await self.request(0x01, 0x02, params + struct.pack(f">{len(words)}H", *words))

    async def write_bit(self, area: str, word: int, bit: int, value: bool) -> None:
        params = read_params(BIT_AREAS[area], word, bit, 1)
        await self.request(0x01, 0x02, params + (b"\x01" if value else b"\x00"))


class FinsDriver(WordPlcDriver[FinsDevice]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        c = self.device.connection
        self._setup_spans(c.max_words_per_read, c.max_gap_words)
        self._client: FinsClient | None = None

    def _location(self, tag: FinsTag) -> tuple[str, int, int]:  # type: ignore[override]
        a = tag.parsed
        return a.area, a.word, tag.word_count

    def _decode(self, tag: FinsTag, units: list[int]) -> Value:  # type: ignore[override]
        return words_to_value(tag, units, tag.parsed.bit, self.device.connection.word_order)

    async def _connect(self) -> None:
        client = FinsClient(self.device.connection)
        await client.connect()
        self._client = client

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _require(self) -> FinsClient:
        if self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        return self._client

    async def _read_units(self, area: str, start: int, count: int) -> list[int]:
        return await self._require().read_words(area, start, count)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        assert isinstance(tag, FinsTag)
        a = tag.parsed
        client = self._require()
        try:
            if tag.data_type is DataType.BOOL:
                await client.write_bit(a.area, a.word, a.bit or 0, bool(raw))
            else:
                words = value_to_words(tag, raw, self.device.connection.word_order)
                await client.write_words(a.area, a.word, words)
        except CodecError as exc:
            raise DriverError(str(exc)) from exc
