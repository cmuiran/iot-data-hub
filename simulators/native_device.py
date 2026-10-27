"""Simulated native device (the platform device contract, hub.drivers.native) for tests and
local development: holds tag values, sends telemetry, carries out commands and acks them,
over HTTP, TCP or MQTT, signed in with its device credential.

Faults for tests: ``refuse`` (ack failed), ``silent`` (no ack, no new value), ``ack_only``
(acks done but never reports the value).

    python -m simulators.native_device --http http://127.0.0.1:8000 --device d… --secret …
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class NativeDeviceSim:
    device_id: str
    secret: str
    values: dict[str, Any] = field(default_factory=dict)
    refuse: bool = False
    silent: bool = False
    ack_only: bool = False
    executed: list[dict[str, Any]] = field(default_factory=list)
    firmware: str = "1.0.0"
    bad_image: bool = False                 # pretend the download is damaged

    def telemetry(self) -> dict[str, Any]:
        return {"values": dict(self.values)}

    def handle(self, command: dict[str, Any]) -> list[dict[str, Any]]:
        """Messages the device sends back for one command (acks, then telemetry)."""
        cid = command.get("command_id")
        if self.silent:
            return []
        if self.refuse:
            return [{"type": "ack", "command_id": cid, "status": "failed",
                     "error": "refused by the simulator"}]
        if any(c.get("command_id") == cid for c in self.executed):     # idempotent
            return [{"type": "ack", "command_id": cid, "status": "done"}]
        self.executed.append(command)
        out = [{"type": "ack", "command_id": cid, "status": "received"}]
        if self.ack_only:
            return out + [{"type": "ack", "command_id": cid, "status": "done"}]
        self.values[command["tag"]] = command["value"]
        return out + [{"type": "ack", "command_id": cid, "status": "done",
                       "value": command["value"]},
                      {"type": "telemetry", **self.telemetry()}]

    # --- HTTP ---------------------------------------------------------------------------------

    async def run_http(self, client: httpx.AsyncClient, period_s: float = 0.1,
                       stop: asyncio.Event | None = None) -> None:
        """Send telemetry and fetch commands every period (client: base_url = the hub)."""
        stop = stop or asyncio.Event()
        base = f"/ingest/v1/devices/{self.device_id}"
        headers = {"Authorization": f"Bearer {self.secret}"}
        await client.post(f"{base}/telemetry", headers=headers, json=self.telemetry())
        while not stop.is_set():
            r = await client.get(f"{base}/commands", headers=headers)
            for command in r.json() if r.status_code == 200 else []:
                if command.get("type") == "ota":
                    await self._ota_http(client, headers, base, command)
                    continue
                for message in self.handle(command):
                    kind = message.pop("type")
                    await client.post(f"{base}/{kind}", headers=headers, json=message)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), period_s)

    async def _ota_http(self, client: httpx.AsyncClient, headers: dict[str, str], base: str,
                        offer: dict[str, Any]) -> None:
        """Download, check the SHA-256, install (just the version here), report."""
        import hashlib

        job = offer["job_id"]
        await client.post(f"{base}/ota", headers=headers,
                          json={"job_id": job, "status": "downloading"})
        r = await client.get(offer["url"], headers=headers)
        digest = hashlib.sha256(r.content).hexdigest()
        ok = r.status_code == 200 and not self.bad_image and digest == offer["sha256"]
        if ok:
            self.firmware = offer["version"]
        await client.post(f"{base}/ota", headers=headers, json={
            "job_id": job, "status": "installed" if ok else "failed",
            "version": self.firmware, "error": None if ok else "checksum mismatch"})

    # --- TCP ----------------------------------------------------------------------------------

    async def run_tcp(self, host: str, port: int, *, tls: ssl.SSLContext | None = None,
                      ready: asyncio.Event | None = None) -> dict[str, Any]:
        """One session; returns the hub's answer to the sign-in when it refuses it."""
        reader, writer = await asyncio.open_connection(host, port, ssl=tls)

        async def send(message: dict[str, Any]) -> None:
            writer.write(json.dumps(message).encode() + b"\n")
            await writer.drain()

        try:
            await send({"device_id": self.device_id, "secret": self.secret})
            answer = json.loads(await reader.readline() or b"{}")
            if not answer.get("ok"):
                return answer
            await send({"type": "telemetry", **self.telemetry()})
            if ready is not None:
                ready.set()
            while line := await reader.readline():
                message = json.loads(line)
                if message.get("type") == "command":
                    for reply in self.handle(message):
                        await send(reply)
            return answer
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # --- MQTT ---------------------------------------------------------------------------------

    async def run_mqtt(self, host: str, port: int, prefix: str = "",
                       ready: asyncio.Event | None = None) -> None:
        import aiomqtt

        base = f"{prefix}d/{self.device_id}"
        async with aiomqtt.Client(host, port, username=self.device_id, password=self.secret,
                                  identifier=f"sim-{self.device_id}") as client:
            await client.subscribe(f"{base}/cmd", qos=1)
            await client.publish(f"{base}/telemetry", json.dumps(self.telemetry()), qos=1)
            if ready is not None:
                ready.set()
            async for message in client.messages:
                command = json.loads(message.payload)
                for reply in self.handle(command):
                    kind = reply.pop("type")
                    await client.publish(f"{base}/{kind}", json.dumps(reply), qos=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulators.native_device")
    parser.add_argument("--device", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--http", help="hub address, e.g. http://127.0.0.1:8000")
    parser.add_argument("--tcp", help="host:port of HUB_TCP_INGEST_PORT (plain TCP)")
    parser.add_argument("--value", action="append", default=[], metavar="TAG=JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    values = {k: json.loads(v) for k, _, v in (a.partition("=") for a in args.value)}
    sim = NativeDeviceSim(args.device, args.secret, values)

    async def run() -> None:
        if args.tcp:
            host, _, port = args.tcp.rpartition(":")
            print(await sim.run_tcp(host, int(port)))
        elif args.http:
            async with httpx.AsyncClient(base_url=args.http, timeout=10) as client:
                await sim.run_http(client, period_s=1.0)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
