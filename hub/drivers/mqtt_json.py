"""MQTT / JSON driver (aiomqtt).

* Subscribes to ``telemetry_topic`` (wildcards allowed) and maps each tag's ``json_path``.
  A field missing from a message is skipped (devices often send partial updates); a field of
  the wrong type gives a ``bad`` sample.
* ``timestamp_path`` / ``timestamp_format``: the device's own time; if it cannot be parsed the
  hub's receive time is used and the samples are ``uncertain``.
* ``status_topic`` (optional, typically the device's LWT): ``online``/``offline`` text or
  ``{"status": "..."}``.
* ``stale_after_s`` (optional): no telemetry for that long -> the device is ``offline`` and its
  tags get one ``bad`` sample, until the next message (silent failures without an LWT).
* Writes publish ``{"<tag>": <raw value>}`` to ``command_topic``. MQTT has no read request, so
  :meth:`read_back` waits for the next telemetry message after the write.

Each device has its own client connection (client id ``hub-<device_id>``). Credentials come
from the broker's ``username_env`` / ``password_env`` environment variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from datetime import datetime
from typing import Any

import aiomqtt

from hub.codec import CodecError, Value, coerce
from hub.drivers.base import DeviceState, Driver, DriverError
from hub.models import (
    Broker,
    MqttDevice,
    MqttTag,
    Quality,
    TagBase,
    parse_device_timestamp,
    parse_json_path,
)

log = logging.getLogger(__name__)

_ONLINE = {"online", "1", "true", "connected", "up"}
_OFFLINE = {"offline", "0", "false", "disconnected", "down", "lost"}
_MISSING = object()


def extract(payload: object, path: str) -> object:
    """Follow a JSON path (hub.models subset); returns _MISSING when a key/index is absent."""
    node = payload
    for part in parse_json_path(path):
        if isinstance(part, int):
            if not isinstance(node, list) or part >= len(node):
                return _MISSING
            node = node[part]
        else:
            if not isinstance(node, dict) or part not in node:
                return _MISSING
            node = node[part]
    return node


class _NamedTlsContext(ssl.SSLContext):
    """Checks the broker certificate against ``server_name`` instead of the connect host."""

    server_name: str | None = None

    def wrap_socket(self, sock: Any, *args: Any, server_hostname: str | None = None,
                    **kwargs: Any) -> ssl.SSLSocket:
        return super().wrap_socket(sock, *args, server_hostname=self.server_name
                                   or server_hostname, **kwargs)


def _env_path(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DriverError(f"environment variable {name} is not set (path of a TLS file)")
    if not os.path.isfile(value):
        raise DriverError(f"{name} points to {value!r}, which is not a file")
    return value


def broker_tls_context(broker: Broker) -> ssl.SSLContext:
    """TLS for a broker: certificate and host name always verified (TLS 1.2+), against the
    system CAs or ``ca_file_env``; client certificate for mutual TLS when configured."""
    ctx = _NamedTlsContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if broker.ca_file_env:
        ctx.load_verify_locations(cafile=_env_path(broker.ca_file_env))
    else:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    if broker.client_certificate_env and broker.client_private_key_env:
        try:
            ctx.load_cert_chain(_env_path(broker.client_certificate_env),
                                _env_path(broker.client_private_key_env))
        except ssl.SSLError as exc:
            raise DriverError(f"client certificate/key for broker {broker.broker_id}: {exc}") \
                from exc
    ctx.server_name = broker.tls_server_name
    return ctx


def client_kwargs(b: Broker, identifier: str) -> dict[str, object]:
    """aiomqtt.Client arguments for a broker (also used by hub-commission)."""
    kwargs: dict[str, object] = {
        "hostname": b.host, "port": b.port, "keepalive": b.keepalive_s,
        "identifier": identifier, "timeout": 10,
    }
    if b.username_env:
        kwargs["username"] = os.environ.get(b.username_env)
        kwargs["password"] = os.environ.get(b.password_env) if b.password_env else None
    if b.tls:
        kwargs["tls_context"] = broker_tls_context(b)
    return kwargs


def topic_matches(topic_filter: str, topic: str) -> bool:
    f_levels, t_levels = topic_filter.split("/"), topic.split("/")
    for i, f in enumerate(f_levels):
        if f == "#":
            return True
        if i >= len(t_levels) or (f != "+" and f != t_levels[i]):
            return False
    return len(f_levels) == len(t_levels)


def parse_status(payload: bytes) -> bool | None:
    """True = online, False = offline, None = not understood."""
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    if isinstance(data, dict):
        data = data.get("status", data.get("state"))
    if isinstance(data, bool):
        return data
    word = str(data).strip().lower()
    if word in _ONLINE:
        return True
    if word in _OFFLINE:
        return False
    return None


class MqttJsonDriver(Driver[MqttDevice]):
    def __init__(self, *args: object, broker: Broker, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.broker = broker
        self._client: aiomqtt.Client | None = None
        self._last: dict[str, tuple[Value, datetime]] = {}
        self._message_seq = 0
        self._new_message = asyncio.Condition()
        self._last_message_at = 0.0   # loop time
        self._stale = False

    # --- connection ---------------------------------------------------------------------------

    def _client_kwargs(self) -> dict[str, object]:
        return client_kwargs(self.broker, f"hub-{self.device.device_id}")

    async def _connect(self) -> None:
        client = aiomqtt.Client(**self._client_kwargs())  # type: ignore[arg-type]
        try:
            await client.__aenter__()
        except aiomqtt.MqttError as exc:
            raise DriverError(f"cannot connect to {self.broker.host}:{self.broker.port}: {exc}") \
                from exc
        self._client = client
        c = self.device.connection
        await client.subscribe(c.telemetry_topic, qos=c.qos)
        if c.status_topic:
            await client.subscribe(c.status_topic, qos=c.qos)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except aiomqtt.MqttError:
                pass

    async def _run_connected(self) -> None:
        assert self._client is not None
        c = self.device.connection
        self._last_message_at = asyncio.get_running_loop().time()
        watchdog = (asyncio.create_task(self._watch_staleness(c.stale_after_s))
                    if c.stale_after_s else None)
        try:
            await self._consume()
        finally:
            if watchdog is not None:
                watchdog.cancel()

    async def _watch_staleness(self, stale_after_s: float) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(min(1.0, stale_after_s / 4))
            silent = loop.time() - self._last_message_at
            if silent >= stale_after_s and not self._stale:
                self._stale = True
                await self.mark_error(DeviceState.OFFLINE,
                                      f"no telemetry for {silent:.0f}s (stale_after_s)")
                await self.emit_all_bad()

    async def _consume(self) -> None:
        assert self._client is not None
        c = self.device.connection
        try:
            async for message in self._client.messages:
                topic = str(message.topic)
                payload = message.payload if isinstance(message.payload, bytes) else (
                    str(message.payload).encode()
                )
                if c.status_topic and topic_matches(c.status_topic, topic):
                    await self._handle_status(payload)
                elif topic_matches(c.telemetry_topic, topic):
                    await self.handle_telemetry(payload)
        except aiomqtt.MqttError as exc:
            raise DriverError(f"connection lost: {exc}") from exc
        raise DriverError("message stream ended")

    # --- messages -----------------------------------------------------------------------------

    async def _handle_status(self, payload: bytes) -> None:
        online = parse_status(payload)
        if online is True:
            await self.mark_seen()
        elif online is False:
            await self.mark_error(DeviceState.OFFLINE, "device reported offline")
            await self.emit_all_bad()

    async def handle_telemetry(self, payload: bytes) -> None:
        received = self._clock()
        with contextlib.suppress(RuntimeError):  # no running loop in some unit tests
            self._last_message_at = asyncio.get_running_loop().time()
        self._stale = False
        try:
            data = json.loads(payload)
        except ValueError:
            await self.mark_error(DeviceState.ERROR, "telemetry is not valid JSON")
            return
        c = self.device.connection
        timestamp, ts_quality = received, None
        if c.timestamp_path and c.timestamp_format:
            raw_ts = extract(data, c.timestamp_path)
            try:
                if raw_ts is _MISSING:
                    raise ValueError(f"no {c.timestamp_path} in message")
                timestamp = parse_device_timestamp(raw_ts, c.timestamp_format)
            except (ValueError, OverflowError, OSError) as exc:
                log.warning("%s: bad device timestamp (%s); using receive time",
                            self.device.device_id, exc)
                timestamp, ts_quality = received, Quality.UNCERTAIN

        samples = []
        for tag in self.device.tags:
            assert isinstance(tag, MqttTag)
            raw = extract(data, tag.json_path)
            if raw is _MISSING:
                continue
            try:
                value = coerce(raw, tag.data_type)
            except CodecError as exc:
                log.warning("%s.%s: %s", self.device.device_id, tag.name, exc)
                samples.append(self.make_sample(tag, None, timestamp=timestamp))
                continue
            samples.append(self.make_sample(tag, value, quality=ts_quality, timestamp=timestamp))
            self._last[tag.name] = (value, received)
        await self.emit(samples)
        await self.mark_seen()
        async with self._new_message:
            self._message_seq += 1
            self._new_message.notify_all()

    # --- read / write -------------------------------------------------------------------------

    async def _read_raw(self, tag: TagBase) -> Value:
        if tag.name not in self._last:
            raise DriverError(f"no value received yet for {tag.name}")
        return self._last[tag.name][0]

    async def read_back(self, name: str, timeout_s: float = 5.0) -> Value:
        """Wait for the next telemetry message that carries ``name`` and return its value."""
        self.tag(name)
        start_seq = self._message_seq
        last_before = self._last.get(name)

        def arrived() -> bool:
            return self._message_seq > start_seq and self._last.get(name) is not last_before

        async with self._new_message:
            try:
                await asyncio.wait_for(self._new_message.wait_for(arrived), timeout_s)
            except TimeoutError:
                raise DriverError(f"no telemetry for {name} within {timeout_s:g}s") from None
        return await self.read_tag(name)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        topic = self.device.connection.command_topic
        if not topic:
            raise DriverError(f"{self.device.device_id} has no command_topic")
        if self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        try:
            await self._client.publish(topic, json.dumps({tag.name: raw}),
                                       qos=self.device.connection.qos)
        except aiomqtt.MqttError as exc:
            raise DriverError(f"publish to {topic} failed: {exc}") from exc
