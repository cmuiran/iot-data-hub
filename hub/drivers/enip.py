"""EtherNet/IP driver for Allen-Bradley Logix controllers: CIP explicit messaging by tag name.

* Encapsulation (TCP 44818): RegisterSession, then SendRRData with a Common Packet Format of
  a null address item and an unconnected data item carrying the CIP request.
* With ``slot`` the request is wrapped in Unconnected Send (Connection Manager, service 0x52)
  with the route path "backplane port 1, slot n"; without, it goes to the Message Router.
* Read Tag (0x4C) / Write Tag (0x4D) with a symbolic EPATH (0x91 segments, 0x28/0x29/0x2A
  element segments); many reads go in one Multiple Service Packet (0x0A).

Values come with their CIP type, which is checked against the tag's data_type; writes use the
type the controller reported for that tag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import struct
from typing import Any

from hub.codec import CodecError, Value
from hub.drivers.base import DeviceState, Driver, DriverError, sleep_until_next
from hub.models import DataType, EnipDevice, EnipTag, TagBase

log = logging.getLogger(__name__)

REGISTER_SESSION, UNREGISTER_SESSION, SEND_RR_DATA = 0x65, 0x66, 0x6F
READ_TAG, WRITE_TAG, MULTIPLE, UNCONNECTED_SEND = 0x4C, 0x4D, 0x0A, 0x52
MESSAGE_ROUTER = bytes([0x20, 0x02, 0x24, 0x01])
CONNECTION_MANAGER = bytes([0x20, 0x06, 0x24, 0x01])

# CIP elementary types: code -> (name, struct format)
CIP_TYPES: dict[int, tuple[str, str]] = {
    0xC1: ("BOOL", "<B"), 0xC2: ("SINT", "<b"), 0xC3: ("INT", "<h"), 0xC4: ("DINT", "<i"),
    0xC5: ("LINT", "<q"), 0xC6: ("USINT", "<B"), 0xC7: ("UINT", "<H"), 0xC8: ("UDINT", "<I"),
    0xC9: ("ULINT", "<Q"), 0xCA: ("REAL", "<f"), 0xCB: ("LREAL", "<d"),
}
CIP_CODE = {name: code for code, (name, _) in CIP_TYPES.items()}
STRUCT_TYPE, LOGIX_STRING_HANDLE = 0x02A0, 0x0FCE
INTEGER_CODES = frozenset({0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9})
# which CIP types each hub data type accepts
ACCEPTS: dict[DataType, frozenset[int]] = {
    DataType.BOOL: frozenset({0xC1}),
    DataType.INT16: frozenset({0xC2, 0xC3, 0xC6}),
    DataType.UINT16: frozenset({0xC6, 0xC7}),
    DataType.INT32: frozenset({0xC2, 0xC3, 0xC4, 0xC6, 0xC7}),
    DataType.UINT32: frozenset({0xC6, 0xC7, 0xC8}),
    DataType.INT64: INTEGER_CODES,
    DataType.UINT64: frozenset({0xC6, 0xC7, 0xC8, 0xC9}),
    DataType.FLOAT32: frozenset({0xCA}),
    DataType.FLOAT64: frozenset({0xCA, 0xCB}),
}
DEFAULT_CODE = {DataType.BOOL: 0xC1, DataType.INT16: 0xC3, DataType.UINT16: 0xC7,
                DataType.INT32: 0xC4, DataType.UINT32: 0xC8, DataType.INT64: 0xC5,
                DataType.UINT64: 0xC9, DataType.FLOAT32: 0xCA, DataType.FLOAT64: 0xCB}
GENERAL_STATUS = {0x01: "connection failure", 0x04: "path segment error (unknown tag?)",
                  0x05: "path destination unknown", 0x06: "partial transfer",
                  0x08: "service not supported", 0x0E: "attribute not settable",
                  0x10: "device state conflict (mode/keyswitch)", 0x13: "not enough data",
                  0x15: "too much data", 0x1E: "embedded service error", 0x26: "path size "
                  "invalid", 0xFF: "general error (type mismatch / not writable?)"}

_SEGMENT = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_:]*)(?:\[(?P<index>[\d,]+)\])?")


class CipError(DriverError):
    """The controller refused one request (unknown tag, type mismatch): connection is fine."""


def tag_path(tag: str) -> bytes:
    """Symbolic EPATH of a Logix tag name (``Program:Main.Motors[3].Current``)."""
    out = bytearray()
    # "Program:Main.Level": the program scope and the tag are two symbol segments
    for part in tag.split("."):
        m = _SEGMENT.fullmatch(part)
        if m is None:
            raise ValueError(f"cannot encode tag {tag!r}")
        name = m["name"].encode("ascii")
        out += bytes([0x91, len(name)]) + name + (b"\x00" if len(name) % 2 else b"")
        if m["index"]:
            for index in (int(i) for i in m["index"].split(",")):
                if index <= 0xFF:
                    out += bytes([0x28, index])
                elif index <= 0xFFFF:
                    out += bytes([0x29, 0x00]) + struct.pack("<H", index)
                else:
                    out += bytes([0x2A, 0x00]) + struct.pack("<I", index)
    return bytes(out)


def parse_path(path: bytes) -> str:
    """Tag name from a symbolic EPATH (simulator side)."""
    parts: list[str] = []
    i = 0
    while i < len(path):
        seg = path[i]
        if seg == 0x91:
            n = path[i + 1]
            parts.append(path[i + 2:i + 2 + n].decode("ascii"))
            i += 2 + n + (n % 2)
        elif seg == 0x28:
            parts[-1] += f"[{path[i + 1]}]"
            i += 2
        elif seg == 0x29:
            parts[-1] += f"[{struct.unpack_from('<H', path, i + 2)[0]}]"
            i += 4
        elif seg == 0x2A:
            parts[-1] += f"[{struct.unpack_from('<I', path, i + 2)[0]}]"
            i += 6
        else:
            raise ValueError(f"unsupported path segment {seg:#x}")
    return ".".join(parts)


def cip_request(service: int, path: bytes, data: bytes = b"") -> bytes:
    if len(path) % 2:
        raise ValueError("EPATH must have an even length")
    return bytes([service, len(path) // 2]) + path + data


def parse_reply(reply: bytes) -> tuple[int, int, bytes]:
    """(reply service, general status, data) of a CIP reply."""
    if len(reply) < 4:
        raise DriverError("CIP reply too short")
    service, status, ext_words = reply[0], reply[2], reply[3]
    return service, status, reply[4 + 2 * ext_words:]


def status_text(status: int) -> str:
    return f"CIP status {status:#04x} ({GENERAL_STATUS.get(status, 'error')})"


def multiple_service(requests: list[bytes]) -> bytes:
    count = len(requests)
    offsets, offset = [], 2 + 2 * count
    for r in requests:
        offsets.append(offset)
        offset += len(r)
    data = struct.pack(f"<H{count}H", count, *offsets) + b"".join(requests)
    return cip_request(MULTIPLE, MESSAGE_ROUTER, data)


def split_multiple(data: bytes) -> list[bytes]:
    (count,) = struct.unpack_from("<H", data)
    offsets = list(struct.unpack_from(f"<{count}H", data, 2)) + [len(data)]
    return [data[offsets[i]:offsets[i + 1]] for i in range(count)]


def unconnected_send(request: bytes, slot: int) -> bytes:
    route = bytes([0x01, slot])                         # port 1 (backplane), slot
    body = bytes([0x0A, 0x05]) + struct.pack("<H", len(request)) + request
    if len(request) % 2:
        body += b"\x00"
    body += bytes([len(route) // 2, 0x00]) + route
    return cip_request(UNCONNECTED_SEND, CONNECTION_MANAGER, body)


def encap(command: int, session: int, data: bytes, context: bytes = b"hubenip0",
          status: int = 0) -> bytes:
    return struct.pack("<HHII8sI", command, len(data), session, status, context, 0) + data


def rr_data(cip: bytes) -> bytes:
    """SendRRData payload: interface 0, timeout, CPF with null address + unconnected data."""
    return struct.pack("<IHHHHHH", 0, 10, 2, 0x0000, 0, 0x00B2, len(cip)) + cip


def cip_from_rr(data: bytes) -> bytes:
    """The CIP message inside a SendRRData payload (CPF parsing)."""
    (count,) = struct.unpack_from("<H", data, 6)
    pos = 8
    for _ in range(count):
        item_type, length = struct.unpack_from("<HH", data, pos)
        pos += 4
        if item_type == 0x00B2:
            return data[pos:pos + length]
        pos += length
    raise DriverError("SendRRData without unconnected data item")


async def read_encap(reader: asyncio.StreamReader) -> tuple[int, int, int, bytes]:
    head = await reader.readexactly(24)
    command, length, session, status, _, _ = struct.unpack("<HHII8sI", head)
    return command, session, status, await reader.readexactly(length)


def decode_value(data: bytes) -> tuple[int, Value]:
    """(CIP type, value) of a Read Tag reply's data."""
    if len(data) < 2:
        raise CodecError("empty Read Tag reply")
    (code,) = struct.unpack_from("<H", data)
    if code == STRUCT_TYPE:
        (handle,) = struct.unpack_from("<H", data, 2)
        if handle != LOGIX_STRING_HANDLE:
            raise CodecError(f"structure (handle {handle:#06x}) is not a STRING")
        (length,) = struct.unpack_from("<i", data, 4)
        return code, data[8:8 + max(0, length)].decode("latin-1")
    if code not in CIP_TYPES:
        raise CodecError(f"unsupported CIP type {code:#06x}")
    name, fmt = CIP_TYPES[code]
    (value,) = struct.unpack_from(fmt, data, 2)
    return code, (value != 0) if name == "BOOL" else value


def encode_value(code: int, value: Value) -> bytes:
    name, fmt = CIP_TYPES[code]
    if name == "BOOL":
        return struct.pack("<HH", code, 1) + (b"\xff" if value else b"\x00")
    try:
        return struct.pack("<HH", code, 1) + struct.pack(fmt, value)
    except struct.error as exc:
        raise CodecError(f"{value!r} does not fit {name}") from exc


class EnipClient:
    def __init__(self, host: str, port: int, slot: int | None, timeout_s: float) -> None:
        self.host, self.port, self.slot, self.timeout = host, port, slot, timeout_s
        self.session = 0
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
            self._writer.write(encap(REGISTER_SESSION, 0, struct.pack("<HH", 1, 0)))
            await self._writer.drain()
            command, session, status, _ = await asyncio.wait_for(read_encap(self._reader),
                                                                 self.timeout)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            await self.close()
            raise DriverError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        if command != REGISTER_SESSION or status or not session:
            await self.close()
            raise DriverError(f"RegisterSession refused (status {status:#x})")
        self.session = session

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.write(encap(UNREGISTER_SESSION, self.session, b""))
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def send(self, request: bytes) -> bytes:
        """One CIP request (routed when a slot is set); returns the CIP reply."""
        if self._writer is None or self._reader is None:
            raise DriverError("not connected")
        msg = unconnected_send(request, self.slot) if self.slot is not None else request
        async with self._lock:
            try:
                self._writer.write(encap(SEND_RR_DATA, self.session, rr_data(msg)))
                await self._writer.drain()
                command, _, status, data = await asyncio.wait_for(read_encap(self._reader),
                                                                  self.timeout)
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise DriverError(f"EtherNet/IP: {exc}") from exc
            except TimeoutError as exc:
                raise DriverError(f"EtherNet/IP: no answer in {self.timeout:g} s") from exc
        if command != SEND_RR_DATA or status:
            raise DriverError(f"SendRRData failed (encapsulation status {status:#x})")
        reply = cip_from_rr(data)
        service, gstatus, _ = parse_reply(reply)
        if service == UNCONNECTED_SEND | 0x80 and gstatus:
            raise DriverError(f"routing to slot {self.slot} failed: {status_text(gstatus)}")
        return reply

    async def read_many(self, tags: list[str]) -> list[tuple[int, Any] | CipError]:
        requests = [cip_request(READ_TAG, tag_path(t), struct.pack("<H", 1)) for t in tags]
        if len(requests) == 1:
            replies = [await self.send(requests[0])]
        else:
            service, status, data = parse_reply(await self.send(multiple_service(requests)))
            if status not in (0x00, 0x1E):
                raise CipError(status_text(status))
            replies = split_multiple(data)
        out: list[tuple[int, Any] | CipError] = []
        for name, reply in zip(tags, replies, strict=True):
            _, status, data = parse_reply(reply)
            if status:
                out.append(CipError(f"{name}: {status_text(status)}"))
                continue
            try:
                out.append(decode_value(data))
            except CodecError as exc:
                out.append(CipError(f"{name}: {exc}"))
        return out

    async def write(self, tag: str, code: int, value: Value) -> None:
        reply = await self.send(cip_request(WRITE_TAG, tag_path(tag), encode_value(code, value)))
        _, status, _ = parse_reply(reply)
        if status:
            raise CipError(f"write {tag}: {status_text(status)}")


class EnipDriver(Driver[EnipDevice]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._client: EnipClient | None = None
        self._codes: dict[str, int] = {}                # tag name -> CIP type seen on read
        n = self.device.connection.max_tags_per_request
        tags = list(self.device.tags)
        self._batches = [tags[i:i + n] for i in range(0, len(tags), n)]

    async def _connect(self) -> None:
        c = self.device.connection
        client = EnipClient(c.host, c.port, c.slot, c.timeout_ms / 1000)
        await client.connect()
        self._client = client

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _require(self) -> EnipClient:
        if self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        return self._client

    async def _run_connected(self) -> None:
        period = self.device.poll_interval_ms / 1000
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.poll_once()
            await sleep_until_next(period, started, self._stopping)

    def _convert(self, tag: EnipTag, code: int, value: Any) -> Value:
        if tag.bit is not None:
            if code not in INTEGER_CODES:
                raise CodecError(f"{tag.tag} is {CIP_TYPES.get(code, ('?',))[0]}, not an integer")
            return bool((int(value) >> tag.bit) & 1)
        if tag.data_type is DataType.STRING:
            if code != STRUCT_TYPE:
                raise CodecError(f"{tag.tag} is not a STRING")
            return value
        if code not in ACCEPTS[tag.data_type]:
            name = "STRING" if code == STRUCT_TYPE else CIP_TYPES.get(code, (f"{code:#x}",))[0]
            raise CodecError(f"{tag.tag} is {name} in the controller; data_type "
                             f"{tag.data_type} does not match")
        self._codes[tag.name] = code
        return value

    async def poll_once(self) -> None:
        client = self._require()
        samples, problems = [], []
        for batch in self._batches:
            results = await client.read_many([t.tag for t in batch])
            for tag, result in zip(batch, results, strict=True):
                raw: Value | None = None
                if isinstance(result, CipError):
                    problems.append(str(result))
                else:
                    try:
                        raw = self._convert(tag, *result)
                    except CodecError as exc:
                        problems.append(str(exc))
                samples.append(self.make_sample(tag, raw))
        await self.emit(samples)
        if problems:
            await self.mark_error(DeviceState.ERROR, "; ".join(problems[:3]))
        else:
            await self.mark_seen()

    async def _read_raw(self, tag: TagBase) -> Value:
        assert isinstance(tag, EnipTag)
        (result,) = await self._require().read_many([tag.tag])
        if isinstance(result, CipError):
            raise result
        return self._convert(tag, *result)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        assert isinstance(tag, EnipTag)
        if tag.name not in self._codes:
            await self._read_raw(tag)                   # learn the controller's type first
        code = self._codes.get(tag.name, DEFAULT_CODE[tag.data_type])
        try:
            await self._require().write(tag.tag, code, raw)
        except CodecError as exc:
            raise DriverError(str(exc)) from exc
