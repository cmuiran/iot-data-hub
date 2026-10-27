"""Driver of a calculated (virtual) device: no connection, values come from the calc engine.

The hub's :class:`hub.calc.engine.CalcEngine` computes every calculated tag and emits the
results; this driver only reports the device as online and answers reads from the engine's
latest results. Calculated tags are read-only, so writes are always refused.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from hub.codec import Value
from hub.drivers.base import Clock, Driver, DriverError, SampleSink, StatusSink, WriteNotPermitted
from hub.drivers.base import utcnow as _utcnow
from hub.models import CalcDevice, TagBase

if TYPE_CHECKING:
    from hub.calc.engine import CalcEngine

HEARTBEAT_S = 30.0


class CalcDriver(Driver[CalcDevice]):  # type: ignore[type-var]
    def __init__(self, device: CalcDevice, on_samples: SampleSink, on_status: StatusSink, *,
                 engine: CalcEngine | None, clock: Clock = _utcnow) -> None:
        super().__init__(device, on_samples, on_status, clock=clock)  # type: ignore[arg-type]
        self.engine = engine

    async def _connect(self) -> None:
        if self.engine is None:
            raise DriverError("calculation engine not running")

    async def _disconnect(self) -> None:
        return None

    async def _run_connected(self) -> None:
        while not self._stopping.is_set():
            await self.mark_seen()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), HEARTBEAT_S)

    async def _read_raw(self, tag: TagBase) -> Value:
        sample = self.engine.current(self.device.tag_path(tag.name)) if self.engine else None
        if sample is None or sample.value is None:
            raise DriverError(f"{self.device.tag_path(tag.name)}: no result yet")
        return sample.value

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        raise WriteNotPermitted("calculated tags are read-only")
