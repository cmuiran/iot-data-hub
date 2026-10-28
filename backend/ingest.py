"""Device ingest: native devices (hub.drivers.native) over MQTT, HTTP and TCP.

Every transport ends here: the device is authenticated with its credential (platform phase 3;
over MQTT the broker does that through the hooks in backend.api.ingest), its messages go to
its driver, and commands go back over the transport the device is reachable on:

* an open TCP session first (immediate),
* else the platform MQTT broker (``d/<device_id>/cmd``) when the device uses MQTT,
* else a queue the device fetches over HTTP (``GET /ingest/v1/devices/<id>/commands``).

Acks advance the command's status (hub.storage.commands). A device acks only its own
commands. Credentials are checked against the database, a valid one is cached for a minute;
failures are audited (``device_auth_failed``) and rate limited per address.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from hub.drivers.base import DriverError, utcnow
from hub.drivers.native import NativeDriver
from hub.models import NativeDevice

log = logging.getLogger(__name__)
AUTH_CACHE_S = 60
MAX_QUEUED = 100
FAILURES_PER_ADDR = 20          # per 10 minutes


class DeviceAuthError(Exception):
    pass


class Ingest:
    def __init__(self, svc: Any, clock: Callable[[], datetime] = utcnow) -> None:
        self.svc = svc
        self.clock = clock
        self.tcp: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self.queues: dict[str, deque[dict[str, Any]]] = {}
        self.mqtt: Any = None        # backend.mqtt_ingest.MqttIngest, when configured
        self._auth: dict[tuple[str, str], float] = {}
        self._failures: dict[str, deque[float]] = {}
        self.counters = {"telemetry": 0, "acks": 0, "commands": 0, "auth_failed": 0,
                         "rejected": 0}

    # --- authentication -----------------------------------------------------------------------

    async def authenticate(self, device_id: str, secret: str, client_addr: str | None,
                           transport: str) -> NativeDriver:
        """The running driver of a native device whose credential is ``secret``."""
        now = time.monotonic()
        failures = self._failures.setdefault(client_addr or "-", deque())
        while failures and now - failures[0] > 600:
            failures.popleft()
        if len(failures) >= FAILURES_PER_ADDR:
            raise DeviceAuthError("too many failed sign-ins from this address")
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        key = (device_id, digest)
        ok = self._auth.get(key, 0) > now
        if not ok:
            ok = bool(secret) and await self.svc.claims.credential_valid(device_id, secret)
            if ok:
                self._auth[key] = now + AUTH_CACHE_S
                if len(self._auth) > 100_000:
                    self._auth.clear()
        driver = self.driver(device_id) if ok else None
        if driver is None or transport not in driver.device.connection.transports:
            failures.append(now)
            self.counters["auth_failed"] += 1
            await self.svc.store.add_auth_audit(
                "device_auth_failed", device_id[:64], client_addr=client_addr,
                detail={"transport": transport,
                        "reason": "credential" if not ok else "not running or transport off"})
            raise DeviceAuthError("unknown device or wrong credential")
        await driver.touch()                      # any signed-in contact keeps it online
        return driver

    def driver(self, device_id: str) -> NativeDriver | None:
        driver = self.svc.hub.drivers.get(device_id)
        return driver if isinstance(driver, NativeDriver) else None

    def forget(self, device_id: str) -> None:
        """A credential was rotated or the device removed: sign in again."""
        for key in [k for k in self._auth if k[0] == device_id]:
            del self._auth[key]

    # --- device -> hub ------------------------------------------------------------------------

    async def telemetry(self, driver: NativeDriver, payload: Any) -> int:
        self.counters["telemetry"] += 1
        try:
            return await driver.receive(payload)
        except ValueError:
            self.counters["rejected"] += 1
            raise

    async def state(self, driver: NativeDriver, payload: Any) -> None:
        """``{"state": "online", "firmware": "1.4.2"}``: the firmware version is kept."""
        await driver.state(payload)
        firmware = payload.get("firmware") if isinstance(payload, dict) else None
        if isinstance(firmware, str) and firmware.strip():
            await self.svc.store.versions.report_firmware(driver.device.device_id,
                                                          firmware.strip())

    async def ota(self, driver: NativeDriver, payload: Any) -> str:
        """The device reports on a firmware job: downloading / installed / failed."""
        if not isinstance(payload, dict) or payload.get("status") not in (
                "downloading", "installed", "failed"):
            raise ValueError('ota is {"job_id": ..., "status": "downloading|installed|failed"}')
        try:
            job_id = UUID(str(payload.get("job_id")))
        except ValueError:
            raise ValueError("job_id is not a UUID") from None
        version = payload.get("version") if isinstance(payload.get("version"), str) else None
        record = await self.svc.store.versions.advance_job(
            job_id, driver.device.device_id, payload["status"], version=version,
            error=str(payload.get("error"))[:500] if payload["status"] == "failed" else None)
        if record is not None and payload["status"] == "installed" and version:
            await self.svc.store.versions.report_firmware(driver.device.device_id, version)
        return record.status if record else "ignored"

    async def send_ota(self, device_id: str, message: dict[str, Any]) -> str:
        """Offer a firmware job: TCP session, platform broker (d/<id>/ota), else HTTP queue."""
        driver = self.driver(device_id)
        transports = driver.device.connection.transports if driver else ["http"]
        session = self.tcp.get(device_id)
        if session is not None and "tcp" in transports:
            await session(message)
            transport = "tcp"
        elif self.mqtt is not None and self.mqtt.connected and "mqtt" in transports:
            await self.mqtt.publish(f"d/{device_id}/ota", message)
            transport = "mqtt"
        else:
            self.queues.setdefault(device_id, deque(maxlen=MAX_QUEUED)).append(message)
            return "http"
        await self.svc.store.versions.advance_job(UUID(message["job_id"]), device_id, "sent")
        return transport

    async def ack(self, driver: NativeDriver, payload: Any, transport: str) -> str:
        """Advance a command from the device's ack; returns the new status (or 'ignored')."""
        if not isinstance(payload, dict):
            raise ValueError('ack is {"command_id": ..., "status": "received|done|failed"}')
        status = {"received": "delivered", "done": "executed", "failed": "failed"}.get(
            str(payload.get("status")))
        try:
            command_id = UUID(str(payload.get("command_id")))
        except ValueError:
            raise ValueError("command_id is not a UUID") from None
        if status is None:
            raise ValueError("status must be received, done or failed")
        self.counters["acks"] += 1
        error = str(payload.get("error"))[:500] if status == "failed" else None
        record = await self.svc.store.commands.advance(
            command_id, status, self.clock(), transport=transport, error=error,
            device_id=driver.device.device_id)
        driver.ack(payload | {"command_id": str(command_id)})
        return record.status if record else "ignored"

    # --- hub -> device (the NativeDriver's channel) -------------------------------------------

    async def send(self, device: NativeDevice, command: dict[str, Any]) -> str:
        expires = self.clock() + timedelta(seconds=device.connection.command_timeout_s)
        message = command | {"expires_at": expires.isoformat()}
        transports = device.connection.transports
        session = self.tcp.get(device.device_id)
        if session is not None and "tcp" in transports:
            await session({"type": "command", **message})
            transport = "tcp"
        elif self.mqtt is not None and self.mqtt.connected and "mqtt" in transports:
            await self.mqtt.publish_command(device.device_id, message)
            transport = "mqtt"
        elif "http" in transports:
            queue = self.queues.setdefault(device.device_id, deque(maxlen=MAX_QUEUED))
            queue.append(message)
            self.counters["commands"] += 1
            return "http"                     # "sent" once the device fetches it
        else:
            raise DriverError("the device is not connected on any of its transports")
        self.counters["commands"] += 1
        await self.svc.store.commands.advance(UUID(command["command_id"]), "sent",
                                              self.clock(), transport=transport)
        return transport

    async def fetch(self, driver: NativeDriver, limit: int = 20) -> list[dict[str, Any]]:
        """Commands waiting for an HTTP device (they count as sent now); expired ones drop."""
        queue = self.queues.get(driver.device.device_id)
        out: list[dict[str, Any]] = []
        now = self.clock()
        while queue and len(out) < limit:
            message = queue.popleft()
            if message.get("type") == "ota":           # a firmware offer, not a command
                out.append(message)
                await self.svc.store.versions.advance_job(
                    UUID(message["job_id"]), driver.device.device_id, "sent")
                continue
            if datetime.fromisoformat(message["expires_at"]) < now:
                continue
            out.append(message)
            await self.svc.store.commands.advance(UUID(message["command_id"]), "sent", now,
                                                  transport="http")
        return out

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.mqtt is not None:
                await self.mqtt.stop()
        await asyncio.sleep(0)
