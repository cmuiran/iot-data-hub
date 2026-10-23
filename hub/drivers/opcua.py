"""OPC UA driver (asyncua).

Tags are monitored through one subscription whose publishing interval is the device's
``poll_interval_ms``, so values arrive on change instead of being polled. The OPC UA status code
maps to quality (Good / Uncertain / Bad) and the server's source timestamp is used when present.
A periodic read of the server state detects a dead connection (subscriptions alone fail
silently), which triggers the reconnect loop.

``node_id`` without ``ns=``/``nsu=`` is resolved in ``connection.namespace`` (URI -> index at
connect time). Credentials and certificate paths come from the configured environment variables.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from asyncua import Client, ua
from asyncua.crypto import security_policies

from hub.codec import CodecError, Value, coerce
from hub.drivers.base import Driver, DriverError
from hub.models import (
    DataType,
    OpcUaConnection,
    OpcUaDevice,
    OpcUaTag,
    Quality,
    SecurityMode,
    SecurityPolicy,
    TagBase,
    UsernameAuth,
)
from hub.pki import application_uri

log = logging.getLogger(__name__)
for _name in ("asyncua", "asyncua.client.ua_client", "asyncua.common.subscription"):
    logging.getLogger(_name).setLevel(logging.WARNING)

VARIANT: dict[DataType, ua.VariantType] = {
    DataType.BOOL: ua.VariantType.Boolean,
    DataType.INT16: ua.VariantType.Int16,
    DataType.UINT16: ua.VariantType.UInt16,
    DataType.INT32: ua.VariantType.Int32,
    DataType.UINT32: ua.VariantType.UInt32,
    DataType.INT64: ua.VariantType.Int64,
    DataType.UINT64: ua.VariantType.UInt64,
    DataType.FLOAT32: ua.VariantType.Float,
    DataType.FLOAT64: ua.VariantType.Double,
    DataType.STRING: ua.VariantType.String,
}

_POLICIES = {
    SecurityPolicy.BASIC256SHA256: security_policies.SecurityPolicyBasic256Sha256,
    SecurityPolicy.AES128_SHA256_RSAOAEP: security_policies.SecurityPolicyAes128Sha256RsaOaep,
    SecurityPolicy.AES256_SHA256_RSAPSS: security_policies.SecurityPolicyAes256Sha256RsaPss,
}
_MODES = {
    SecurityMode.SIGN: ua.MessageSecurityMode.Sign,
    SecurityMode.SIGN_AND_ENCRYPT: ua.MessageSecurityMode.SignAndEncrypt,
}

_SERVER_STATE = ua.NodeId(ua.ObjectIds.Server_ServerStatus_State)


def quality_of(status: ua.StatusCode | None) -> Quality:
    if status is None or status.is_good():
        return Quality.GOOD
    if status.is_uncertain():
        return Quality.UNCERTAIN
    return Quality.BAD


def _env(name: str | None, what: str) -> str:
    if not name:
        raise DriverError(f"{what} is not configured")
    value = os.environ.get(name)
    if not value:
        raise DriverError(f"environment variable {name} ({what}) is not set")
    return value


async def open_client(c: OpcUaConnection) -> Client:
    """A connected client with the connection's security and sign-in (also used by
    hub-commission)."""
    client = Client(c.endpoint_url, timeout=c.timeout_ms / 1000)
    if isinstance(c.auth, UsernameAuth):
        client.set_user(_env(c.auth.username_env, "OPC UA username"))
        client.set_password(_env(c.auth.password_env, "OPC UA password"))
    if c.security_policy is not SecurityPolicy.NONE:
        cert = _env(c.client_certificate_env, "client certificate")
        uri = application_uri(cert)
        if uri:  # servers compare it with the certificate's subjectAltName
            client.application_uri = uri
        await client.set_security(
            _POLICIES[c.security_policy],
            certificate=cert,
            private_key=_env(c.client_private_key_env, "client private key"),
            mode=_MODES[c.security_mode],
        )
    try:
        await client.connect()
    except (OSError, TimeoutError, ua.UaError) as exc:
        raise DriverError(f"cannot connect to {c.endpoint_url}: {exc}") from exc
    return client


class _Handler:
    """asyncua calls this for every data change; hand it to the driver's queue."""

    def __init__(self, queue: asyncio.Queue[tuple[Any, ua.DataValue]]) -> None:
        self.queue = queue

    async def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        self.queue.put_nowait((node, data.monitored_item.Value))

    async def status_change_notification(self, status: Any) -> None:
        log.warning("OPC UA subscription status changed: %s", status)


class OpcUaDriver(Driver[OpcUaDevice]):
    KEEPALIVE_S = 2.0

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._client: Client | None = None
        self._nodes: dict[str, Any] = {}          # tag name -> Node
        self._by_node: dict[str, OpcUaTag] = {}  # str(NodeId) -> tag
        self._queue: asyncio.Queue[tuple[Any, ua.DataValue]] = asyncio.Queue()

    async def _connect(self) -> None:
        self._client = await open_client(self.device.connection)
        await self._resolve_nodes()
        await self._subscribe()

    async def _resolve_nodes(self) -> None:
        assert self._client is not None
        c = self.device.connection
        ns_index: int | None = None
        if c.namespace:
            try:
                ns_index = await self._client.get_namespace_index(c.namespace)
            except ValueError as exc:
                raise DriverError(f"namespace {c.namespace} not on server") from exc
        self._nodes.clear()
        self._by_node.clear()
        for tag in self.device.tags:
            assert isinstance(tag, OpcUaTag)
            text = tag.node_id
            if not text.startswith(("ns=", "nsu=")) and ns_index is not None:
                text = f"ns={ns_index};{text}"
            if text.startswith("nsu="):
                uri, rest = text[4:].split(";", 1)
                text = f"ns={await self._client.get_namespace_index(uri)};{rest}"
            node = self._client.get_node(text)
            self._nodes[tag.name] = node
            self._by_node[node.nodeid.to_string()] = tag

    async def _subscribe(self) -> None:
        assert self._client is not None
        self._queue = asyncio.Queue()
        sub = await self._client.create_subscription(self.device.poll_interval_ms,
                                                     _Handler(self._queue))
        results = await sub.subscribe_data_change(list(self._nodes.values()))
        for tag, result in zip(list(self._nodes), results, strict=True):
            if not isinstance(result, int):
                log.warning("%s.%s: cannot monitor %s: %s", self.device.device_id, tag,
                            self.tag(tag), result)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 3)
            except Exception:  # noqa: BLE001 - the connection is gone anyway
                pass

    async def _run_connected(self) -> None:
        loop = asyncio.get_running_loop()
        next_check = loop.time() + self.KEEPALIVE_S
        while not self._stopping.is_set():
            timeout = max(0.0, next_check - loop.time())
            try:
                node, value = await asyncio.wait_for(self._queue.get(), timeout)
                batch = [(node, value)]
                while not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                await self._handle(batch)
            except TimeoutError:
                pass
            if loop.time() >= next_check:
                await self._keepalive()
                next_check = loop.time() + self.KEEPALIVE_S

    async def _keepalive(self) -> None:
        assert self._client is not None
        try:
            await self._client.get_node(_SERVER_STATE).read_value()
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"connection lost: {exc}") from exc

    async def _handle(self, batch: list[tuple[Any, ua.DataValue]]) -> None:
        samples = []
        for node, dv in batch:
            tag = self._by_node.get(node.nodeid.to_string())
            if tag is None:
                continue
            ts = dv.SourceTimestamp or dv.ServerTimestamp
            timestamp = ts.replace(tzinfo=UTC) if ts and ts.tzinfo is None else ts
            quality = quality_of(dv.StatusCode)
            raw = dv.Value.Value if dv.Value is not None else None
            if quality is Quality.BAD or raw is None:
                samples.append(self.make_sample(tag, None, timestamp=timestamp))
                continue
            try:
                value = coerce(raw, tag.data_type)
            except CodecError as exc:
                log.warning("%s.%s: %s", self.device.device_id, tag.name, exc)
                samples.append(self.make_sample(tag, None, timestamp=timestamp))
                continue
            samples.append(self.make_sample(
                tag, value, quality=None if quality is Quality.GOOD else quality,
                timestamp=timestamp or datetime.now(UTC)))
        await self.emit(samples)
        if samples:
            await self.mark_seen()

    async def _read_raw(self, tag: TagBase) -> Value:
        node = self._nodes.get(tag.name)
        if node is None or self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        try:
            raw = await node.read_value()
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"read {tag.name}: {exc}") from exc
        return coerce(raw, tag.data_type)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        node = self._nodes.get(tag.name)
        if node is None or self._client is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        try:
            await node.write_value(ua.DataValue(ua.Variant(raw, VARIANT[tag.data_type])))
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"write {tag.name}: {exc}") from exc
