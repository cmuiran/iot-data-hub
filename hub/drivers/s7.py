"""Siemens S7 driver (S7comm over ISO-on-TCP, port 102) using python-snap7's asyncio client.

For S7-300/400 and S7-1200/1500 (the latter with PUT/GET enabled and non-optimized DBs).
Tags are grouped per area/DB into byte blocks (gaps up to ``max_gap_bytes``, at most
``max_bytes_per_read``) so a data block with 50 values is one request. A block the PLC refuses
(address beyond the DB, area not present) is read tag by tag from then on, so one wrong
address does not hide the rest of the device.

Writes come only from the control-safety layer (``Driver.write_tag``). A bool is written with
the protocol's single-bit transport, so the other bits of that byte — which the PLC program
may be changing at the same moment — are never touched (no read-modify-write).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass

from snap7.async_client import AsyncClient
from snap7.datatypes import S7WordLen
from snap7.error import S7ConnectionError, S7Error, S7ProtocolError, S7TimeoutError
from snap7.type import Area

from hub.codec import CodecError, Value
from hub.drivers.base import DeviceState, Driver, DriverError, sleep_until_next
from hub.models import DataType, S7Area, S7Device, S7Tag, TagBase

log = logging.getLogger(__name__)

AREAS = {S7Area.DB: Area.DB, S7Area.MERKER: Area.MK, S7Area.INPUT: Area.PE,
         S7Area.OUTPUT: Area.PA}
_FORMATS = {
    ("W", DataType.INT16): ">h", ("W", DataType.UINT16): ">H",
    ("D", DataType.INT32): ">i", ("D", DataType.UINT32): ">I", ("D", DataType.FLOAT32): ">f",
    ("B", DataType.INT64): ">q", ("B", DataType.UINT64): ">Q", ("B", DataType.FLOAT64): ">d",
}


# --------------------------------------------------------------------------------------------
# Byte layout (pure; shared with the simulator)
# --------------------------------------------------------------------------------------------


def decode_s7(tag: S7Tag, data: bytes | bytearray) -> Value:
    """Value of ``tag`` from the bytes that start at its byte address."""
    a = tag.parsed
    if len(data) < tag.byte_length:
        raise CodecError(f"{tag.address}: need {tag.byte_length} bytes, got {len(data)}")
    if tag.data_type is DataType.BOOL:
        return bool((data[0] >> (a.bit or 0)) & 1)
    if tag.data_type is DataType.STRING:
        declared, current = data[0], data[1]
        length = min(current, declared, tag.string_length or 0)
        return bytes(data[2 : 2 + length]).decode("latin-1")
    if a.size == "B" and tag.data_type in (DataType.INT16, DataType.UINT16):
        return struct.unpack(">b" if tag.data_type is DataType.INT16 else ">B", data[:1])[0]
    return struct.unpack(_FORMATS[(a.size, tag.data_type)], bytes(data[: tag.byte_length]))[0]


def encode_s7(tag: S7Tag, raw: Value) -> bytes:
    """Bytes to write at the tag's byte address (not for bool: those are single-bit writes)."""
    a = tag.parsed
    if tag.data_type is DataType.STRING:
        size = tag.string_length or 0
        text = str(raw).encode("latin-1", errors="replace")[:size]
        return bytes([size, len(text)]) + text.ljust(size, b"\x00")   # the whole declared STRING
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise CodecError(f"{tag.address} needs a number, got {raw!r}")
    try:
        if a.size == "B" and tag.data_type in (DataType.INT16, DataType.UINT16):
            return struct.pack(">b" if tag.data_type is DataType.INT16 else ">B", int(raw))
        fmt = _FORMATS[(a.size, tag.data_type)]
        return struct.pack(fmt, raw if fmt in (">f", ">d") else int(raw))
    except struct.error as exc:
        raise CodecError(f"{raw!r} does not fit {tag.address} ({tag.data_type}): {exc}") from exc


@dataclass
class S7Block:
    area: S7Area
    db: int
    start: int
    size: int
    tags: list[S7Tag]
    split: bool = False


def plan_s7_blocks(tags: list[S7Tag], max_bytes: int, max_gap: int) -> list[S7Block]:
    groups: dict[tuple[S7Area, int], list[S7Tag]] = {}
    for tag in tags:
        a = tag.parsed
        groups.setdefault((a.area, a.db), []).append(tag)
    blocks: list[S7Block] = []
    for (area, db), group in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
        group.sort(key=lambda t: t.parsed.byte)
        current: S7Block | None = None
        for tag in group:
            start = tag.parsed.byte
            end = start + tag.byte_length
            if current is not None:
                cur_end = current.start + current.size
                new_end = max(cur_end, end)
                if start <= cur_end + max_gap and new_end - current.start <= max_bytes:
                    current.size = new_end - current.start
                    current.tags.append(tag)
                    continue
                blocks.append(current)
            current = S7Block(area, db, start, tag.byte_length, [tag])
        if current is not None:
            blocks.append(current)
    return blocks


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------


class S7Driver(Driver[S7Device]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        c = self.device.connection
        self._blocks = plan_s7_blocks(list(self.device.tags), c.max_bytes_per_read,
                                      c.max_gap_bytes)
        self._client: AsyncClient | None = None

    @property
    def _timeout(self) -> float:
        return self.device.connection.timeout_ms / 1000

    async def _connect(self) -> None:
        c = self.device.connection
        client = AsyncClient()
        try:
            await asyncio.wait_for(client.connect(c.host, c.rack, c.slot, tcp_port=c.port),
                                   self._timeout)
        except (S7Error, OSError, TimeoutError) as exc:
            raise DriverError(f"cannot connect to {c.host}:{c.port} rack {c.rack} slot "
                              f"{c.slot}: {exc}") from exc
        self._client = client

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 2)
            except Exception:  # noqa: BLE001 - the connection may already be gone
                pass

    async def _run_connected(self) -> None:
        period = self.device.poll_interval_ms / 1000
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.poll_once()
            await sleep_until_next(period, started, self._stopping)

    async def poll_once(self) -> None:
        samples = []
        failures = 0
        for block in self._blocks:
            values: dict[str, Value | None] = {}
            try:
                values = await self._read_block(block)
            except S7ProtocolError as exc:
                failures += 1
                if not block.split and len(block.tags) > 1:
                    block.split = True
                    log.info("%s: reading %s%s at %d tag by tag after: %s",
                             self.device.device_id, block.area.value,
                             block.db or "", block.start, exc)
                    try:
                        values = await self._read_block(block)
                        failures -= 1
                    except S7ProtocolError as exc2:
                        exc = exc2
                if failures:
                    await self.mark_error(DeviceState.ERROR, f"{block.area.value}"
                                          f"{block.db or ''} byte {block.start}: {exc}")
            for tag in block.tags:
                samples.append(self.make_sample(tag, values.get(tag.name)))
        await self.emit(samples)
        if failures == 0:
            await self.mark_seen()

    async def _read_block(self, block: S7Block) -> dict[str, Value | None]:
        if block.split:
            out: dict[str, Value | None] = {}
            for tag in block.tags:
                try:
                    out[tag.name] = await self._read_raw(tag)
                except (S7ProtocolError, CodecError) as exc:
                    log.debug("%s: %s: %s", self.device.device_id, tag.address, exc)
                    out[tag.name] = None
            if all(v is None for v in out.values()):
                raise S7ProtocolError(f"no tag of the block at byte {block.start} is readable")
            return out
        data = await self._read(block.area, block.db, block.start, block.size)
        out = {}
        for tag in block.tags:
            offset = tag.parsed.byte - block.start
            try:
                out[tag.name] = decode_s7(tag, data[offset : offset + tag.byte_length])
            except CodecError as exc:
                log.warning("%s: %s", self.device.device_id, exc)
                out[tag.name] = None
        return out

    async def _read(self, area: S7Area, db: int, start: int, size: int) -> bytearray:
        client = self._require()
        try:
            return await asyncio.wait_for(client.read_area(AREAS[area], db, start, size),
                                          self._timeout)
        except S7ProtocolError:
            raise
        except (S7ConnectionError, S7TimeoutError, OSError, TimeoutError) as exc:
            raise DriverError(f"read {area.value}{db or ''} byte {start}: {exc}") from exc

    def _require(self) -> AsyncClient:
        if self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        return self._client

    async def _read_raw(self, tag: TagBase) -> Value:
        assert isinstance(tag, S7Tag)
        a = tag.parsed
        data = await self._read(a.area, a.db, a.byte, tag.byte_length)
        return decode_s7(tag, data)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        assert isinstance(tag, S7Tag)
        a = tag.parsed
        client = self._require()
        try:
            if tag.data_type is DataType.BOOL:
                await asyncio.wait_for(self._write_bit(client, tag, bool(raw)), self._timeout)
            else:
                await asyncio.wait_for(
                    client.write_area(AREAS[a.area], a.db, a.byte, bytearray(encode_s7(tag, raw))),
                    self._timeout)
        except CodecError as exc:
            raise DriverError(str(exc)) from exc
        except (S7Error, OSError, TimeoutError) as exc:
            raise DriverError(f"write {tag.address}: {exc}") from exc

    @staticmethod
    async def _write_bit(client: AsyncClient, tag: S7Tag, value: bool) -> None:
        """One bit, with the S7 BIT transport (address = byte * 8 + bit)."""
        a = tag.parsed
        protocol = client.protocol
        request = protocol.build_write_request(
            area=client._map_area(AREAS[a.area]), db_number=a.db,  # noqa: SLF001
            start=a.byte * 8 + (a.bit or 0), word_len=S7WordLen.BIT,
            data=b"\x01" if value else b"\x00")
        response = await client._send_receive(request)  # noqa: SLF001
        protocol.check_write_response(response)


__all__ = ["S7Block", "S7Driver", "decode_s7", "encode_s7", "plan_s7_blocks"]
