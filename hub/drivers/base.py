"""Common driver interface.

A driver owns the connection to one device: it reads (polls or subscribes), turns protocol data
into :class:`hub.models.Sample` objects and reports connection health. It never stalls other
drivers: every driver runs in its own task and reconnects with exponential backoff.

Writes (CLAUDE.md section 5.1): drivers expose :meth:`Driver.write_tag`, but it only accepts a
:class:`WriteGrant`, which is created by the control-safety layer in ``backend.control`` after
RBAC, range, interlock and audit checks. Code that is not the safety layer must not create one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar

from hub.codec import Value, range_quality, to_engineering
from hub.models import (
    Access,
    ModbusDevice,
    ModbusRtuDevice,
    MqttDevice,
    OpcUaDevice,
    Quality,
    Sample,
    TagBase,
)

log = logging.getLogger(__name__)

AnyDevice = ModbusDevice | ModbusRtuDevice | MqttDevice | OpcUaDevice
D = TypeVar("D", ModbusDevice, ModbusRtuDevice, MqttDevice, OpcUaDevice)


class DeviceState(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"        # connected, but reads fail (e.g. Modbus exceptions)
    DISABLED = "disabled"


@dataclass(frozen=True)
class DeviceStatus:
    device_id: str
    state: DeviceState = DeviceState.UNKNOWN
    last_seen: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


SampleSink = Callable[[list[Sample]], Awaitable[None]]
StatusSink = Callable[[DeviceStatus], Awaitable[None]]
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


class DriverError(Exception):
    """A read or write could not be completed (connection lost, protocol exception, timeout)."""


class WriteNotPermitted(DriverError):
    """write_tag() was called without a grant or for a tag that is not read_write."""


@dataclass(frozen=True)
class WriteGrant:
    """Proof that the control-safety layer approved this write. Created only by backend.control."""

    request_id: str
    tag_path: str
    raw_value: Value


@dataclass
class Backoff:
    initial_s: float = 1.0
    max_s: float = 60.0
    factor: float = 2.0
    _current: float = field(default=0.0, init=False)

    def next_delay(self) -> float:
        self._current = self.initial_s if self._current == 0 else min(
            self._current * self.factor, self.max_s
        )
        return self._current * random.uniform(0.8, 1.2)

    def reset(self) -> None:
        self._current = 0.0


class Driver(ABC, Generic[D]):
    """Base class: lifecycle, status bookkeeping and sample construction."""

    def __init__(
        self,
        device: D,
        on_samples: SampleSink,
        on_status: StatusSink,
        *,
        clock: Clock = utcnow,
        backoff: Backoff | None = None,
    ) -> None:
        self.device: D = device
        self._on_samples = on_samples
        self._on_status = on_status
        self._clock = clock
        self._backoff = backoff or Backoff()
        self._tags: dict[str, TagBase] = {t.name: t for t in device.tags}
        self._status = DeviceStatus(device.device_id)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # --- public API ---------------------------------------------------------------------------

    @property
    def status(self) -> DeviceStatus:
        return self._status

    def tag(self, name: str) -> TagBase:
        try:
            return self._tags[name]
        except KeyError:
            raise DriverError(f"{self.device.device_id} has no tag {name!r}") from None

    async def start(self) -> None:
        if not self.device.enabled:
            await self._set_status(DeviceState.DISABLED)
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._supervise(), name=f"driver:{self.device.device_id}")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self._disconnect()

    async def read_tag(self, name: str) -> Value:
        """Fresh read of one tag in engineering units (control layer: previous value/read-back)."""
        tag = self.tag(name)
        return to_engineering(tag, await self._read_raw(tag))

    async def read_back(self, name: str, timeout_s: float = 5.0) -> Value:
        """Value after a write. Polled protocols read again; push protocols override this."""
        return await asyncio.wait_for(self.read_tag(name), timeout_s)

    async def write_tag(self, name: str, grant: WriteGrant) -> None:
        """Write a raw value. Only the control-safety layer calls this, with its grant."""
        if not isinstance(grant, WriteGrant):
            raise WriteNotPermitted("writes need a WriteGrant from the control-safety layer")
        tag = self.tag(name)
        if grant.tag_path != self.device.tag_path(name):
            raise WriteNotPermitted(f"grant is for {grant.tag_path}, not {name}")
        if tag.access is not Access.READ_WRITE:
            raise WriteNotPermitted(f"{grant.tag_path} is not read_write")
        await self._write_raw(tag, grant.raw_value)

    # --- for subclasses -----------------------------------------------------------------------

    @abstractmethod
    async def _connect(self) -> None: ...

    @abstractmethod
    async def _disconnect(self) -> None: ...

    @abstractmethod
    async def _run_connected(self) -> None:
        """Read until the connection fails (raise) or the driver stops (return/cancel)."""

    @abstractmethod
    async def _read_raw(self, tag: TagBase) -> Value: ...

    @abstractmethod
    async def _write_raw(self, tag: TagBase, raw: Value) -> None: ...

    def make_sample(
        self, tag: TagBase, raw: Value | None, quality: Quality | None = None,
        timestamp: datetime | None = None,
    ) -> Sample:
        ts = timestamp or self._clock()
        if raw is None:
            return Sample(tag=self.device.tag_path(tag.name), value=None, timestamp=ts,
                          quality=Quality.BAD)
        value = to_engineering(tag, raw)
        q = quality or range_quality(tag, value)
        return Sample(tag=self.device.tag_path(tag.name), value=value, timestamp=ts, quality=q)

    async def emit(self, samples: list[Sample]) -> None:
        if not samples:
            return
        await self._on_samples(samples)

    async def mark_seen(self) -> None:
        """A successful read/message: online, and remember when."""
        await self._set_status(DeviceState.ONLINE, last_seen=self._clock())

    async def mark_error(self, state: DeviceState, error: str) -> None:
        log.warning("%s: %s", self.device.device_id, error)
        await self._set_status(state, last_error=error, last_error_at=self._clock())

    async def emit_all_bad(self) -> None:
        await self.emit([self.make_sample(t, None) for t in self.device.tags])

    # --- internals ----------------------------------------------------------------------------

    async def _set_status(self, state: DeviceState, **changes: object) -> None:
        new = replace(self._status, state=state, **changes)  # type: ignore[arg-type]
        changed = new.state != self._status.state or new.last_error != self._status.last_error
        self._status = new
        if changed or "last_seen" in changes:
            await self._on_status(new)

    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._connect()
                self._backoff.reset()
                await self._run_connected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a device failure must never escape
                was_online = self._status.state is DeviceState.ONLINE
                await self.mark_error(DeviceState.OFFLINE, f"{type(exc).__name__}: {exc}")
                if was_online:
                    await self.emit_all_bad()
            with contextlib.suppress(Exception):
                await self._disconnect()
            if self._stopping.is_set():
                break
            delay = self._backoff.next_delay()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)


async def sleep_until_next(period_s: float, started: float, stopping: asyncio.Event) -> None:
    """Sleep for the rest of a poll period (compensating for the time the poll took)."""
    loop = asyncio.get_running_loop()
    remaining = max(0.0, period_s - (loop.time() - started))
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stopping.wait(), timeout=remaining)
