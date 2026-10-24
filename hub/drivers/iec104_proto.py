"""IEC 60870-5-104: APCI link handling and the ASDU codec, shared by the driver (controlling
station) and the simulator (controlled station / outstation).

APCI: 0x68, length, 4 control octets. I-format carries an ASDU with send/receive sequence
numbers (15 bit); S-format acknowledges; U-format is STARTDT / STOPDT / TESTFR (act / con).
The endpoint acknowledges after ``w`` I-frames or ``t2`` seconds, stops sending when ``k``
frames are unacknowledged, tests an idle link after ``t3`` and gives up when an
acknowledgement or a TESTFR confirmation does not come within ``t1``.

ASDU (2-byte COT, 2-byte common address, 3-byte IOA): type id, variable structure qualifier
(SQ bit + number), cause of transmission (+ P/N and test bits), originator address, common
address, information objects. Time tags are CP56Time2a.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from hub.drivers.base import DriverError

log = logging.getLogger(__name__)

START = 0x68
STARTDT_ACT, STARTDT_CON = 0x07, 0x0B
STOPDT_ACT, STOPDT_CON = 0x13, 0x23
TESTFR_ACT, TESTFR_CON = 0x43, 0x83

# cause of transmission
COT_PERIODIC, COT_BACKGROUND, COT_SPONTANEOUS, COT_REQUEST = 1, 2, 3, 5
COT_ACT, COT_ACTCON, COT_DEACT, COT_DEACTCON, COT_ACTTERM = 6, 7, 8, 9, 10
COT_RETREM, COT_RETLOC, COT_INROGEN, COT_REQCOGEN = 11, 12, 20, 37
COT_UNKNOWN_TYPE, COT_UNKNOWN_CAUSE, COT_UNKNOWN_CA, COT_UNKNOWN_IOA = 44, 45, 46, 47
COT_NAMES = {44: "unknown type", 45: "unknown cause", 46: "unknown common address",
             47: "unknown information object address"}

C_SC_NA_1, C_DC_NA_1, C_SE_NA_1, C_SE_NB_1, C_SE_NC_1 = 45, 46, 48, 49, 50
C_IC_NA_1, C_CI_NA_1 = 100, 101

# monitoring type id -> (kind, element size without time, with CP56 time?)
MONITOR_TYPES: dict[int, tuple[str, int, bool]] = {
    1: ("single", 1, False), 30: ("single", 1, True),
    3: ("double", 1, False), 31: ("double", 1, True),
    5: ("step", 2, False), 32: ("step", 2, True),
    7: ("bitstring", 5, False), 33: ("bitstring", 5, True),
    9: ("normalized", 3, False), 21: ("normalized", 2, False), 34: ("normalized", 3, True),
    11: ("scaled", 3, False), 35: ("scaled", 3, True),
    13: ("float", 5, False), 36: ("float", 5, True),
    15: ("integrated", 5, False), 37: ("integrated", 5, True),
}
# the type an outstation (simulator) sends for each kind: without and with time tag
SEND_TYPE = {"single": (1, 30), "double": (3, 31), "step": (5, 32), "bitstring": (7, 33),
             "normalized": (9, 34), "scaled": (11, 35), "float": (13, 36),
             "integrated": (15, 37)}
COMMAND_TYPE = {"single": C_SC_NA_1, "double": C_DC_NA_1, "normalized": C_SE_NA_1,
                "scaled": C_SE_NB_1, "float": C_SE_NC_1}
COMMAND_SIZE = {C_SC_NA_1: 1, C_DC_NA_1: 1, C_SE_NA_1: 3, C_SE_NB_1: 3, C_SE_NC_1: 5,
                C_IC_NA_1: 1, C_CI_NA_1: 1}

# quality descriptor bits
IV, NT, SB, BL, OV = 0x80, 0x40, 0x20, 0x10, 0x01


@dataclass
class InfoObject:
    ioa: int
    value: Any                  # bool / int / float, or the raw element for commands
    quality: int = 0            # IV NT SB BL OV bits (or command qualifier)
    time: datetime | None = None
    select: bool = False        # commands: S/E bit


@dataclass
class Asdu:
    type_id: int
    cot: int
    ca: int
    objects: list[InfoObject] = field(default_factory=list)
    negative: bool = False
    test: bool = False
    oa: int = 0
    sq: bool = False


# --- CP56Time2a ---------------------------------------------------------------------------


def encode_cp56(t: datetime) -> bytes:
    t = t.astimezone(UTC)
    ms = t.second * 1000 + t.microsecond // 1000
    return struct.pack("<HBBBBB", ms, t.minute, t.hour,
                       t.day | (t.isoweekday() << 5), t.month, t.year % 100)


def decode_cp56(data: bytes) -> datetime | None:
    ms, minute, hour, day, month, year = struct.unpack("<HBBBBB", data[:7])
    if minute & 0x80:                                    # IV: time not valid
        return None
    try:
        return datetime(2000 + (year & 0x7F), month & 0x0F, day & 0x1F, hour & 0x1F,
                         minute & 0x3F, tzinfo=UTC) + timedelta(milliseconds=ms)
    except ValueError:
        return None


# --- ASDU codec ---------------------------------------------------------------------------


def _decode_element(kind: str, data: bytes) -> tuple[Any, int]:
    """(value, quality bits) of one monitoring element."""
    if kind == "single":
        return bool(data[0] & 1), data[0] & 0xF0
    if kind == "double":
        return data[0] & 0x03, data[0] & 0xF0
    if kind == "step":
        raw = data[0] & 0x7F
        return raw - 128 if raw & 0x40 else raw, data[1] & 0xF1
    if kind == "bitstring":
        return struct.unpack_from("<I", data)[0], data[4] & 0xF1
    if kind == "normalized":
        value = struct.unpack_from("<h", data)[0] / 32768
        return value, (data[2] & 0xF1) if len(data) > 2 else 0
    if kind == "scaled":
        return struct.unpack_from("<h", data)[0], data[2] & 0xF1
    if kind == "float":
        return struct.unpack_from("<f", data)[0], data[4] & 0xF1
    if kind == "integrated":
        seq = data[4]
        return struct.unpack_from("<i", data)[0], IV if seq & 0x80 else 0
    raise ValueError(kind)


def _encode_element(kind: str, value: Any, quality: int) -> bytes:
    if kind == "single":
        return bytes([(1 if value else 0) | (quality & 0xF0)])
    if kind == "double":
        return bytes([(int(value) & 3) | (quality & 0xF0)])
    if kind == "step":
        return bytes([int(value) & 0x7F, quality & 0xF1])
    if kind == "bitstring":
        return struct.pack("<IB", int(value) & 0xFFFFFFFF, quality & 0xF1)
    if kind == "normalized":
        raw = max(-32768, min(32767, round(float(value) * 32768)))
        return struct.pack("<hB", raw, quality & 0xF1)
    if kind == "scaled":
        return struct.pack("<hB", int(value), quality & 0xF1)
    if kind == "float":
        return struct.pack("<fB", float(value), quality & 0xF1)
    if kind == "integrated":
        return struct.pack("<iB", int(value), 0x80 if quality & IV else 0)
    raise ValueError(kind)


def _decode_command(type_id: int, data: bytes) -> tuple[Any, int, bool]:
    """(value, qualifier, select) of a command element."""
    if type_id == C_SC_NA_1:
        return bool(data[0] & 1), (data[0] >> 2) & 0x1F, bool(data[0] & 0x80)
    if type_id == C_DC_NA_1:
        return data[0] & 3, (data[0] >> 2) & 0x1F, bool(data[0] & 0x80)
    if type_id in (C_SE_NA_1, C_SE_NB_1):
        raw = struct.unpack_from("<h", data)[0]
        value = raw / 32768 if type_id == C_SE_NA_1 else raw
        return value, data[2] & 0x7F, bool(data[2] & 0x80)
    if type_id == C_SE_NC_1:
        return struct.unpack_from("<f", data)[0], data[4] & 0x7F, bool(data[4] & 0x80)
    if type_id in (C_IC_NA_1, C_CI_NA_1):
        return data[0], 0, False
    raise ValueError(type_id)


def _encode_command(type_id: int, value: Any, select: bool) -> bytes:
    se = 0x80 if select else 0
    if type_id == C_SC_NA_1:
        return bytes([(1 if value else 0) | se])
    if type_id == C_DC_NA_1:
        return bytes([(int(value) & 3) | se])
    if type_id == C_SE_NA_1:
        return struct.pack("<hB", max(-32768, min(32767, round(float(value) * 32768))), se)
    if type_id == C_SE_NB_1:
        return struct.pack("<hB", int(value), se)
    if type_id == C_SE_NC_1:
        return struct.pack("<fB", float(value), se)
    if type_id in (C_IC_NA_1, C_CI_NA_1):
        return bytes([int(value)])
    raise ValueError(type_id)


def encode_asdu(a: Asdu) -> bytes:
    count = len(a.objects)
    cot = (a.cot & 0x3F) | (0x40 if a.negative else 0) | (0x80 if a.test else 0)
    out = bytearray([a.type_id, (0x80 if a.sq else 0) | count, cot, a.oa])
    out += struct.pack("<H", a.ca)
    monitor = MONITOR_TYPES.get(a.type_id)
    for i, obj in enumerate(a.objects):
        if not a.sq or i == 0:
            out += obj.ioa.to_bytes(3, "little")
        if monitor is not None:
            kind, _, timed = monitor
            out += _encode_element(kind, obj.value, obj.quality)
            if timed:
                out += encode_cp56(obj.time or datetime.now(UTC))
        else:
            out += _encode_command(a.type_id, obj.value, obj.select)
    return bytes(out)


def decode_asdu(data: bytes) -> Asdu:
    if len(data) < 6:
        raise DriverError("ASDU too short")
    type_id, vsq, cot, oa = data[0], data[1], data[2], data[3]
    (ca,) = struct.unpack_from("<H", data, 4)
    a = Asdu(type_id, cot & 0x3F, ca, negative=bool(cot & 0x40), test=bool(cot & 0x80), oa=oa,
             sq=bool(vsq & 0x80))
    count = vsq & 0x7F
    pos = 6
    monitor = MONITOR_TYPES.get(type_id)
    if monitor is not None:
        kind, size, timed = monitor
        width = size + (7 if timed else 0)
    elif type_id in COMMAND_SIZE:
        kind, width, timed = "", COMMAND_SIZE[type_id], False
    else:
        return a                                        # unknown type: header only
    ioa = 0
    for i in range(count):
        if not a.sq or i == 0:
            ioa = int.from_bytes(data[pos:pos + 3], "little")
            pos += 3
        else:
            ioa += 1
        element = data[pos:pos + width]
        if len(element) < width:
            raise DriverError(f"ASDU type {type_id} truncated")
        pos += width
        if monitor is not None:
            value, quality = _decode_element(kind, element)
            time = decode_cp56(element[size:]) if timed else None
            a.objects.append(InfoObject(ioa, value, quality, time))
        else:
            value, qualifier, select = _decode_command(type_id, element)
            a.objects.append(InfoObject(ioa, value, qualifier, select=select))
    return a


# --- link endpoint -----------------------------------------------------------------------


class LinkClosed(DriverError):
    pass


class Endpoint:
    """One IEC 104 TCP connection, either side. ``on_asdu`` receives every I-frame's ASDU."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 k: int = 12, w: int = 8, t1: float = 15, t2: float = 10, t3: float = 20,
                 on_asdu: Callable[[Asdu], Awaitable[None]] | None = None,
                 outstation: bool = False) -> None:
        self.reader, self.writer = reader, writer
        self.k, self.w, self.t1, self.t2, self.t3 = k, w, t1, t2, t3
        self.on_asdu = on_asdu
        self.outstation = outstation
        self.started = asyncio.Event()                  # STARTDT confirmed / received
        self._vs = self._vr = 0                         # send / receive state variables
        self._ack = 0                                   # our frames the peer has acknowledged
        self._unacked_rx = 0
        self._sent_times: dict[int, float] = {}
        self._rx_first: float | None = None
        self._last_rx = 0.0
        self._testfr_since: float | None = None
        self._window = asyncio.Condition()
        self._u_waiters: dict[int, asyncio.Future[None]] = {}
        self._closed = False
        self.frames_rx = 0

    # sending ------------------------------------------------------------------------------

    def _write(self, frame: bytes) -> None:
        if self._closed:
            raise LinkClosed("connection closed")
        self.writer.write(frame)

    async def send_u(self, code: int) -> None:
        self._write(bytes([START, 4, code, 0, 0, 0]))
        await self.writer.drain()

    async def request_u(self, act: int, con: int) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._u_waiters[con] = fut
        await self.send_u(act)
        try:
            await asyncio.wait_for(fut, self.t1)
        except TimeoutError as exc:
            raise LinkClosed(f"no confirmation of U-frame {act:#04x} within t1") from exc
        finally:
            self._u_waiters.pop(con, None)

    async def send_asdu(self, asdu: Asdu) -> None:
        loop = asyncio.get_running_loop()
        async with self._window:
            await asyncio.wait_for(
                self._window.wait_for(lambda: self._closed or
                                      (self._vs - self._ack) % 32768 < self.k), self.t1)
        if self._closed:
            raise LinkClosed("connection closed")
        payload = encode_asdu(asdu)
        control = struct.pack("<HH", (self._vs << 1) & 0xFFFE, (self._vr << 1) & 0xFFFE)
        self._write(bytes([START, 4 + len(payload)]) + control + payload)
        self._sent_times[self._vs] = loop.time()
        self._vs = (self._vs + 1) % 32768
        self._unacked_rx = 0                            # an I-frame acknowledges too
        self._rx_first = None
        await self.writer.drain()

    async def _send_s(self) -> None:
        self._write(bytes([START, 4, 0x01, 0x00]) + struct.pack("<H", (self._vr << 1) & 0xFFFE))
        self._unacked_rx = 0
        self._rx_first = None
        await self.writer.drain()

    # receiving ----------------------------------------------------------------------------

    async def _peer_ack(self, nr: int) -> None:
        for seq in list(self._sent_times):
            if (nr - seq - 1) % 32768 < 32768 // 2:
                self._sent_times.pop(seq, None)
        self._ack = nr
        async with self._window:
            self._window.notify_all()

    async def run(self) -> None:
        """Read frames until the connection fails; raises LinkClosed."""
        timer = asyncio.create_task(self._timers())
        try:
            while True:
                head = await self.reader.readexactly(2)
                if head[0] != START or head[1] < 4:
                    raise LinkClosed(f"bad APCI start {head.hex()}")
                body = await self.reader.readexactly(head[1])
                self._last_rx = asyncio.get_running_loop().time()
                self._testfr_since = None
                self.frames_rx += 1
                await self._frame(body)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            raise LinkClosed(f"connection lost: {exc}") from exc
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
            await self.close()

    async def _frame(self, body: bytes) -> None:
        c0 = body[0]
        if c0 & 0x01 == 0:                              # I-format
            ns = struct.unpack_from("<H", body, 0)[0] >> 1
            nr = struct.unpack_from("<H", body, 2)[0] >> 1
            if ns != self._vr:
                raise LinkClosed(f"sequence error: got N(S)={ns}, expected {self._vr}")
            self._vr = (self._vr + 1) % 32768
            await self._peer_ack(nr)
            self._unacked_rx += 1
            if self._rx_first is None:
                self._rx_first = asyncio.get_running_loop().time()
            if self._unacked_rx >= self.w:
                await self._send_s()
            if self.on_asdu is not None:
                await self.on_asdu(decode_asdu(body[4:]))
        elif c0 & 0x03 == 0x01:                         # S-format
            await self._peer_ack(struct.unpack_from("<H", body, 2)[0] >> 1)
        else:                                           # U-format
            if c0 == TESTFR_ACT:
                await self.send_u(TESTFR_CON)
            elif c0 == STARTDT_ACT and self.outstation:
                await self.send_u(STARTDT_CON)
                self.started.set()
            elif c0 == STOPDT_ACT and self.outstation:
                self.started.clear()
                await self.send_u(STOPDT_CON)
            elif c0 in self._u_waiters:
                if not self._u_waiters[c0].done():
                    self._u_waiters[c0].set_result(None)
                if c0 == STARTDT_CON:
                    self.started.set()

    async def _timers(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_rx = loop.time()
        while True:
            await asyncio.sleep(0.2)
            now = loop.time()
            if self._sent_times and now - min(self._sent_times.values()) > self.t1:
                log.warning("IEC 104: no acknowledgement within t1, closing")
                await self.close()
                return
            if self._rx_first is not None and now - self._rx_first >= self.t2:
                await self._send_s()
            if self._testfr_since is not None and now - self._testfr_since > self.t1:
                log.warning("IEC 104: no TESTFR confirmation within t1, closing")
                await self.close()
                return
            if self._testfr_since is None and now - self._last_rx > self.t3:
                self._testfr_since = now
                await self.send_u(TESTFR_ACT)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._window:
            self._window.notify_all()
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()
