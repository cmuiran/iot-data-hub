"""The hub at run time: drivers, latest-value cache, storage writer, alarms and the live bus.

Data path::

    driver -> Hub._on_samples -> latest cache  -> LiveBus (WebSocket clients)
                              -> AlarmEngine   -> store (alarm_events) + LiveBus
                              -> write buffer  -> store.write_samples (batched, retried)
                              -> CalcEngine    -> calculated tags -> Hub._on_samples

Nothing on this path waits for the database: samples are buffered (bounded; the oldest are
dropped and counted if the database is down for long) and written in batches by one task.
The hub can also run without a store (console mode); then only the live parts work.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import select
import selectors
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from hub.alarms import AlarmEngine, AlarmRaise
from hub.calc.engine import CalcEngine
from hub.config_loader import ConfigReport
from hub.drivers import Driver, DriverError, create_driver
from hub.drivers.base import AnyDevice, Clock, DeviceState, DeviceStatus, utcnow
from hub.drivers.calc import CalcDriver
from hub.history import HistoryFilter
from hub.models import Broker, CalcDevice, Quality, QualityReason, Sample, TagBase
from hub.spool import DiskQueue

log = logging.getLogger(__name__)


class Store(Protocol):  # the part of PostgresStore the runtime uses
    async def sync_config(self, devices: Any) -> Any: ...
    async def write_samples(self, samples: Any) -> int: ...
    async def upsert_device_statuses(self, statuses: list[DeviceStatus]) -> None: ...
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
    spool_batches: int = 0          # sample batches waiting on disk (store & forward)
    spool_dropped_batches: int = 0  # dropped because the spool reached its size limit


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
    # store & forward: samples the database cannot take wait here instead of being dropped
    spool: DiskQueue | None = None
    spool_drain_batches: int = 20   # spooled batches written per flush (live data first)

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
        self.calc: CalcEngine | None = self._make_calc(self.report)
        self.history = HistoryFilter()
        # async callbacks receiving alarm events (notifications); errors are logged, not raised
        self.alarm_listeners: list[Callable[[dict[str, Any]], Any]] = []
        self._buffer: deque[Sample] = deque()
        self._status_dirty: dict[str, DeviceStatus] = {}
        # (ts, device_id, state, message) waiting for the store (collection_errors)
        self._collection_errors: deque[tuple[datetime, str, str, str]] = deque(maxlen=1000)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        for device_id in self.devices:
            self.statuses[device_id] = DeviceStatus(device_id)

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        if self.report.has_errors:
            raise RuntimeError("configuration has errors; run python -m hub.validate_config")
        if self.spool is not None:                 # a backlog from before a restart
            self.stats.spool_batches = self.spool.count
        if self.store is not None:
            await self.store.sync_config(list(self.devices.values()))
            await self._sync_building(self.report)
            open_alarms = await self.store.open_alarm_sources()
            self.alarms.restore({src: rec.kind for src, rec in open_alarms.items()})
        if self.calc is not None:
            await self._seed_calc(self.calc)
        self._running = True
        self._tasks = [asyncio.create_task(self._writer(), name="hub:writer"),
                       asyncio.create_task(self._ticker(), name="hub:alarm-ticker")]
        for device in self.devices.values():
            self.drivers[device.device_id] = self._create_driver(device, self.report.brokers)
        await asyncio.gather(*(d.start() for d in self.drivers.values()))
        if self.calc is not None:
            await self.calc.start()
        log.info("hub started: %d devices, %d tags", len(self.devices), len(self.tags))

    async def stop(self) -> None:
        self._running = False
        if self.calc is not None:
            await self.calc.stop()
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
            ref = getattr(getattr(device, "connection", None), "broker_ref", None)
            return ref is not None and old_brokers.get(ref) != report.brokers.get(ref)

        for device_id in sorted(set(self.devices) - set(new_devices)):
            driver = self.drivers.pop(device_id, None)
            if driver is not None:
                await driver.stop()
            self.statuses.pop(device_id, None)
            changes["removed"].append(device_id)

        if self.store is not None:
            await self.store.sync_config(list(new_devices.values()))
            await self._sync_building(report)

        self.report = report
        self.devices = new_devices
        self.tags = report.tags_by_path()
        for path in [p for p in self.latest if p not in self.tags]:
            del self.latest[path]

        old_calc, self.calc = self.calc, self._make_calc(report)
        if old_calc is not None:
            await old_calc.stop()
        if self.calc is not None:
            if old_calc is not None:
                self.calc.adopt(old_calc)
            else:
                await self._seed_calc(self.calc)

        for device_id, device in sorted(new_devices.items()):
            old = self.drivers.get(device_id)
            if old is not None and old.device == device and not broker_changed(device):
                if isinstance(old, CalcDriver):
                    old.engine = self.calc         # results now come from the new engine
                changes["unchanged"].append(device_id)
                continue
            if old is not None:
                await old.stop()
                changes["restarted"].append(device_id)
            else:
                changes["added"].append(device_id)
            self.statuses[device_id] = DeviceStatus(device_id)
            driver = self._create_driver(device, report.brokers)
            self.drivers[device_id] = driver
            await driver.start()

        self.alarms = AlarmEngine(report.alarms, self._raise_alarm, self._clear_alarm)
        if self.store is not None:
            open_alarms = await self.store.open_alarm_sources()
            self.alarms.restore({src: rec.kind for src, rec in open_alarms.items()})
        if self.calc is not None and self._running:
            await self.calc.start()
        self.bus.publish({"type": "config", "action": "reloaded", **changes})
        log.info("configuration reloaded: %s",
                 {k: v for k, v in changes.items() if v and k != "unchanged"})
        return changes

    def _create_driver(self, device: AnyDevice, brokers: dict[str, Broker]) -> Driver:
        if isinstance(device, CalcDevice):
            return CalcDriver(device, self._on_samples, self._on_status, engine=self.calc,
                              clock=self.clock)
        return self.driver_factory(device, self._on_samples, self._on_status,
                                   brokers=brokers, clock=self.clock)

    async def _sync_building(self, report: ConfigReport) -> None:
        sync = getattr(self.store, "sync_building", None)
        if sync is not None:
            await sync(report.building)

    def _make_calc(self, report: ConfigReport) -> CalcEngine | None:
        devices = {d.device.device_id for d in report.devices}
        defs = {p: d for p, d in report.calcs.items() if d.device_id in devices}
        if not defs:     # (a report may be cut down to some devices, e.g. hub-commission read)
            return None
        return CalcEngine(defs, [p for p in report.calc_order if p in defs], self._on_samples,
                          self.clock)

    async def _seed_calc(self, engine: CalcEngine) -> None:
        """Inputs from the live cache; running totals (integrate/runtime) from the last stored
        result, so counters continue after a restart instead of starting from zero."""
        engine.seed(self.latest.values())
        latest = getattr(self.store, "latest", None)
        if latest is None:
            return
        try:
            rows = await latest(list(engine.defs))
        except Exception:  # noqa: BLE001 - a fresh start from zero beats not starting
            log.exception("could not read the last results of the calculated tags")
            return
        engine.seed(Sample(tag=r.tag_path, value=r.value, timestamp=r.ts, quality=r.quality)
                    for r in rows if r.value is not None)

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
        received = self.clock()
        samples = [s if s.received_at is not None else
                   s.model_copy(update={"received_at": received}) for s in samples]
        for s in samples:
            previous = self.latest.get(s.tag)
            if previous is None or s.timestamp >= previous.timestamp:
                self.latest[s.tag] = s
        if self.bus.subscriber_count:            # no dashboard open: skip building the event
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
            if overflow > 0 and self.spool is not None:
                await self._spill()
            elif overflow > 0:
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
        if self.calc is not None:
            self.calc.feed(samples)

    async def _on_status(self, status: DeviceStatus) -> None:
        old = self.statuses.get(status.device_id)
        self.statuses[status.device_id] = status
        if old is None or old.state != status.state or old.last_error != status.last_error:
            self.bus.publish(status_event(status))
            self._status_dirty[status.device_id] = status
            if status.state in (DeviceState.OFFLINE, DeviceState.ERROR) and status.last_error:
                self._collection_errors.append((status.last_error_at or self.clock(),
                                                status.device_id, str(status.state),
                                                status.last_error))
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

    async def raise_system_alarm(self, alarm: AlarmRaise) -> None:
        """Alarms about the hub itself (``system:...`` sources: certificates, audit, ...)."""
        await self._raise_alarm(alarm)

    async def clear_system_alarm(self, source: str, ts: datetime) -> None:
        await self._clear_alarm(source, ts)

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
                if self.spool is not None:          # keep it safe on disk until it can go
                    with contextlib.suppress(Exception):
                        await self._spill()
                raise
        self.stats.buffered = 0
        await self._drain_spool()
        dirty, self._status_dirty = self._status_dirty, {}
        try:
            if dirty:
                await self.store.upsert_device_statuses(list(dirty.values()))
        except Exception:
            self._status_dirty = dirty | self._status_dirty
            raise
        add_errors = getattr(self.store, "add_collection_errors", None)
        if self._collection_errors and add_errors is not None:
            errors = list(self._collection_errors)   # taken before awaiting: flushes overlap
            self._collection_errors.clear()
            try:
                await add_errors(errors)
            except Exception:
                self._collection_errors.extendleft(reversed(errors))
                raise


    # --- store & forward -------------------------------------------------------------------

    async def _spill(self) -> None:
        """Move the memory buffer to the disk spool (oldest first, in batches)."""
        assert self.spool is not None
        samples = list(self._buffer)
        if not samples:
            return
        blobs = [encode_samples(samples[i:i + self.batch_size])
                 for i in range(0, len(samples), self.batch_size)]
        dropped = await self.spool.put(blobs)
        self._buffer.clear()
        self.stats.buffered = 0
        self.stats.spool_dropped_batches += dropped
        self.stats.spool_batches = self.spool.count
        log.info("store & forward: %d sample(s) moved to disk (%d batch(es) waiting)",
                 len(samples), self.spool.count)

    async def _drain_spool(self) -> None:
        """Write a part of the spool after a successful flush; the rest on the next ones."""
        if self.spool is None or not self.spool.count or self.store is None:
            return
        items = await self.spool.peek(self.spool_drain_batches)
        last_id = None
        try:
            for item_id, blob in items:
                self.stats.samples_written += await self.store.write_samples(
                    decode_samples(blob))
                last_id = item_id
        finally:
            if last_id is not None:
                await self.spool.ack(last_id)
            self.stats.spool_batches = self.spool.count
        if self.spool.count:
            self._wake.set()                      # more to forward: come back soon
        else:
            log.info("store & forward: everything written, the spool is empty")


def encode_samples(samples: list[Sample]) -> bytes:
    return json.dumps([[s.tag, s.timestamp.isoformat(), s.value, str(s.quality),
                        s.reason and str(s.reason)]
                       for s in samples], separators=(",", ":")).encode()


def decode_samples(blob: bytes) -> list[Sample]:
    out = []
    for row in json.loads(blob):          # rows spooled by older versions have no reason
        tag, ts, value, quality, *rest = row
        out.append(Sample(tag=tag, timestamp=datetime.fromisoformat(ts), value=value,
                          quality=Quality(quality),
                          reason=QualityReason(rest[0]) if rest and rest[0] else None))
    return out


_WSAENOTSOCK = 10038


class _ResilientSelector(selectors.SelectSelector):
    """select() that survives a socket closed while still registered.

    On Windows select() fails for *every* socket when one of them was closed behind the loop's
    back (WinError 10038) - e.g. paho-mqtt after a refused TLS handshake - and that error would
    stop the whole event loop, i.e. all drivers. The dead entries are dropped and select() runs
    again; the owner of the socket has already closed it and handles its own error.
    """

    def select(self, timeout: float | None = None) -> list[tuple[Any, int]]:
        for _ in range(3):
            try:
                return super().select(timeout)
            except OSError as exc:
                if getattr(exc, "winerror", None) != _WSAENOTSOCK:
                    raise
                self._drop_closed()
                timeout = 0
        return super().select(0)

    def _drop_closed(self) -> None:
        for key in list(self.get_map().values()):
            try:
                select.select([key.fd], [], [], 0)
            except OSError:
                self.unregister(key.fileobj)
                log.warning("dropped a socket closed while registered (fd %s)", key.fd)


def new_event_loop() -> asyncio.AbstractEventLoop:
    """The hub's event loop: selector based on Windows (aiomqtt and psycopg need it)."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(_ResilientSelector())
    return asyncio.new_event_loop()


def run(coro: Any) -> Any:
    """asyncio.run on the hub's event loop (see new_event_loop)."""
    return asyncio.run(coro, loop_factory=new_event_loop)
