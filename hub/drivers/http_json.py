"""HTTP/REST driver: polls JSON endpoints (newer IoT devices, gateways, inverters, weather
stations) with httpx.

Each poll requests every distinct tag ``path`` once (``base_url + path``) and takes the values
by JSON path. Credentials come from environment variables (basic, bearer token or an API-key
header); TLS is verified against the system CAs or ``ca_file_env``. HTTP devices are
read-only in the hub.

Errors: a network failure means offline (reconnect with backoff, values bad); an HTTP error
status or a body that is not JSON makes that path's values bad and the device state
``error`` while the other paths keep working.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from datetime import datetime

import httpx

from hub.codec import CodecError, Value, coerce
from hub.drivers.base import DeviceState, Driver, DriverError, sleep_until_next
from hub.drivers.mqtt_json import _MISSING, extract
from hub.models import (
    HttpBasicAuth,
    HttpBearerAuth,
    HttpConnection,
    HttpDevice,
    HttpHeaderAuth,
    HttpTag,
    TagBase,
    parse_device_timestamp,
)

log = logging.getLogger(__name__)
USER_AGENT = "iot-data-hub"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DriverError(f"environment variable {name} is not set")
    return value


class _PathError(Exception):
    """The endpoint answered, but not with usable JSON."""


def build_client(c: HttpConnection) -> httpx.AsyncClient:
    """The HTTP client with the connection's auth, headers and TLS (also used by
    hub-commission)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **c.headers}
    auth: httpx.Auth | None = None
    if isinstance(c.auth, HttpBasicAuth):
        auth = httpx.BasicAuth(_env(c.auth.username_env), _env(c.auth.password_env))
    elif isinstance(c.auth, HttpBearerAuth):
        headers["Authorization"] = f"Bearer {_env(c.auth.token_env)}"
    elif isinstance(c.auth, HttpHeaderAuth):
        headers[c.auth.header] = _env(c.auth.value_env)
    verify: bool | ssl.SSLContext = c.verify_tls
    if c.verify_tls and c.ca_file_env:
        verify = ssl.create_default_context(cafile=_env(c.ca_file_env))
    return httpx.AsyncClient(base_url=c.base_url, headers=headers, auth=auth, verify=verify,
                             timeout=c.timeout_ms / 1000, follow_redirects=False)


class HttpJsonDriver(Driver[HttpDevice]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._paths: dict[str, list[HttpTag]] = {}
        for tag in self.device.tags:
            self._paths.setdefault(tag.path, []).append(tag)
        self._client: httpx.AsyncClient | None = None

    def _build_client(self) -> httpx.AsyncClient:
        return build_client(self.device.connection)

    async def _connect(self) -> None:
        self._client = self._build_client()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _run_connected(self) -> None:
        period = self.device.poll_interval_ms / 1000
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.poll_once()
            await sleep_until_next(period, started, self._stopping)

    async def _get(self, path: str) -> object:
        if self._client is None:
            raise DriverError(f"{self.device.device_id} is not started")
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:  # connection refused, timeout, TLS failure, ...
            raise DriverError(f"GET {self.device.connection.base_url}{path}: "
                              f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise _PathError(f"GET {path or '/'}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise _PathError(f"GET {path or '/'}: the answer is not JSON") from exc

    def _timestamp(self, body: object) -> datetime | None:
        c = self.device.connection
        if c.timestamp_path is None or c.timestamp_format is None:
            return None
        raw = extract(body, c.timestamp_path)
        if raw is _MISSING:
            return None
        try:
            return parse_device_timestamp(raw, c.timestamp_format)
        except ValueError as exc:
            log.debug("%s: bad timestamp: %s", self.device.device_id, exc)
            return None

    def _value(self, tag: HttpTag, body: object) -> Value | None:
        raw = extract(body, tag.json_path)
        if raw is _MISSING or raw is None:
            return None
        try:
            return coerce(raw, tag.data_type)
        except CodecError as exc:
            log.debug("%s: %s: %s", self.device.device_id, tag.name, exc)
            return None

    async def poll_once(self) -> None:
        samples = []
        problem: str | None = None
        for path, tags in self._paths.items():
            try:
                body = await self._get(path)
            except _PathError as exc:
                problem = str(exc)
                samples += [self.make_sample(t, None) for t in tags]
                continue
            ts = self._timestamp(body)
            for tag in tags:
                samples.append(self.make_sample(tag, self._value(tag, body), timestamp=ts))
        await self.emit(samples)
        if problem:
            await self.mark_error(DeviceState.ERROR, problem)
        else:
            await self.mark_seen()

    async def _read_raw(self, tag: TagBase) -> Value:
        assert isinstance(tag, HttpTag)
        try:
            value = self._value(tag, await self._get(tag.path))
        except _PathError as exc:
            raise DriverError(str(exc)) from exc
        if value is None:
            raise DriverError(f"{tag.json_path} is not in the answer of {tag.path or '/'}")
        return value

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        raise DriverError("HTTP devices are read-only")


__all__ = ["HttpJsonDriver"]
