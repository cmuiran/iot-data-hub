"""An Omron PLC answering FINS/TCP and FINS/UDP (memory area read / write) for tests and
local development. One server = one PLC; its memory is a :class:`WordMemory`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct

from hub.drivers.fins import (
    AREA_OF_CODE,
    BIT_AREAS,
    TCP_FRAME,
    TCP_HANDSHAKE,
    build_frame,
    read_tcp_message,
    tcp_message,
)
from hub.models import FinsDevice, FinsTag, WordAddress
from simulators.plc_memory import SimulatedWordDevice, WordMemory

log = logging.getLogger(__name__)
SERVER_NODE = 1


class FinsSimServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1", transport: str = "tcp") -> None:
        self.host, self.port, self.transport = host, port, transport
        self.memory = WordMemory()
        self.protected: set[str] = set()      # areas that answer "protected" to writes
        self.unreadable: set[str] = set()     # areas that answer "address range exceeded"
        self._server: asyncio.base_events.Server | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._next_client = 10
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if self.transport == "tcp":
            self._server = await asyncio.start_server(self._client, self.host, self.port)
            self.port = self._server.sockets[0].getsockname()[1]
        else:
            loop = asyncio.get_running_loop()
            sim = self

            class _Proto(asyncio.DatagramProtocol):
                def connection_made(self, transport: asyncio.BaseTransport) -> None:
                    self.transport = transport  # type: ignore[assignment]

                def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                    reply = sim.handle_frame(data)
                    if reply is not None:
                        self.transport.sendto(reply, addr)  # type: ignore[attr-defined]

            self._udp, _ = await loop.create_datagram_endpoint(
                _Proto, local_addr=(self.host, self.port))
            self.port = self._udp.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):           # a PLC that goes away drops them
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
        if self._udp is not None:
            self._udp.close()

    async def __aenter__(self) -> FinsSimServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                command, _, payload = await read_tcp_message(reader)
                if command == TCP_HANDSHAKE:
                    node = self._next_client
                    self._next_client += 1
                    writer.write(tcp_message(1, struct.pack(">II", node, SERVER_NODE)))
                elif command == TCP_FRAME:
                    reply = self.handle_frame(payload)
                    if reply is not None:
                        writer.write(tcp_message(TCP_FRAME, reply))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception:  # noqa: BLE001 - a broken request closes only this connection
            log.exception("FINS simulator")
        finally:
            self._writers.discard(writer)
            writer.close()

    def handle_frame(self, frame: bytes) -> bytes | None:
        if len(frame) < 12:
            return None
        dna, da1, da2, sna, sa1, sa2, sid, mrc, src = (frame[3], frame[4], frame[5], frame[6],
                                                       frame[7], frame[8], frame[9], frame[10],
                                                       frame[11])
        body = frame[12:]

        def answer(end: int, data: bytes = b"") -> bytes:
            return build_frame(sna, sa1, sa2, dna, da1, da2, sid, mrc, src,
                               struct.pack(">H", end) + data, response=True)

        if (mrc, src) not in ((0x01, 0x01), (0x01, 0x02)) or len(body) < 6:
            return answer(0x0401)                           # service not supported
        code, word, bit, count = struct.unpack(">BHBH", body[:6])
        area = AREA_OF_CODE.get(code)
        if area is None:
            return answer(0x1101)
        if word + count > 32768:
            return answer(0x1103)
        is_bit = code in BIT_AREAS.values()
        if src == 0x01 and area in self.unreadable:
            return answer(0x1103)
        if src == 0x01:                                     # read
            if is_bit:
                words = self.memory.read(area, word, 1)
                return answer(0, bytes([(words[0] >> bit) & 1]))
            return answer(0, struct.pack(f">{count}H", *self.memory.read(area, word, count)))
        if area in self.protected:
            return answer(0x2002)
        data = body[6:]
        if is_bit:
            for i in range(count):
                self.memory.set_bit(area, word, bit + i, bool(data[i]))
        else:
            if len(data) != 2 * count:
                return answer(0x1002)
            self.memory.write(area, word, list(struct.unpack(f">{count}H", data)))
        return answer(0)


def _locate(tag: FinsTag) -> tuple[WordAddress, bool]:
    return tag.parsed, False


class SimulatedFinsDevice(SimulatedWordDevice):
    def __init__(self, server: FinsSimServer, device: FinsDevice) -> None:
        super().__init__(server.memory, device, device.connection.word_order, _locate)
