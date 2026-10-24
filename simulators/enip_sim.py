"""A Logix controller answering EtherNet/IP explicit messages (RegisterSession, SendRRData with
or without Unconnected Send, Multiple Service Packet, Read/Write Tag) for tests and local
development. Tags live in a dict name -> [CIP type, value]."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import struct
from typing import Any

from hub.codec import Value, to_raw
from hub.drivers.enip import (
    CIP_TYPES,
    LOGIX_STRING_HANDLE,
    MULTIPLE,
    READ_TAG,
    REGISTER_SESSION,
    SEND_RR_DATA,
    STRUCT_TYPE,
    UNCONNECTED_SEND,
    UNREGISTER_SESSION,
    WRITE_TAG,
    cip_from_rr,
    encap,
    parse_path,
    read_encap,
    rr_data,
    split_multiple,
)
from hub.models import Access, DataType, EnipDevice
from simulators.plc_memory import animated

log = logging.getLogger(__name__)
SIM_TYPE = {DataType.BOOL: 0xC1, DataType.INT16: 0xC3, DataType.UINT16: 0xC7,
            DataType.INT32: 0xC4, DataType.UINT32: 0xC8, DataType.INT64: 0xC5,
            DataType.UINT64: 0xC9, DataType.FLOAT32: 0xCA, DataType.FLOAT64: 0xCB}


def _reply(service: int, status: int, data: bytes = b"") -> bytes:
    return bytes([service | 0x80, 0, status, 0]) + data


class EnipSimServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.host, self.port = host, port
        self.tags: dict[str, list[Any]] = {}            # name -> [cip type, value]
        self.read_only: set[str] = set()
        self.requests = 0
        self._sessions = itertools.count(0x1000)
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    async def __aenter__(self) -> EnipSimServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        session = 0
        try:
            while True:
                command, sess, _, data = await read_encap(reader)
                if command == REGISTER_SESSION:
                    session = next(self._sessions)
                    writer.write(encap(REGISTER_SESSION, session, struct.pack("<HH", 1, 0)))
                elif command == UNREGISTER_SESSION:
                    break
                elif command == SEND_RR_DATA:
                    if sess != session:
                        writer.write(encap(SEND_RR_DATA, sess, b"", status=0x64))
                    else:
                        self.requests += 1
                        writer.write(encap(SEND_RR_DATA, session,
                                           rr_data(self.handle(cip_from_rr(data)))))
                else:
                    writer.write(encap(command, sess, b"", status=0x01))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def handle(self, request: bytes) -> bytes:
        service, words = request[0], request[1]
        path, data = request[2:2 + 2 * words], request[2 + 2 * words:]
        if service == UNCONNECTED_SEND:
            (size,) = struct.unpack_from("<H", data, 2)
            return self.handle(data[4:4 + size])            # route path: any slot is "the CPU"
        if service == MULTIPLE:
            parts = [self.handle(r) for r in split_multiple(data)]
            count = len(parts)
            offsets, offset = [], 2 + 2 * count
            for part in parts:
                offsets.append(offset)
                offset += len(part)
            status = 0x1E if any(p[2] for p in parts) else 0
            return _reply(MULTIPLE, status, struct.pack(f"<H{count}H", count, *offsets)
                          + b"".join(parts))
        try:
            name = parse_path(path)
        except ValueError:
            return _reply(service, 0x04)
        entry = self.tags.get(name)
        if entry is None:
            return _reply(service, 0x04)
        code, value = entry
        if service == READ_TAG:
            if code == STRUCT_TYPE:
                text = str(value).encode("latin-1")[:82]
                return _reply(service, 0, struct.pack("<HHi", STRUCT_TYPE, LOGIX_STRING_HANDLE,
                                                      len(text)) + text.ljust(82, b"\x00"))
            return _reply(service, 0, struct.pack("<H", code)
                          + struct.pack(CIP_TYPES[code][1], value))
        if service == WRITE_TAG:
            if name in self.read_only:
                return _reply(service, 0x0E)
            wcode, _ = struct.unpack_from("<HH", data)
            if wcode != code:
                return _reply(service, 0xFF)              # Logix: type mismatch
            (new,) = struct.unpack_from(CIP_TYPES[code][1], data, 4)
            entry[1] = (new != 0) if CIP_TYPES[code][0] == "BOOL" else new
            return _reply(service, 0)
        return _reply(service, 0x08)


class SimulatedEnipDevice:
    """Creates the device's tags in the simulated controller and animates the read-only ones."""

    def __init__(self, server: EnipSimServer, device: EnipDevice) -> None:
        self.server = server
        self.device = device
        self._tags = {t.name: t for t in device.tags}
        self._t = 0.0
        for tag in device.tags:
            if tag.bit is not None:
                server.tags.setdefault(tag.tag, [0xC4, 0])           # the DINT behind the bit
            elif tag.data_type is DataType.STRING:
                server.tags[tag.tag] = [STRUCT_TYPE, tag.name.upper()]
            else:
                server.tags[tag.tag] = [SIM_TYPE[tag.data_type], 0]
                self.set(tag.name, False if tag.data_type is DataType.BOOL else animated(tag, 0))

    def set(self, name: str, value: Value) -> None:
        tag = self._tags[name]
        entry = self.server.tags[tag.tag]
        if tag.bit is not None:
            mask = 1 << tag.bit
            entry[1] = (entry[1] | mask) if value else (entry[1] & ~mask)
        elif tag.data_type in (DataType.BOOL, DataType.STRING):
            entry[1] = value
        else:
            entry[1] = to_raw(tag, value)

    def get(self, name: str) -> Any:
        return self.server.tags[self._tags[name].tag][1]

    def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for tag in self.device.tags:
            if tag.access is Access.READ_WRITE or tag.data_type in (DataType.BOOL,
                                                                     DataType.STRING):
                continue
            self.set(tag.name, animated(tag, self._t))
