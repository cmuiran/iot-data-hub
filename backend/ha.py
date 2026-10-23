"""High availability: two hub-api nodes, one active, one standby (HUB_HA=on).

Leadership is a PostgreSQL session advisory lock held on a dedicated connection: whoever holds
it is active, and PostgreSQL guarantees only one holder. The active node runs the drivers,
control, notifications, schedules, housekeeping and the OPC UA server; the standby node runs
none of them and answers 503 on the API (``/api/health`` too, with ``"role": "standby"``), so a
load balancer / reverse proxy sends traffic only to the active node.

* The standby retries the lock every ``RETRY_S`` seconds and takes over when it gets it.
* The active node checks its lock connection every ``RETRY_S`` seconds. If the connection is
  lost (database restart, network split) the lock is gone too: the node stops all device
  traffic *at once* (fencing) before trying to become active again, so two nodes never write to
  the same devices.
* Each node records its role and a heartbeat in ``hub_nodes`` (GET /api/system/nodes) and every
  change of role in ``system_events`` (``failover``).

Requirements: both nodes use the same database and the same config directory (shared folder or
deployed together), and the same HUB_JWT_SECRET so tokens stay valid after a failover.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any

import psycopg

from backend.api.system import VERSION

log = logging.getLogger(__name__)
LOCK_KEY = 0x4855_4231  # "HUB1"
RETRY_S = 3.0


class HaCoordinator:
    def __init__(self, services: Any, database_url: str, *, retry_s: float = RETRY_S) -> None:
        self.services = services
        self.database_url = database_url
        self.retry_s = retry_s
        self.node_id = services.settings.node_id
        self._lock_conn: psycopg.AsyncConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self.transitions: list[str] = []

    @property
    def active(self) -> bool:
        return self.services.role == "active"

    async def start(self) -> None:
        self.services.role = "standby"
        await self._heartbeat()
        self._task = asyncio.create_task(self._loop(), name="ha")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.active:
            await self._demote("shutdown")
        await self._close_lock()
        self.services.role = "stopped"
        with contextlib.suppress(Exception):
            await self._heartbeat()

    async def _loop(self) -> None:
        while True:
            try:
                if self.active:
                    if not await self._still_holding():
                        await self._demote("lost the database lock")
                elif await self._try_acquire():
                    await self._promote()
                await self._heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("HA loop error")
            await asyncio.sleep(self.retry_s)

    async def _try_acquire(self) -> bool:
        try:
            if self._lock_conn is None or self._lock_conn.closed:
                self._lock_conn = await psycopg.AsyncConnection.connect(self.database_url,
                                                                        autocommit=True)
            row = await (await self._lock_conn.execute("SELECT pg_try_advisory_lock(%s)",
                                                       (LOCK_KEY,))).fetchone()
            return bool(row and row[0])
        except psycopg.Error as exc:
            log.warning("HA: cannot reach the database for the lock: %s", exc)
            await self._close_lock()
            return False

    async def _still_holding(self) -> bool:
        if self._lock_conn is None or self._lock_conn.closed:
            return False
        try:
            row = await (await asyncio.wait_for(self._lock_conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND pid = pg_backend_pid() AND objid = %s", (LOCK_KEY & 0xFFFFFFFF,)),
                self.retry_s * 2)).fetchone()
            return bool(row and row[0])
        except (psycopg.Error, TimeoutError, OSError):
            return False

    async def _close_lock(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _promote(self) -> None:
        log.warning("HA: node %s becomes ACTIVE", self.node_id)
        await self.services.start()
        self.transitions.append("active")
        await self._heartbeat()
        await self._event("active")

    async def _demote(self, reason: str) -> None:
        log.error("HA: node %s goes to STANDBY (%s): stopping all device traffic",
                  self.node_id, reason)
        self.services.role = "standby"
        with contextlib.suppress(Exception):
            await self.services.stop()
        await self._close_lock()
        self.transitions.append("standby")
        await self._heartbeat()
        await self._event("standby", reason)

    async def _heartbeat(self) -> None:
        with contextlib.suppress(Exception):
            await self.services.store.heartbeat_node(self.node_id, socket.gethostname(),
                                                     self.services.role, VERSION)

    async def _event(self, role: str, reason: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.services.store.add_event("failover", self.node_id,
                                                {"role": role, "reason": reason})
