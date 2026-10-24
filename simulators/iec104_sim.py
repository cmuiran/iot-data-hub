"""An IEC 60870-5-104 outstation (RTU) for tests and local development.

Answers STARTDT/TESTFR, general and counter interrogation (ACT_CON, the points with COT 20/37,
ACT_TERM), executes single/double commands and setpoints (ACT_CON, then the new value as a
return-information update, ACT_TERM) and sends spontaneous changes with a time tag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hub.codec import Value, to_raw
from hub.drivers.iec104_proto import (
    C_CI_NA_1,
    C_DC_NA_1,
    C_IC_NA_1,
    C_SC_NA_1,
    C_SE_NA_1,
    C_SE_NB_1,
    C_SE_NC_1,
    COT_ACTCON,
    COT_ACTTERM,
    COT_INROGEN,
    COT_REQCOGEN,
    COT_RETREM,
    COT_SPONTANEOUS,
    COT_UNKNOWN_CA,
    COT_UNKNOWN_IOA,
    COT_UNKNOWN_TYPE,
    SEND_TYPE,
    Asdu,
    Endpoint,
    InfoObject,
)
from hub.models import Access, DataType, Iec104Device
from simulators.plc_memory import animated

log = logging.getLogger(__name__)
COMMAND_KIND = {C_SC_NA_1: "single", C_DC_NA_1: "double", C_SE_NA_1: "normalized",
                C_SE_NB_1: "scaled", C_SE_NC_1: "float"}


@dataclass
class Point:
    kind: str
    value: Any
    quality: int = 0


class Iec104SimServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1", common_address: int = 1) -> None:
        self.host, self.port, self.ca = host, port, common_address
        self.points: dict[int, Point] = {}
        self.command_targets: dict[int, int] = {}       # command IOA -> monitor IOA
        self.refuse_commands: set[int] = set()          # IOAs that answer negatively
        self.commands: list[tuple[int, Any, bool]] = []  # (ioa, value, select)
        self.time_tags = True
        self._server: asyncio.base_events.Server | None = None
        self._endpoints: set[Endpoint] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for ep in list(self._endpoints):
                await ep.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    async def __aenter__(self) -> Iec104SimServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ep = Endpoint(reader, writer, outstation=True, t3=60)
        ep.on_asdu = lambda asdu: self._on_asdu(ep, asdu)
        self._endpoints.add(ep)
        try:
            await ep.run()
        except Exception:  # noqa: BLE001 - the controlling station went away
            pass
        finally:
            self._endpoints.discard(ep)

    def _object(self, ioa: int, p: Point, cot: int) -> Asdu:
        type_id = SEND_TYPE[p.kind][1 if self.time_tags and cot == COT_SPONTANEOUS else 0]
        return Asdu(type_id, cot, self.ca, [InfoObject(ioa, p.value, p.quality,
                                                       datetime.now(UTC))])

    async def _on_asdu(self, ep: Endpoint, a: Asdu) -> None:
        reply = Asdu(a.type_id, COT_ACTCON, a.ca, list(a.objects), oa=a.oa)
        if a.ca not in (self.ca, 0xFFFF):
            reply.cot, reply.negative = COT_UNKNOWN_CA, True
            await ep.send_asdu(reply)
            return
        if a.type_id in (C_IC_NA_1, C_CI_NA_1):
            await ep.send_asdu(reply)
            counters = a.type_id == C_CI_NA_1
            cot = COT_REQCOGEN if counters else COT_INROGEN
            for ioa, p in sorted(self.points.items()):
                if (p.kind == "integrated") == counters:
                    await ep.send_asdu(self._object(ioa, p, cot))
            await ep.send_asdu(Asdu(a.type_id, COT_ACTTERM, a.ca, list(a.objects), oa=a.oa))
            return
        if a.type_id not in COMMAND_KIND:
            reply.cot, reply.negative = COT_UNKNOWN_TYPE, True
            await ep.send_asdu(reply)
            return
        obj = a.objects[0]
        target = self.command_targets.get(obj.ioa, obj.ioa)
        point = self.points.get(target)
        if point is None or point.kind != COMMAND_KIND[a.type_id]:
            reply.cot, reply.negative = COT_UNKNOWN_IOA, True
            await ep.send_asdu(reply)
            return
        self.commands.append((obj.ioa, obj.value, obj.select))
        if obj.ioa in self.refuse_commands:
            reply.negative = True
            await ep.send_asdu(reply)
            return
        await ep.send_asdu(reply)
        if not obj.select:
            point.value = obj.value
            await ep.send_asdu(self._object(target, point, COT_RETREM))
            await ep.send_asdu(Asdu(a.type_id, COT_ACTTERM, a.ca, list(a.objects), oa=a.oa))

    async def spontaneous(self, ioa: int) -> None:
        for ep in list(self._endpoints):
            if ep.started.is_set():
                with contextlib.suppress(Exception):
                    await ep.send_asdu(self._object(ioa, self.points[ioa], COT_SPONTANEOUS))


class SimulatedIec104Device:
    def __init__(self, server: Iec104SimServer, device: Iec104Device) -> None:
        self.server = server
        self.device = device
        self._tags = {t.name: t for t in device.tags}
        self._t = 0.0
        server.ca = device.connection.common_address
        for tag in device.tags:
            server.points.setdefault(tag.ioa, Point(tag.type, 0))
            if tag.command_ioa:
                server.command_targets[tag.command_ioa] = tag.ioa
            self.set(tag.name, False if tag.data_type is DataType.BOOL else animated(tag, 0))

    def _raw(self, name: str, value: Value) -> Any:
        tag = self._tags[name]
        if tag.type == "double":
            return (2 if value else 1) if tag.data_type is DataType.BOOL else int(value)
        if tag.data_type is DataType.BOOL:
            return bool(value)
        return to_raw(tag, value)

    def set(self, name: str, value: Value, quality: int = 0) -> None:
        tag = self._tags[name]
        p = self.server.points[tag.ioa]
        p.value, p.quality = self._raw(name, value), quality

    async def change(self, name: str, value: Value, quality: int = 0) -> None:
        self.set(name, value, quality)
        await self.server.spontaneous(self._tags[name].ioa)

    def get(self, name: str) -> Any:
        return self.server.points[self._tags[name].ioa].value

    async def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for tag in self.device.tags:
            if tag.access is Access.READ_WRITE or tag.data_type is DataType.BOOL:
                continue
            await self.change(tag.name, animated(tag, self._t))
