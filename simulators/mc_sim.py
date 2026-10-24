"""A MELSEC PLC answering MC protocol 3E binary frames (batch read / write, word and bit
units) over TCP or UDP, for tests and local development."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct

from hub.drivers.mc import (
    BIT_UNITS,
    READ,
    REQUEST_HEADER,
    RESPONSE_HEADER,
    WORD_UNITS,
    WRITE,
    pack_bits,
    unpack_bits,
)
from hub.models import MC_DEVICES, McDevice, McTag, WordAddress
from simulators.plc_memory import SimulatedWordDevice, WordMemory

log = logging.getLogger(__name__)
CODE_TO_DEVICE = {code: name for name, (code, _, _) in MC_DEVICES.items()}


class McSimServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1", transport: str = "tcp") -> None:
        self.host, self.port, self.transport = host, port, transport
        self.memory = WordMemory()
        self.unreadable: set[str] = set()
        self._server: asyncio.base_events.Server | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if self.transport == "tcp":
            self._server = await asyncio.start_server(self._client, self.host, self.port)
            self.port = self._server.sockets[0].getsockname()[1]
            return
        sim = self

        class _Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport  # type: ignore[assignment]

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                if data[:2] == REQUEST_HEADER and len(data) >= 9:
                    self.transport.sendto(sim.handle(data[:7], data[9:]), addr)  # type: ignore[attr-defined]

        self._udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            _Proto, local_addr=(self.host, self.port))
        self.port = self._udp.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
        if self._udp is not None:
            self._udp.close()

    async def __aenter__(self) -> McSimServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                head = await reader.readexactly(9)
                (length,) = struct.unpack("<H", head[7:9])
                body = await reader.readexactly(length)
                if head[:2] != REQUEST_HEADER:
                    break
                writer.write(self.handle(head[:7], body))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def handle(self, head: bytes, body: bytes) -> bytes:
        route = head[2:7]

        def answer(code: int, data: bytes = b"") -> bytes:
            payload = struct.pack("<H", code) + data
            return RESPONSE_HEADER + route + struct.pack("<H", len(payload)) + payload

        if len(body) < 12:
            return answer(0xC061)
        _, command, sub = struct.unpack("<HHH", body[:6])
        number = int.from_bytes(body[6:9], "little")
        device = CODE_TO_DEVICE.get(body[9])
        (points,) = struct.unpack("<H", body[10:12])
        if device is None:
            return answer(0xC05B)
        if command not in (READ, WRITE) or sub not in (WORD_UNITS, BIT_UNITS):
            return answer(0xC059)
        if command == READ and device in self.unreadable:
            return answer(0xC056)
        is_word_device = MC_DEVICES[device][1]
        if sub == BIT_UNITS and is_word_device:
            return answer(0xC059)
        if command == READ:
            values = self.memory.read(device, number, points)
            if sub == WORD_UNITS and not is_word_device:     # 16 bit points per word
                words = []
                for w in range(points):
                    bits = self.memory.read(device, number + 16 * w, 16)
                    words.append(sum((b & 1) << i for i, b in enumerate(bits)))
                return answer(0, struct.pack(f"<{points}H", *words))
            if sub == BIT_UNITS:
                return answer(0, pack_bits(values))
            return answer(0, struct.pack(f"<{points}H", *values))
        data = body[12:]
        if sub == BIT_UNITS:
            self.memory.write(device, number, unpack_bits(data, points))
        else:
            if len(data) != 2 * points:
                return answer(0xC061)
            self.memory.write(device, number, list(struct.unpack(f"<{points}H", data)))
        return answer(0)


def _locate(tag: McTag) -> tuple[WordAddress, bool]:
    return tag.parsed, tag.is_bit_device


class SimulatedMcDevice(SimulatedWordDevice):
    def __init__(self, server: McSimServer, device: McDevice) -> None:
        super().__init__(server.memory, device, device.connection.word_order, _locate)
