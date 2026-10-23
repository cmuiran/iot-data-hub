"""SNMP driver (pysnmp, asyncio): UPS, PDUs, switches, environment monitors.

SNMPv2c (community from an env var) or SNMPv3 (USM user with SHA/AES; keys from env vars).
Tags name objects by numeric OID; each poll sends GET requests of up to
``max_oids_per_request`` OIDs. Read-only (no SET).

* No answer (timeout, wrong community: agents stay silent) -> offline, values bad.
* An authentication error (wrong v3 key) -> offline with that message.
* ``noSuchObject`` / ``noSuchInstance`` for an OID -> that tag bad, device state ``error``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from pysnmp.hlapi.v3arch import asyncio as snmp
from pysnmp.proto import rfc1902

from hub.codec import CodecError, Value, coerce
from hub.drivers.base import DeviceState, Driver, DriverError, sleep_until_next
from hub.models import DataType, SnmpDevice, SnmpTag, SnmpV2c, TagBase

log = logging.getLogger(__name__)

AUTH = {"md5": snmp.usmHMACMD5AuthProtocol, "sha": snmp.usmHMACSHAAuthProtocol,
        "sha224": snmp.usmHMAC128SHA224AuthProtocol, "sha256": snmp.usmHMAC192SHA256AuthProtocol,
        "sha384": snmp.usmHMAC256SHA384AuthProtocol, "sha512": snmp.usmHMAC384SHA512AuthProtocol}
PRIV = {"des": snmp.usmDESPrivProtocol, "aes128": snmp.usmAesCfb128Protocol,
        "aes192": snmp.usmAesCfb192Protocol, "aes256": snmp.usmAesCfb256Protocol}
_NUMBER_TYPES = (rfc1902.Integer32, rfc1902.Integer, rfc1902.Counter32, rfc1902.Counter64,
                 rfc1902.Gauge32, rfc1902.Unsigned32, rfc1902.TimeTicks)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DriverError(f"environment variable {name} is not set")
    return value


def convert(tag: TagBase, value: Any) -> Value | None:
    """An SNMP value as the tag's data type; None for noSuch* / endOfMibView / null."""
    name = type(value).__name__
    if name in ("NoSuchObject", "NoSuchInstance", "EndOfMibView", "Null"):
        return None
    if isinstance(value, _NUMBER_TYPES):
        raw: object = int(value)
    elif isinstance(value, rfc1902.OctetString):
        text = value.prettyPrint() if not value.asOctets().isascii() else value.asOctets().decode()
        if tag.data_type is DataType.STRING:
            return text.strip()
        try:   # some agents send numbers as text, e.g. "23.5"
            raw = float(text) if "." in text else int(text)
        except ValueError as exc:
            raise CodecError(f"{text!r} is not a number") from exc
    elif isinstance(value, rfc1902.IpAddress | rfc1902.ObjectIdentifier):
        raw = value.prettyPrint()
    else:
        raw = value.prettyPrint()
    if tag.data_type is DataType.BOOL and isinstance(raw, int):
        # TruthValue (SNMPv2-TC): 1 = true, 2 = false
        if raw in (1, 2):
            return raw == 1
    return coerce(raw, tag.data_type)


class SnmpDriver(Driver[SnmpDevice]):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        c = self.device.connection
        tags = list(self.device.tags)
        self._chunks = [tags[i : i + c.max_oids_per_request]
                        for i in range(0, len(tags), c.max_oids_per_request)]
        self._engine: snmp.SnmpEngine | None = None
        self._target: Any = None
        self._auth: Any = None

    def _credentials(self) -> Any:
        sec = self.device.connection.security
        if isinstance(sec, SnmpV2c):
            return snmp.CommunityData(_env(sec.community_env), mpModel=1)
        return snmp.UsmUserData(
            sec.username,
            authKey=_env(sec.auth_key_env) if sec.auth_key_env else None,
            privKey=_env(sec.priv_key_env) if sec.priv_key_env else None,
            authProtocol=AUTH[sec.auth_protocol] if sec.auth_protocol else snmp.usmNoAuthProtocol,
            privProtocol=PRIV[sec.priv_protocol] if sec.priv_protocol else snmp.usmNoPrivProtocol)

    async def _connect(self) -> None:
        c = self.device.connection
        self._auth = self._credentials()
        self._engine = snmp.SnmpEngine()
        self._target = await snmp.UdpTransportTarget.create(
            (c.host, c.port), timeout=c.timeout_ms / 1000, retries=c.retries)

    async def _disconnect(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.close_dispatcher()
            except Exception:  # noqa: BLE001 - already closed
                pass

    async def _run_connected(self) -> None:
        period = self.device.poll_interval_ms / 1000
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.poll_once()
            await sleep_until_next(period, started, self._stopping)

    async def _get(self, tags: list[SnmpTag]) -> dict[str, Any]:
        if self._engine is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        objects = [snmp.ObjectType(snmp.ObjectIdentity(t.oid)) for t in tags]
        error, status, index, var_binds = await snmp.get_cmd(
            self._engine, self._auth, self._target, snmp.ContextData(), *objects,
            lookupMib=False)  # numeric OIDs in, numeric OIDs out: no MIB files needed
        if error:
            raise DriverError(f"{self.device.connection.host}: {error}")
        if status:
            bad = tags[int(index) - 1].oid if index and int(index) <= len(tags) else "?"
            raise CodecError(f"agent answered {status.prettyPrint()} for {bad}")
        return {str(oid): value for oid, value in var_binds}

    async def poll_once(self) -> None:
        samples = []
        problem: str | None = None
        for chunk in self._chunks:
            try:
                answers = await self._get(chunk)
            except CodecError as exc:
                problem = str(exc)
                samples += [self.make_sample(t, None) for t in chunk]
                continue
            for tag in chunk:
                value: Value | None = None
                try:
                    value = convert(tag, answers.get(tag.oid))
                    if value is None:
                        problem = f"{tag.oid} ({tag.name}): no such object on the agent"
                except CodecError as exc:
                    problem = f"{tag.oid} ({tag.name}): {exc}"
                samples.append(self.make_sample(tag, value))
        await self.emit(samples)
        if problem:
            await self.mark_error(DeviceState.ERROR, problem)
        else:
            await self.mark_seen()

    async def _read_raw(self, tag: TagBase) -> Value:
        assert isinstance(tag, SnmpTag)
        try:
            value = convert(tag, (await self._get([tag])).get(tag.oid))
        except CodecError as exc:
            raise DriverError(str(exc)) from exc
        if value is None:
            raise DriverError(f"{tag.oid}: no such object")
        return value

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        raise DriverError("SNMP devices are read-only")


__all__ = ["SnmpDriver", "convert"]
