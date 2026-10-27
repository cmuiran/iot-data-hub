"""TCP listener for native devices: newline-delimited JSON, TLS (HUB_TLS_CERT / HUB_TLS_KEY).

Session::

    device -> {"device_id": "d…", "secret": "<device credential>"}     first line, within 10 s
    hub    -> {"ok": true} | {"ok": false, "error": "..."} (then closes)
    device -> {"type": "telemetry", "ts": ..., "values": {...}}
    device -> {"type": "state", "state": "online"}
    device -> {"type": "ack", "command_id": "...", "status": "done", "value": ...}
    hub    -> {"type": "command", "command_id": "...", "tag": "...", "value": ..., ...}
    hub    -> {"type": "error", "error": "..."}            a message that was refused

Lines are at most 64 KiB; a session idle for longer than the device's stale time (at least
60 s) is closed. A device has one session: a new one replaces the old. Plain TCP only with
HUB_TCP_INGEST_INSECURE=true (development).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from typing import Any

from backend.ingest import DeviceAuthError, Ingest

log = logging.getLogger(__name__)
MAX_LINE = 64 * 1024
AUTH_TIMEOUT_S = 10.0


class TcpIngest:
    def __init__(self, ingest: Ingest, host: str, port: int,
                 ssl_context: ssl.SSLContext | None) -> None:
        self.ingest = ingest
        self.host, self.port = host, port
        self.ssl_context = ssl_context
        self.server: asyncio.Server | None = None
        self.sessions = 0

    @classmethod
    def from_settings(cls, ingest: Ingest, settings: Any) -> TcpIngest | None:
        if settings.tcp_ingest_port is None:
            return None
        context = None
        if settings.tls:
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(settings.tls_cert_file, settings.tls_key_file)
        elif not settings.tcp_ingest_insecure:
            raise RuntimeError("HUB_TCP_INGEST_PORT needs TLS (HUB_TLS_CERT / HUB_TLS_KEY), or "
                               "HUB_TCP_INGEST_INSECURE=true for development")
        return cls(ingest, settings.tcp_ingest_host, settings.tcp_ingest_port, context)

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._session, self.host, self.port,
                                                 ssl=self.ssl_context, limit=MAX_LINE)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("TCP device ingest on %s:%d (%s)", self.host, self.port,
                 "TLS" if self.ssl_context else "plain, development only")

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 5)
            self.server = None

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        addr = peer[0] if isinstance(peer, tuple) else None
        lock = asyncio.Lock()

        async def send(message: dict[str, Any]) -> None:
            async with lock:
                writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
                await writer.drain()

        device_id = None
        try:
            hello = await asyncio.wait_for(self._line(reader), AUTH_TIMEOUT_S)
            if not isinstance(hello, dict):
                raise DeviceAuthError("first line is {\"device_id\": ..., \"secret\": ...}")
            device_id = str(hello.get("device_id", ""))[:64]
            driver = await self.ingest.authenticate(device_id, str(hello.get("secret", "")),
                                                    addr, "tcp")
            old = self.ingest.tcp.get(device_id)
            self.ingest.tcp[device_id] = send
            if old is not None:
                log.info("device %s: a new TCP session replaces the old one", device_id)
            await send({"ok": True})
            self.sessions += 1
            idle = max(60.0, driver.device.connection.stale_after_s or 60.0)
            await driver.state({"state": "online"})
            while True:
                try:
                    message = await asyncio.wait_for(self._line(reader), idle)
                    if message is None:
                        break
                    await self._handle(driver, message)
                except ValueError as exc:              # refused: the session goes on
                    await send({"type": "error", "error": str(exc)})
        except DeviceAuthError as exc:
            with contextlib.suppress(Exception):
                await send({"ok": False, "error": str(exc)})
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        except Exception:  # noqa: BLE001 - one device must never stop the listener
            log.exception("TCP session of %s failed", device_id)
        finally:
            if device_id and self.ingest.tcp.get(device_id) is send:
                del self.ingest.tcp[device_id]
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    @staticmethod
    async def _line(reader: asyncio.StreamReader) -> Any:
        try:
            raw = await reader.readline()
        except (asyncio.LimitOverrunError, ValueError):
            raise ConnectionError("line longer than 64 KiB") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("not JSON") from None

    async def _handle(self, driver: Any, message: Any) -> None:
        if not isinstance(message, dict):
            raise ValueError("a message is a JSON object with a type")
        kind = message.get("type")
        if kind == "telemetry":
            await self.ingest.telemetry(driver, message)
        elif kind == "ack":
            await self.ingest.ack(driver, message, "tcp")
        elif kind == "state":
            await self.ingest.state(driver, message)
        elif kind == "ota":
            await self.ingest.ota(driver, message)
        else:
            raise ValueError("type is telemetry, ack, state or ota")
