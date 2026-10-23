"""Health of the hub itself, checked every ``HUB_HEALTH_INTERVAL_S`` on the active node.

Each problem is a system alarm (``system:<check>``) like any device alarm: it shows in the alarm
list and goes out through the notification rules (e-mail, Telegram, SMS), and it clears by
itself when the condition is gone.

| check     | alarm                 | condition                                                    |
|-----------|-----------------------|--------------------------------------------------------------|
| database  | system:database       | the database does not answer (3 checks in a row) — critical  |
| disk      | system:disk:<volume>  | free space below HUB_DISK_WARN_PERCENT (high) / _CRITICAL_   |
| backup    | system:backup         | last backup failed, or none for HUB_BACKUP_MAX_AGE_HOURS     |
| restore   | system:restore        | the last restore test failed — critical                      |
| clock     | system:clock          | clock differs from the database server (or HUB_NTP_SERVER)   |
|           |                       | by more than HUB_CLOCK_MAX_SKEW_S                            |
| storage   | system:storage        | samples waiting for the database above half the buffer       |
|           |                       | (high), or samples lost because the buffer was full          |
|           |                       | (critical)                                                   |
| loop      | system:loop           | the hub's event loop was blocked for more than 2 s (medium)  |

While the database is down the alarm cannot be stored; it is still sent to the notification
channels, and written to the alarm history (raised and cleared) once the database is back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hub.alarms import AlarmRaise
from hub.drivers.base import utcnow
from hub.models import AlarmSeverity

log = logging.getLogger(__name__)

DB_FAILURES_FOR_ALARM = 3
LOOP_BLOCKED_S = 2.0
_NTP_EPOCH = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01


@dataclass
class CheckResult:
    name: str
    status: str = "ok"                 # ok | warning | critical | unknown
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.name, "status": self.status, "message": self.message,
                "details": self.details}


# --------------------------------------------------------------------------------------------
# Small helpers (pure / stdlib)
# --------------------------------------------------------------------------------------------


def volume_of(path: Path) -> str:
    """The volume a path is on: ``C:`` on Windows, the mount point elsewhere."""
    p = path.resolve()
    while not p.exists():
        p = p.parent
    if os.name == "nt":
        return p.drive or str(p.anchor)
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return str(p)


async def sntp_offset(server: str, timeout: float = 2.0) -> float:
    """Offset of the local clock against an NTP server in seconds (positive: local is ahead)."""
    host, _, port = server.partition(":")
    loop = asyncio.get_running_loop()
    done: asyncio.Future[tuple[bytes, float]] = loop.create_future()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: Any) -> None:
            if not done.done():
                done.set_result((data, time.time()))

        def error_received(self, exc: Exception) -> None:
            if not done.done():
                done.set_exception(exc)

    transport, _ = await loop.create_datagram_endpoint(
        _Proto, remote_addr=(host, int(port or 123)), family=socket.AF_INET)
    try:
        t1 = time.time()
        transport.sendto(b"\x23" + bytes(47))               # LI 0, version 4, mode 3 (client)
        data, t4 = await asyncio.wait_for(done, timeout)
    finally:
        transport.close()
    if len(data) < 48:
        raise ValueError("short NTP answer")

    def ts(offset: int) -> float:
        seconds, fraction = struct.unpack("!II", data[offset : offset + 8])
        return seconds - _NTP_EPOCH + fraction / 2**32

    t2, t3 = ts(32), ts(40)
    return -((t2 - t1) + (t3 - t4)) / 2     # NTP offset is server - local; report local - server


# --------------------------------------------------------------------------------------------
# The watch
# --------------------------------------------------------------------------------------------


class HealthWatch:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.results: dict[str, CheckResult] = {}
        self.last_run: datetime | None = None
        self._open: set[str] = set()
        self._db_failures = 0
        self._db_down_since: datetime | None = None
        self._dropped_seen: int | None = None
        self._loop_lag = 0.0
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def settings(self) -> Any:
        return self.services.settings

    async def start(self) -> None:
        with contextlib.suppress(Exception):
            open_sources = await asyncio.wait_for(self.services.store.open_alarm_sources(), 10)
            self._open = {s for s in open_sources if s.startswith("system:")
                          and not s.startswith(("system:cert:", "system:audit"))}
        self._dropped_seen = self.services.hub.stats.samples_dropped
        self._tasks = [asyncio.create_task(self._loop(), name="health-watch"),
                       asyncio.create_task(self._loop_monitor(), name="health-loop-lag")]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("health watch failed")
            await asyncio.sleep(self.settings.health_interval_s)

    async def _loop_monitor(self) -> None:
        """How late the event loop wakes up: a long block stalls every driver at once."""
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + 1.0
            await asyncio.sleep(1.0)
            self._loop_lag = max(self._loop_lag, loop.time() - expected)

    async def run_once(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        db = await self.check_database(now)
        results = [db, *await self.check_disks(now), await self.check_storage(now),
                   await self.check_loop(now)]
        if db.status == "ok":
            results += [await self.check_backup(now), await self.check_restore(now),
                        await self.check_clock(now)]
        else:   # these read the database: unknown until it is back
            results += [CheckResult(n, "unknown", "database unreachable")
                        for n in ("backup", "restore", "clock")]
        self.results = {r.name: r for r in results}
        self.last_run = now
        return [r.as_dict() for r in results]

    # --- checks -------------------------------------------------------------------------------

    async def check_database(self, now: datetime) -> CheckResult:
        try:
            await asyncio.wait_for(self.services.store.ping(), 5)
        except Exception as exc:  # noqa: BLE001
            self._db_failures += 1
            self._db_down_since = self._db_down_since or now
            buffered = self.services.hub.stats.buffered
            message = (f"database unreachable since {self._db_down_since:%Y-%m-%d %H:%M} UTC "
                       f"({type(exc).__name__}); {buffered} samples waiting in memory")
            if self._db_failures >= DB_FAILURES_FOR_ALARM:
                await self._raise("system:database", AlarmSeverity.CRITICAL, message,
                                  self._db_down_since)
            return CheckResult("database", "critical", message,
                               {"failures": self._db_failures, "buffered": buffered})
        if self._db_down_since is not None and "system:database" in self._open:
            await self._record_outage(now)
        self._db_failures, self._db_down_since = 0, None
        return CheckResult("database", "ok", "database answers")

    async def _record_outage(self, now: datetime) -> None:
        """The outage alarm could not be stored while it lasted: write it now, then clear it."""
        store = self.services.store
        started = self._db_down_since or now
        with contextlib.suppress(Exception):
            if "system:database" not in await store.open_alarm_sources():
                await store.open_alarm(
                    source="system:database", kind="state", severity="critical",
                    message=f"database unreachable from {started:%Y-%m-%d %H:%M:%S} UTC "
                            f"to {now:%H:%M:%S} UTC", value=None, raised_at=started)
        await self._clear("system:database", now)

    def _disk_paths(self) -> list[Path]:
        s = self.settings
        paths = [Path(s.config_dir), Path.cwd()]
        if s.backup_dir:
            paths.append(Path(s.backup_dir))
        if s.log_file:
            paths.append(Path(s.log_file).parent)
        return paths

    async def check_disks(self, now: datetime) -> list[CheckResult]:
        s = self.settings
        volumes: dict[str, Path] = {}
        for path in self._disk_paths():
            with contextlib.suppress(OSError):
                volumes.setdefault(volume_of(path), path)
        results = []
        seen: set[str] = set()
        for volume, path in sorted(volumes.items()):
            usage = shutil.disk_usage(path)
            free_pct = usage.free / usage.total * 100 if usage.total else 100.0
            name = "".join(c if c.isalnum() else "_" for c in volume).strip("_").lower() or "root"
            source = f"system:disk:{name}"
            seen.add(source)
            details = {"volume": volume, "free_gb": round(usage.free / 1e9, 2),
                       "total_gb": round(usage.total / 1e9, 2), "free_percent": round(free_pct, 1)}
            message = f"{volume.rstrip(':')}: {free_pct:.1f}% free ({usage.free / 1e9:.1f} GB)"
            if free_pct < s.disk_critical_percent:
                await self._raise(source, AlarmSeverity.CRITICAL, message, now)
                results.append(CheckResult(f"disk:{name}", "critical", message, details))
            elif free_pct < s.disk_warn_percent:
                await self._raise(source, AlarmSeverity.HIGH, message, now)
                results.append(CheckResult(f"disk:{name}", "warning", message, details))
            else:
                await self._clear(source, now)
                results.append(CheckResult(f"disk:{name}", "ok", message, details))
        for source in [x for x in self._open if x.startswith("system:disk:") and x not in seen]:
            await self._clear(source, now)
        return results

    async def check_storage(self, now: datetime) -> CheckResult:
        hub = self.services.hub
        stats = hub.stats
        capacity = getattr(hub, "max_buffer", 200_000)
        dropped_now = stats.samples_dropped - (self._dropped_seen or 0)
        self._dropped_seen = stats.samples_dropped
        details = {"buffered": stats.buffered, "capacity": capacity,
                   "dropped": stats.samples_dropped, "write_errors": stats.write_errors}
        if dropped_now > 0:
            message = (f"{dropped_now} samples were lost because the storage buffer was full "
                       "(database too slow or unreachable)")
            await self._raise("system:storage", AlarmSeverity.CRITICAL, message, now)
            return CheckResult("storage", "critical", message, details)
        if stats.buffered > capacity / 2:
            message = f"{stats.buffered} samples waiting for the database ({capacity} max)"
            await self._raise("system:storage", AlarmSeverity.HIGH, message, now)
            return CheckResult("storage", "warning", message, details)
        if stats.buffered < capacity / 10:
            await self._clear("system:storage", now)
        return CheckResult("storage", "ok", f"{stats.buffered} samples waiting", details)

    async def check_loop(self, now: datetime) -> CheckResult:
        lag, self._loop_lag = self._loop_lag, 0.0
        details = {"max_lag_s": round(lag, 3)}
        if lag > LOOP_BLOCKED_S:
            message = f"the hub was unresponsive for {lag:.1f} s (event loop blocked)"
            await self._raise("system:loop", AlarmSeverity.MEDIUM, message, now)
            return CheckResult("loop", "warning", message, details)
        await self._clear("system:loop", now)
        return CheckResult("loop", "ok", f"largest delay {lag:.2f} s", details)

    async def check_backup(self, now: datetime) -> CheckResult:
        s = self.settings
        if not s.backup_dir:
            return CheckResult("backup", "unknown", "backups are not configured (HUB_BACKUP_DIR)")
        store = self.services.store
        ok = await store.events("backup_ok", 1)
        failed = await store.events("backup_failed", 1)
        last_ok = ok[0]["ts"] if ok else None
        last_failed = failed[0] if failed else None
        max_age = timedelta(hours=s.backup_max_age_hours)
        details = {"last_ok": last_ok.isoformat() if last_ok else None}
        if last_failed and (last_ok is None or last_failed["ts"] > last_ok):
            message = f"last backup failed: {last_failed['detail'].get('error', '?')[:200]}"
            await self._raise("system:backup", AlarmSeverity.HIGH, message, now)
            return CheckResult("backup", "warning", message, details)
        reference = last_ok or self.services.started_at
        if now - reference > max_age:
            message = (f"no successful backup since {last_ok:%Y-%m-%d %H:%M} UTC" if last_ok
                       else f"no backup in the {s.backup_max_age_hours} h since the hub started")
            await self._raise("system:backup", AlarmSeverity.HIGH, message, now)
            return CheckResult("backup", "warning", message, details)
        await self._clear("system:backup", now)
        return CheckResult("backup", "ok", f"last backup {last_ok:%Y-%m-%d %H:%M} UTC"
                           if last_ok else "no backup yet (not due)", details)

    async def check_restore(self, now: datetime) -> CheckResult:
        if self.settings.restore_test == "off":
            return CheckResult("restore", "unknown", "restore tests are off (HUB_RESTORE_TEST)")
        store = self.services.store
        ok = await store.events("restore_ok", 1)
        failed = await store.events("restore_failed", 1)
        if failed and (not ok or failed[0]["ts"] > ok[0]["ts"]):
            d = failed[0]["detail"]
            message = (f"restore test ({d.get('mode')}) of {d.get('file')} failed: "
                       f"{str(d.get('error', '?'))[:200]}")
            await self._raise("system:restore", AlarmSeverity.CRITICAL, message, now)
            return CheckResult("restore", "critical", message, d)
        await self._clear("system:restore", now)
        if ok:
            d = ok[0]["detail"]
            return CheckResult("restore", "ok", f"last test ({d.get('mode')}) passed on "
                               f"{ok[0]['ts']:%Y-%m-%d}", d)
        return CheckResult("restore", "unknown", "no restore test yet (runs after a backup)")

    async def check_clock(self, now: datetime) -> CheckResult:
        s = self.settings
        details: dict[str, Any] = {}
        worst = 0.0
        problems = []
        with contextlib.suppress(Exception):
            t0 = time.time()
            db_now = await asyncio.wait_for(self.services.store.server_time(), 5)
            t1 = time.time()
            skew = (t0 + t1) / 2 - db_now.timestamp()
            details["database_skew_s"] = round(skew, 3)
            worst = max(worst, abs(skew))
            if abs(skew) > s.clock_max_skew_s:
                problems.append(f"{skew:+.1f} s against the database server")
        if s.ntp_server:
            try:
                offset = await sntp_offset(s.ntp_server)
                details["ntp_offset_s"] = round(offset, 3)
                worst = max(worst, abs(offset))
                if abs(offset) > s.clock_max_skew_s:
                    problems.append(f"{offset:+.1f} s against {s.ntp_server}")
            except Exception as exc:  # noqa: BLE001
                details["ntp_error"] = f"{type(exc).__name__}: {exc}"
        if problems:
            message = ("the server clock is off by " + " and ".join(problems)
                       + ": timestamps, schedules and certificates depend on it")
            await self._raise("system:clock", AlarmSeverity.HIGH, message, now)
            return CheckResult("clock", "warning", message, details)
        await self._clear("system:clock", now)
        return CheckResult("clock", "ok", f"within {worst:.2f} s", details)

    # --- alarms -------------------------------------------------------------------------------

    async def _raise(self, source: str, severity: AlarmSeverity, message: str,
                     ts: datetime) -> None:
        if source in self._open:
            return
        self._open.add(source)
        log.error("HEALTH: %s", message)
        await self.services.hub.raise_system_alarm(AlarmRaise(
            source=source, kind="state", severity=severity, message=message, value=None,
            ts=ts.astimezone(UTC)))

    async def _clear(self, source: str, now: datetime) -> None:
        if source not in self._open:
            return
        self._open.discard(source)
        log.info("HEALTH: %s cleared", source)
        with contextlib.suppress(Exception):
            await self.services.hub.clear_system_alarm(source, now)

    def describe(self) -> dict[str, Any]:
        return {"last_run": self.last_run.isoformat() if self.last_run else None,
                "checks": [r.as_dict() for r in self.results.values()],
                "open_alarms": sorted(self._open)}
