"""Platform device contract (protocol ``native``): the device connects to the hub, not the
other way round, over MQTT, HTTP or TCP (backend.ingest), signed in with its own credential.

Messages are the same JSON on every transport (docs/platform.md, phase 4)::

    telemetry  {"ts": "2026-10-27T10:00:00Z" | 1793000000000, "values": {"relay_1": true}}
    state      {"state": "online" | "offline"}
    command    {"command_id": "<uuid>", "tag": "relay_1", "value": true,
                "expires_at": "<iso>"}                                   (hub -> device)
    ack        {"command_id": "<uuid>", "status": "received" | "done" | "failed",
                "value": <value now>, "error": "..."}                    (device -> hub)

Values are keyed by tag key; unknown keys are ignored, a value of the wrong type gives a bad
sample. ``ts`` is optional (else the hub's receive time). No message for ``stale_after_s``
-> offline and every value bad, until the next message. A write sends a command through the
channel the hub was given (backend.ingest.Ingest) and the read-back waits for the value to
come back (telemetry, or an ack carrying it); an ack ``failed`` fails it at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from hub.codec import CodecError, Value, coerce
from hub.drivers.base import DeviceState, Driver, DriverError, WriteGrant, WriteNotPermitted
from hub.models import Access, NativeDevice, Quality, QualityReason, TagBase

log = logging.getLogger(__name__)
MAX_VALUES = 2000


class CommandChannel(Protocol):
    async def send(self, device: NativeDevice, command: dict[str, Any]) -> str:
        """Hand a command to the device's transport; returns the transport's name."""


class NativeDriver(Driver[NativeDevice]):
    def __init__(self, device: NativeDevice, on_samples: Any, on_status: Any, *,
                 channel: CommandChannel | None = None, **kwargs: Any) -> None:
        super().__init__(device, on_samples, on_status, **kwargs)
        self.channel = channel
        self._last: dict[str, Value] = {}
        self._last_message: float | None = None
        self._last_touch_seen = 0.0
        self._waiters: dict[str, list[asyncio.Future[Value]]] = {}
        self._pending: dict[str, str] = {}      # command_id -> tag
        self.counters = {"messages": 0, "unknown_keys": 0, "bad_values": 0}

    # --- from the transports ------------------------------------------------------------------

    async def receive(self, payload: Any) -> int:
        """A telemetry message; returns the number of samples. Raises ValueError if the
        message is not one (the transport answers the device with that)."""
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
            raise ValueError('telemetry is {"values": {"<tag>": value, ...}}')
        values: dict[str, Any] = payload["values"]
        if len(values) > MAX_VALUES:
            raise ValueError(f"at most {MAX_VALUES} values per message")
        ts, quality = self._timestamp(payload.get("ts"))
        samples = []
        for key, raw in values.items():
            tag = self._tags.get(key)
            if tag is None:
                self.counters["unknown_keys"] += 1
                continue
            try:
                value = coerce(raw, tag.data_type)
            except CodecError:
                self.counters["bad_values"] += 1
                samples.append(self.make_sample(tag, None, timestamp=ts,
                                                reason=QualityReason.DEVICE_REPORTED))
                continue
            sample = self.make_sample(tag, value, quality, timestamp=ts)
            samples.append(sample)
            self._last[key] = sample.value
            self._resolve(key, sample.value)
        self.counters["messages"] += 1
        self._last_message = asyncio.get_running_loop().time()
        await self.mark_seen()
        await self.emit(samples)
        return len(samples)

    async def touch(self) -> None:
        """The device was in contact (signed in, fetched commands): it is alive, even when it
        has no new values to send."""
        now = asyncio.get_running_loop().time()
        self._last_message = now
        if self.status.state is not DeviceState.ONLINE or now - self._last_touch_seen > 10:
            self._last_touch_seen = now           # last_seen stays fresh, at most every 10 s
            await self.mark_seen()

    async def state(self, payload: Any) -> None:
        state = payload.get("state") if isinstance(payload, dict) else payload
        if state == "offline":
            await self.mark_error(DeviceState.OFFLINE, "the device reported offline")
            await self.emit_all_bad()
        elif state == "online":
            self._last_message = asyncio.get_running_loop().time()
            await self.mark_seen()

    def ack(self, payload: dict[str, Any]) -> None:
        """An ack for one of this device's commands: a value confirms, failed fails it."""
        tag = self._pending.get(str(payload.get("command_id")))
        if tag is None:
            return
        if payload.get("status") == "failed":
            self._pending.pop(str(payload.get("command_id")), None)
            self._resolve(tag, error=str(payload.get("error") or "the device refused it"))
        elif payload.get("status") == "done" and "value" in payload:
            definition = self._tags[tag]
            with contextlib.suppress(CodecError):
                self._resolve(tag, coerce(payload["value"], definition.data_type))

    def _timestamp(self, raw: Any) -> tuple[datetime | None, Quality | None]:
        if raw is None:
            return None, None
        try:
            if isinstance(raw, int | float) and not isinstance(raw, bool):
                return datetime.fromtimestamp(raw / 1000, UTC), None
            if isinstance(raw, str):
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if ts.tzinfo is not None:
                    return ts.astimezone(UTC), None
        except (ValueError, OverflowError, OSError):
            pass
        return None, Quality.UNCERTAIN          # unreadable device time: hub time, uncertain

    def _resolve(self, key: str, value: Value | None = None, error: str | None = None) -> None:
        for fut in self._waiters.pop(key, []):
            if fut.done():
                continue
            if error is not None:
                fut.set_exception(DriverError(error))
            else:
                fut.set_result(value)

    # --- driver interface ---------------------------------------------------------------------

    async def _connect(self) -> None:
        pass                                 # the device connects to the hub

    async def _disconnect(self) -> None:
        pass

    async def _run_connected(self) -> None:
        loop = asyncio.get_running_loop()
        stale = self.device.connection.stale_after_s
        started = loop.time()
        offline = False
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=1.0)
            if stale is None:
                continue
            last = self._last_message if self._last_message is not None else started
            if loop.time() - last > stale:
                if not offline:
                    offline = True
                    await self.mark_error(DeviceState.OFFLINE,
                                          f"no message for {stale:g} s")
                    await self.emit_all_bad()
            else:
                offline = False

    async def _read_raw(self, tag: TagBase) -> Value:
        if tag.name not in self._last:
            raise DriverError(f"{tag.name}: no value received yet")
        return self._last[tag.name]

    async def read_tag(self, name: str) -> Value:
        return await self._read_raw(self.tag(name))       # already engineering values

    async def read_back(self, name: str, timeout_s: float = 5.0) -> Value:
        fut: asyncio.Future[Value] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(name, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            waiters = self._waiters.get(name, [])
            if fut in waiters:
                waiters.remove(fut)

    async def write_tag(self, name: str, grant: WriteGrant) -> None:
        if not isinstance(grant, WriteGrant):
            raise WriteNotPermitted("writes need a WriteGrant from the control-safety layer")
        tag = self.tag(name)
        if grant.tag_path != self.device.tag_path(name):
            raise WriteNotPermitted(f"grant is for {grant.tag_path}, not {name}")
        if tag.access is not Access.READ_WRITE:
            raise WriteNotPermitted(f"{grant.tag_path} is not read_write")
        if self.channel is None:
            raise DriverError("no device transport is running on this hub")
        self._pending[grant.request_id] = name
        if len(self._pending) > 1000:
            self._pending.pop(next(iter(self._pending)))
        self.last_transport = await self.channel.send(self.device, {
            "command_id": grant.request_id, "tag": name, "value": grant.raw_value})

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:   # pragma: no cover
        raise DriverError("native devices are written through write_tag")
