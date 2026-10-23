"""The hub at run time: drivers, latest-value cache, storage writer, alarms and the live bus.

Data path::

    driver -> Hub._on_samples -> latest cache  -> LiveBus (WebSocket clients)
                              -> AlarmEngine   -> store (alarm_events) + LiveBus
                              -> write buffer  -> store.write_samples (batched, retried)

Nothing on this path waits for the database: samples are buffered (bounded; the oldest are
dropped and counted if the database is down for long) and written in batches by one task.
The hub can also run without a store (console mode); then only the live parts work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from hub.alarms import AlarmEngine, AlarmRaise
from hub.history import HistoryFilter
from hub.config_loader import ConfigReport
from hub.drivers import Driver, DriverError, create_driver
from hub.drivers.base import AnyDevice, Clock, DeviceState, DeviceStatus, utcnow
from hub.models import TagBase, Sample

log = logging.getLogger(__name__)


class Store(Protocol):  # the part of PostgresStore the runtime uses
    async def sync_config(self, devices: Any) -> Any: ...
    async def write_samples(self, samples: Any) -> int: ...
    async def upsert_device_status(self, status: DeviceStatus) -> None: ...
    async def open_alarm(self, **kwargs: Any) -> Any: ...
    async def clear_alarm(self, source: str, cleared_at: datetime) -> Any: ...
    async def open_alarm_sources(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------------------------
# Live bus
# --------------------------------------------------------------------------------------------


class LiveBus:
    """Fan-out of live events to subscribers (WebSocket clients). A subscriber that cannot keep
    up is disconnected instead of growing memory without bound."""

    def __init__(self, queue_size: int = 1000) -> None:
        self.queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(self.queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait(None)  # tells the consumer it was dropped


def sample_event(samples: list[Sample]) -> dict[str, Any]:
    return {"type": "samples", "samples": [
        {"tag": s.tag, "value": s.value, "ts": s.timestamp.isoformat(), "quality": str(s.quality)}
        for s in samples]}


def status_event(status: DeviceStatus) -> dict[str, Any]:
    return {"type": "status", "device_id": status.device_id, "state": str(status.state),
            "last_seen": status.last_seen.isoformat() if status.last_seen else None,
            "last_error": status.last_error}


# --------------------------------------------------------------------------------------------
# Hub
# --------------------------------------------------------------------------------------------


@dataclass
class HubStats:
    samples_received: int = 0
    samples_filtered: int = 0       # not stored: unchanged within deadband / heartbeat
    samples_written: int = 0
    samples_dropped: int = 0
    write_errors: int = 0
    last_write_error: str | None = None
    buffered: int = 0


DriverFactory = Callable[..., Driver]


@dataclass
class Hub:
    report: ConfigReport
    store: Store | None = None
    clock: Clock = utcnow
    batch_size: int = 500
    flush_interval_s: float = 1.0
    max_buffer: int = 200_000
    driver_factory: DriverFactory = create_driver

    latest: dict[str, Sample] = field(default_factory=dict)
    statuses: dict[str, DeviceStatus] = field(default_factory=dict)
    bus: LiveBus = field(default_factory=LiveBus)
    stats: HubStats = field(default_factory=HubStats)

    def __post_init__(self) -> None:
        self.devices: dict[str, AnyDevice] = {d.device.device_id: d.device
                                               for d in self.report.devices}
        self.tags: dict[str, tuple[AnyDevice, TagBase]] = self.report.tags_by_path()
        self.drivers: dict[str, Driver] = {}
        self.alarms = AlarmEngine(self.report.alarms, self._raise_alarm, self._clear_alarm)
        self.history = HistoryFilter()
        # async callbacks receiving alarm events (notifications); errors are logged, not raised
        self.alarm_listeners: list[Callable[[dict[str, Any]], Any]] = []
        self._buffer: deque[Sample] = deque()
        self._status_dirty: dict[str, DeviceStatus] = {}
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        for device_id in self.devices:
            self.statuses[device_id] = DeviceStatus(device_id)

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        if self.report.has_errors:
            raise RuntimeError("configuration has errors; run python -m hub.validate_config")
        if self.store is not None:
            await self.store.sync_config(list(self.devices.values()))
            open_alarms = await self.store.open_alarm_sources()
            self.alarms.restore({src: rec.kind for src, rec in open_alarms.items()})
        self._running = True
        self._tasks = [asyncio.create_task(self._writer(), name="hub:writer"),
                       asyncio.create_task(self._ticker(), name="hub:alarm-ticker")]
        for device in self.devices.values():
            driver = self.driver_factory(device, self._on_samples, self._on_status,
                                         brokers=self.report.brokers, clock=self.clock)
            self.drivers[device.device_id] = driver
        await asyncio.gather(*(d.start() for d in self.drivers.values()))
        log.info("hub started: %d devices, %d tags", len(self.devices), len(self.tags))

    async def stop(self) -> None:
        self._running = False
        await asyncio.gather(*(d.stop() for d in self.drivers.values()), return_exceptions=True)
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        await self.flush()

    async def reload(self, report: ConfigReport) -> dict[str, list[str]]:
        """Apply a new configuration without stopping the process.

        Only drivers whose device (or MQTT broker) changed are restarted; removed devices are
        stopped and their tags vanish from the live cache. Alarm rules are replaced; open alarms
        stay open (they are restored from the store). Returns what happened per device.
        """
        if report.has_errors:
            raise RuntimeError("configuration has errors; not applied")
        new_devices = {d.device.device_id: d.device for d in report.devices}
        old_brokers = self.report.brokers
        changes: dict[str, list[str]] = {"added": [], "removed": [], "restarted": [],
                                         "unchanged": []}

        def broker_changed(device: AnyDevice) -> bool:
            ref = getattr(device.connection, "broker_ref", None)
            return ref is not None and old_brokers.get(ref) != report.brokers.get(ref)

        for device_id in sorted(set(self.devices) - set(new_devices)):
            driver = self.drivers.pop(device_id, None)
            if driver is not None:
                await driver.stop()
            self.statuses.pop(device_id, None)
            changes["removed"].append(device_id)

        if self.store is not None:
            await self.store.sync_config(list(new_devices.values()))

        self.report = report
        self.devices = new_devices
        self.tags = report.tags_by_path()
        for path in [p for p in self.latest if p not in self.tags]:
            del self.latest[path]

        for device_id, device in sorted(new_devices.items()):
            old = self.drivers.get(device_id)
            if old is not None and old.device == device and not broker_changed(device):
                changes["unchanged"].append(device_id)
                continue
            if old is not None:
                await old.stop()
                changes["restarted"].append(device_id)
            else:
                changes["added"].append(device_id)
            self.statuses[device_id] = DeviceStatus(device_id)
            driver = self.driver_factory(device, self._on_samples, self._on_status,
                                         brokers=report.brokers, clock=self.clock)
            self.drivers[device_id] = driver
            await driver.start()

        self.alarms = AlarmEngine(report.alarms, self._raise_alarm, self._clear_alarm)
        if self.store is not None:
            open_alarms = await self.store.open_alarm_sources()
            self.alarms.restore({src: rec.kind for src, rec in open_alarms.items()})
        self.bus.publish({"type": "config", "action": "reloaded", **changes})
        log.info("configuration reloaded: %s",
                 {k: v for k, v in changes.items() if v and k != "unchanged"})
        return changes

    async def __aenter__(self) -> Hub:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # --- lookup (used by the backend) ---------------------------------------------------------

    def driver_for_tag(self, tag_path: str) -> tuple[Driver, TagBase]:
        found = self.tags.get(tag_path)
        if found is None:
            raise DriverError(f"unknown tag {tag_path}")
        device, tag = found
        driver = self.drivers.get(device.device_id)
        if driver is None:
            raise DriverError(f"{device.device_id} has no running driver")
        return driver, tag

    # --- driver callbacks ---------------------------------------------------------------------

    async def _on_samples(self, samples: list[Sample]) -> None:
        self.stats.samples_received += len(samples)
        for s in samples:
            previous = self.latest.get(s.tag)
            if previous is None or s.timestamp >= previous.timestamp:
                self.latest[s.tag] = s
        self.bus.publish(sample_event(samples))
        if self.store is not None:
            for s in samples:
                found = self.tags.get(s.tag)
                if found is None:
                    continue
                device, tag = found
                if self.history.should_store(s, tag, device.history_heartbeat_s):
                    self._buffer.append(s)
                else:
                    self.stats.samples_filtered += 1
            overflow = len(self._buffer) - self.max_buffer
            if overflow > 0:
                for _ in range(overflow):
                    self._buffer.popleft()
                self.stats.samples_dropped += overflow
            self.stats.buffered = len(self._buffer)
            if len(self._buffer) >= self.batch_size:
                self._wake.set()
        try:
            await self.alarms.on_samples(samples)
        except Exception:  # noqa: BLE001 - alarms must never break data collection
            log.exception("alarm evaluation failed")

    async def _on_status(self, status: DeviceStatus) -> None:
        old = self.statuses.get(status.device_id)
        self.statuses[status.device_id] = status
        if old is None or old.state != status.state or old.last_error != status.last_error:
            self.bus.publish(status_event(status))
            self._status_dirty[status.device_id] = status
        elif status.state is DeviceState.ONLINE:
            self._status_dirty[status.device_id] = status  # keep last_seen fresh (batched)
        try:
            await self.alarms.on_status(status, self.clock())
        except Exception:  # noqa: BLE001
            log.exception("offline alarm evaluation failed")

    # --- alarms -------------------------------------------------------------------------------

    async def _raise_alarm(self, alarm: AlarmRaise) -> None:
        record: Any = None
        if self.store is not None:
            try:
                record = await self.store.open_alarm(
                    source=alarm.source, kind=alarm.kind, severity=str(alarm.severity),
                    message=alarm.message, value=alarm.value, raised_at=alarm.ts,
                    tag_path=alarm.tag_path, device_id=alarm.device_id)
            except Exception:  # noqa: BLE001
                log.exception("could not store alarm %s", alarm.source)
        log.warning("ALARM %s %s: %s", alarm.severity, alarm.source, alarm.message)
        event = {"type": "alarm", "action": "raised", "source": alarm.source,
                 "kind": alarm.kind, "severity": str(alarm.severity), "message": alarm.message,
                 "value": alarm.value, "ts": alarm.ts.isoformat(),
                 "alarm_id": getattr(record, "alarm_id", None),
                 "tag_path": alarm.tag_path, "device_id": alarm.device_id}
        self.bus.publish(event)
        await self._notify(event)

    async def _clear_alarm(self, source: str, ts: datetime) -> None:
        if self.store is not None:
            try:
                await self.store.clear_alarm(source, ts)
            except Exception:  # noqa: BLE001
                log.exception("could not clear alarm %s", source)
        log.info("alarm cleared: %s", source)
        event = {"type": "alarm", "action": "cleared", "source": source, "ts": ts.isoformat()}
        self.bus.publish(event)
        await self._notify(event)

    async def _notify(self, event: dict[str, Any]) -> None:
        for listener in list(self.alarm_listeners):
            try:
                await listener(event)
            except Exception:  # noqa: BLE001 - a notification problem never stops the hub
                log.exception("alarm listener failed")

    async def _ticker(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            try:
                await self.alarms.tick(self.clock())
            except Exception:  # noqa: BLE001
                log.exception("alarm tick failed")

    # --- storage writer -----------------------------------------------------------------------

    async def _writer(self) -> None:
        delay = 1.0
        while self._running:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.flush_interval_s)
            self._wake.clear()
            try:
                await self.flush()
                delay = 1.0
            except Exception as exc:  # noqa: BLE001 - keep the data, retry later
                self.stats.write_errors += 1
                self.stats.last_write_error = f"{type(exc).__name__}: {exc}"
                log.warning("storage write failed (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def flush(self) -> None:
        """Write everything buffered. On failure the unwritten samples stay buffered."""
        if self.store is None:
            return
        while self._buffer:
            batch = [self._buffer.popleft() for _ in range(min(self.batch_size, len(self._buffer)))]
            try:
                self.stats.samples_written += await self.store.write_samples(batch)
            except Exception:
                self._buffer.extendleft(reversed(batch))
                self.stats.buffered = len(self._buffer)
                raise
        self.stats.buffered = 0
        dirty, self._status_dirty = self._status_dirty, {}
        try:
            for status in dirty.values():
                await self.store.upsert_device_status(status)
        except Exception:
            self._status_dirty = dirty | self._status_dirty
            raise


def run(coro: Any) -> Any:
    """asyncio.run on the selector loop (aiomqtt and psycopg need it on Windows)."""
    import sys

    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
