"""IEC 60870-5-104 driver (controlling station).

After connecting: STARTDT, general interrogation (C_IC, QOI 20) and counter interrogation
(C_CI), both repeated at the configured intervals. Values arrive pushed (spontaneous,
periodic, interrogation responses) and are emitted as they come; IV quality makes a sample
bad, NT/SB/BL/OV uncertain.

Commands (only via the control-safety layer): single / double commands and setpoints
(normalized, scaled, float), optionally select-before-operate; the driver waits for the
positive activation confirmation (ACT_CON) and fails on a negative one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from hub.codec import Value
from hub.drivers.base import Driver, DriverError
from hub.drivers.iec104_proto import (
    BL,
    C_CI_NA_1,
    C_IC_NA_1,
    COMMAND_TYPE,
    COT_ACT,
    COT_ACTCON,
    COT_ACTTERM,
    COT_NAMES,
    IV,
    MONITOR_TYPES,
    NT,
    OV,
    SB,
    STARTDT_ACT,
    STARTDT_CON,
    Asdu,
    Endpoint,
    InfoObject,
)
from hub.models import DataType, Iec104Device, Iec104Tag, Quality, QualityReason, TagBase

log = logging.getLogger(__name__)


def quality_of(bits: int) -> Quality | None:
    if bits & IV:
        return Quality.BAD
    if bits & (NT | SB | BL | OV):
        return Quality.UNCERTAIN
    return None


def to_tag_value(tag: Iec104Tag, value: object) -> Value | None:
    if tag.type == "double" and tag.data_type is DataType.BOOL:
        return {1: False, 2: True}.get(int(value))      # 0 / 3 = intermediate / faulty
    if tag.data_type is DataType.BOOL:
        return bool(value)
    if tag.data_type in (DataType.FLOAT32, DataType.FLOAT64):
        return float(value)                             # type: ignore[arg-type]
    return int(value)                                   # type: ignore[call-overload]


class Iec104Driver(Driver[Iec104Device]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._by_ioa: dict[int, list[Iec104Tag]] = {}
        for tag in self.device.tags:
            self._by_ioa.setdefault(tag.ioa, []).append(tag)
        self._endpoint: Endpoint | None = None
        self._latest: dict[str, Value | None] = {}
        self._updated: dict[str, float] = {}
        self._changed = asyncio.Event()
        self._confirm: dict[tuple[int, int], asyncio.Future[bool]] = {}
        self._interrogated = asyncio.Event()

    # --- connection ---------------------------------------------------------------------------

    async def _connect(self) -> None:
        c = self.device.connection
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(c.host, c.port),
                                                    c.t0_s)
        except (OSError, TimeoutError) as exc:
            raise DriverError(f"cannot connect to {c.host}:{c.port}: {exc}") from exc
        self._endpoint = Endpoint(reader, writer, k=c.k, w=c.w, t1=c.t1_s, t2=c.t2_s,
                                  t3=c.t3_s, on_asdu=self._on_asdu)

    async def _disconnect(self) -> None:
        endpoint, self._endpoint = self._endpoint, None
        if endpoint is not None:
            await endpoint.close()
        for fut in self._confirm.values():
            if not fut.done():
                fut.set_exception(DriverError("connection closed"))
        self._confirm.clear()

    async def _run_connected(self) -> None:
        endpoint = self._require()
        reader = asyncio.create_task(endpoint.run(), name=f"iec104:{self.device.device_id}")
        try:
            await endpoint.request_u(STARTDT_ACT, STARTDT_CON)
            await self._interrogate(C_IC_NA_1, 20)
            await self._interrogate(C_CI_NA_1, 5)
            await self.mark_seen()
            await self._supervise_link(reader)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _supervise_link(self, reader: asyncio.Task[None]) -> None:
        c = self.device.connection
        loop = asyncio.get_running_loop()
        next_gi = loop.time() + c.interrogation_interval_s if c.interrogation_interval_s else None
        next_ci = loop.time() + c.counter_interrogation_interval_s \
            if c.counter_interrogation_interval_s else None
        last_frames = -1
        last_activity = loop.time()
        while not self._stopping.is_set():
            done, _ = await asyncio.wait({reader}, timeout=1.0)
            if done:
                reader.result()                             # raises LinkClosed
                raise DriverError("IEC 104 link ended")
            now = loop.time()
            endpoint = self._require()
            if endpoint.frames_rx != last_frames:
                last_frames, last_activity = endpoint.frames_rx, now
                await self.mark_seen()
            if c.stale_after_s and now - last_activity > c.stale_after_s:
                raise DriverError(f"nothing received for {c.stale_after_s:g} s")
            if next_gi is not None and now >= next_gi:
                next_gi = now + c.interrogation_interval_s
                await self._interrogate(C_IC_NA_1, 20)
            if next_ci is not None and now >= next_ci:
                next_ci = now + c.counter_interrogation_interval_s
                await self._interrogate(C_CI_NA_1, 5)

    def _require(self) -> Endpoint:
        if self._endpoint is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        return self._endpoint

    async def _interrogate(self, type_id: int, qualifier: int) -> None:
        if type_id == C_CI_NA_1 and not any(t.type == "integrated" for t in self.device.tags):
            return
        try:
            await self._activate(Asdu(type_id, COT_ACT, self.device.connection.common_address,
                                      [InfoObject(0, qualifier)],
                                      oa=self.device.connection.originator_address))
        except DriverError as exc:
            log.warning("%s: interrogation: %s", self.device.device_id, exc)

    async def _activate(self, asdu: Asdu) -> None:
        """Send an activation and wait for its (positive) ACT_CON."""
        key = (asdu.type_id, asdu.objects[0].ioa)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._confirm[key] = fut
        try:
            await self._require().send_asdu(asdu)
            positive = await asyncio.wait_for(fut, self.device.connection.t1_s)
        except TimeoutError as exc:
            raise DriverError(f"type {asdu.type_id} IOA {key[1]}: no activation "
                              "confirmation") from exc
        finally:
            self._confirm.pop(key, None)
        if not positive:
            raise DriverError(f"type {asdu.type_id} IOA {key[1]}: negative confirmation")

    # --- incoming data -----------------------------------------------------------------------

    async def _on_asdu(self, asdu: Asdu) -> None:
        c = self.device.connection
        if asdu.cot == COT_ACTCON or asdu.cot in COT_NAMES:
            ioa = asdu.objects[0].ioa if asdu.objects else 0
            fut = self._confirm.get((asdu.type_id, ioa))
            if fut is not None and not fut.done():
                fut.set_result(asdu.cot == COT_ACTCON and not asdu.negative)
            if asdu.cot in COT_NAMES:
                log.warning("%s: outstation answered %s for type %d IOA %d",
                            self.device.device_id, COT_NAMES[asdu.cot], asdu.type_id, ioa)
            return
        if asdu.cot == COT_ACTTERM or asdu.type_id not in MONITOR_TYPES:
            return
        if asdu.ca != c.common_address:
            log.debug("%s: ASDU for common address %d ignored", self.device.device_id, asdu.ca)
            return
        kind = MONITOR_TYPES[asdu.type_id][0]
        samples = []
        loop = asyncio.get_running_loop()
        for obj in asdu.objects:
            for tag in self._by_ioa.get(obj.ioa, []):
                if tag.type != kind:
                    continue
                value = to_tag_value(tag, obj.value)
                quality = quality_of(obj.quality) if value is not None else Quality.BAD
                self._latest[tag.name] = value if quality is not Quality.BAD else None
                self._updated[tag.name] = loop.time()
                stamp = obj.time if obj.time is not None and obj.time <= datetime.now(UTC) \
                    else None
                samples.append(self.make_sample(tag, value if quality is not Quality.BAD
                                                else None, quality, stamp,
                                                QualityReason.DEVICE_REPORTED
                                                if quality is not Quality.GOOD else None))
        if samples:
            self._changed.set()
            await self.emit(samples)

    # --- reads / writes ----------------------------------------------------------------------

    async def _read_raw(self, tag: TagBase) -> Value:
        value = self._latest.get(tag.name)
        if value is None:
            raise DriverError(f"{tag.name}: no valid value received yet")
        return value

    async def read_back(self, name: str, timeout_s: float = 5.0) -> Value:
        """After a command: the value the outstation reports next (spontaneous or return
        information), or the last known value when nothing comes within the timeout."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        tag = self.tag(name)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                while self._updated.get(name, 0) < start:
                    self._changed.clear()
                    await self._changed.wait()
        return await self.read_tag(tag.name)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        assert isinstance(tag, Iec104Tag)
        type_id = COMMAND_TYPE[tag.type]
        value: object = raw
        if tag.type == "double":
            value = (2 if raw else 1) if tag.data_type is DataType.BOOL else int(raw)
        ioa = tag.command_ioa or tag.ioa
        c = self.device.connection

        def command(select: bool) -> Asdu:
            return Asdu(type_id, COT_ACT, c.common_address,
                        [InfoObject(ioa, value, select=select)], oa=c.originator_address)

        if tag.select_before_operate:
            await self._activate(command(True))
        await self._activate(command(False))
