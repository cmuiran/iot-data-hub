"""Database housekeeping, run by the active hub node: hourly roll-up, retention, backups.

* Roll-up (every 5 min): complete hours (in ``timezone``) of good numeric samples ->
  ``samples_hourly`` (avg/min/max/first/last/n). The last 2 hours are recomputed so late samples are included.
  Averages are over *stored* samples (see hub/history.py), not time-weighted.
* Retention (hourly): raw samples older than ``retention_raw_days`` and hourly rows older than
  ``retention_hourly_days`` are removed. Raw data is never removed before it was rolled up.
  With TimescaleDB whole chunks are dropped, otherwise rows are deleted in batches.
* Backup (daily at ``backup_time`` in ``timezone``, when ``backup_dir`` is set): ``pg_dump
  --format=custom``; the newest ``backup_keep`` files are kept. With ``data_key``
  (HUB_DATA_KEY) the dump is encrypted (``.dump.enc``, AES-256-GCM, see backend/crypto.py) and
  the plain file is removed. Restore: ``hub-admin decrypt-backup`` (if encrypted), then
  ``pg_restore --clean --dbname <url> <file>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.conninfo import conninfo_to_dict

from hub.drivers.base import utcnow

log = logging.getLogger(__name__)

ROLLUP_INTERVAL_S = 300
RETENTION_INTERVAL_S = 3600


@dataclass(frozen=True)
class MaintenanceSettings:
    retention_raw_days: int = 365
    retention_hourly_days: int = 3650
    backup_dir: Path | None = None
    backup_time: time = time(2, 30)
    backup_keep: int = 14
    timezone: str = "Asia/Tehran"
    pg_dump: str | None = None
    data_key: str | None = None      # encrypt backups when set


def hour_floor(ts: datetime, tz: str = "UTC") -> datetime:
    """Start of the hour containing ts, in time zone tz (returned in ts's zone)."""
    local = ts.astimezone(ZoneInfo(tz)).replace(minute=0, second=0, microsecond=0)
    return local.astimezone(ts.tzinfo)


def find_pg_dump(explicit: str | None = None) -> str:
    """pg_dump from: the setting, PATH, a standard Windows install, or pgserver (dev)."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    found = shutil.which("pg_dump")
    if found:
        candidates.append(found)
    candidates += sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"), reverse=True)
    with contextlib.suppress(ImportError):
        import pgserver

        base = Path(pgserver.__file__).parent
        candidates += [str(p) for p in base.rglob("pg_dump*") if p.stem == "pg_dump"]
    for c in candidates:
        if Path(c).is_file():
            return c
    raise FileNotFoundError("pg_dump not found: install the PostgreSQL client tools or set "
                            "HUB_PG_DUMP to its full path")


def backup_database(database_url: str, out_dir: Path, pg_dump: str | None = None,
                    now: datetime | None = None) -> Path:
    """Write a custom-format dump. The password goes through PGPASSWORD, never the command line."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or utcnow()).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"hub-{stamp}.dump"
    params = conninfo_to_dict(database_url)
    env = dict(os.environ)
    if params.get("password"):
        env["PGPASSWORD"] = str(params.pop("password"))
    args = [find_pg_dump(pg_dump), "--format=custom", "--no-owner", f"--file={target}"]
    for key, flag in (("host", "--host"), ("port", "--port"), ("user", "--username"),
                      ("dbname", "--dbname")):
        if params.get(key):
            args.append(f"{flag}={params[key]}")
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=3600,
                            check=False)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed ({result.returncode}): {result.stderr.strip()[:500]}")
    return target


def encrypt_backup(path: Path, data_key: str) -> Path:
    """Encrypt a dump next to itself and delete the plain file."""
    from backend.crypto import encrypt_file

    target = path.with_name(path.name + ".enc")
    encrypt_file(data_key, path, target)
    path.unlink()
    return target


def prune_backups(out_dir: Path, keep: int) -> list[Path]:
    files = sorted([*out_dir.glob("hub-*.dump"), *out_dir.glob("hub-*.dump.enc")],
                   key=lambda p: p.name, reverse=True)
    removed = files[keep:]
    for f in removed:
        f.unlink(missing_ok=True)
    return removed


def next_backup_at(now: datetime, at: time, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    candidate = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(now.tzinfo)


class Maintenance:
    def __init__(self, store: Any, settings: MaintenanceSettings, *, database_url: str | None,
                 node_id: str | None = None, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.settings = settings
        self.database_url = database_url
        self.node_id = node_id
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self.last_backup: Path | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="hub:maintenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        last_retention = 0.0
        loop = asyncio.get_running_loop()
        next_backup = (next_backup_at(self.clock(), self.settings.backup_time,
                                      self.settings.timezone)
                       if self.settings.backup_dir else None)
        while True:
            try:
                await self.run_rollup()
                if loop.time() - last_retention >= RETENTION_INTERVAL_S:
                    await self.run_retention()
                    last_retention = loop.time()
                if next_backup is not None and self.clock() >= next_backup:
                    await self.run_backup()
                    next_backup = next_backup_at(self.clock(), self.settings.backup_time,
                                                 self.settings.timezone)
            except Exception:  # noqa: BLE001 - try again next round
                log.exception("maintenance round failed")
            await asyncio.sleep(ROLLUP_INTERVAL_S)

    async def run_rollup(self) -> int:
        """Roll up complete hours since the last roll-up (re-doing the last 2 hours)."""
        tz = self.settings.timezone
        end = hour_floor(self.clock(), tz)
        last = await self.store.last_rollup()
        if last is not None:
            start = last - timedelta(hours=1)
        else:
            oldest = await self.store.oldest_sample()
            if oldest is None:
                return 0
            start = hour_floor(oldest, tz)
        if start >= end:
            return 0
        rows = 0
        cursor = start
        while cursor < end:  # one day per statement keeps transactions short
            stop = min(cursor + timedelta(days=1), end)
            rows += await self.store.rollup_hourly(cursor, stop, tz)
            cursor = stop
        return rows

    async def run_retention(self) -> dict[str, int]:
        now = self.clock()
        last = await self.store.last_rollup()
        raw_cutoff = now - timedelta(days=self.settings.retention_raw_days)
        # never delete raw samples that were not rolled up yet (the last bucket covers 1 hour)
        raw_cutoff = min(raw_cutoff, last + timedelta(hours=1)) if last is not None else None
        deleted_raw = 0
        if raw_cutoff is not None:
            timescale = await self.store.has_timescale()
            deleted_raw = await self.store.delete_raw_before(raw_cutoff, timescale)
        deleted_hourly = await self.store.delete_hourly_before(
            now - timedelta(days=self.settings.retention_hourly_days))
        if deleted_raw or deleted_hourly:
            await self.store.add_event("retention", self.node_id,
                                       {"raw_deleted": deleted_raw,
                                        "hourly_deleted": deleted_hourly})
        return {"raw": deleted_raw, "hourly": deleted_hourly}

    async def run_backup(self) -> Path | None:
        if not self.settings.backup_dir or not self.database_url:
            return None
        try:
            path = await asyncio.to_thread(backup_database, self.database_url,
                                           self.settings.backup_dir, self.settings.pg_dump,
                                           self.clock())
            if self.settings.data_key:
                path = await asyncio.to_thread(encrypt_backup, path, self.settings.data_key)
            removed = prune_backups(self.settings.backup_dir, self.settings.backup_keep)
        except Exception as exc:  # noqa: BLE001
            log.error("backup failed: %s", exc)
            await self.store.add_event("backup_failed", self.node_id, {"error": str(exc)[:500]})
            return None
        self.last_backup = path
        log.info("backup written: %s (%d bytes)", path, path.stat().st_size)
        await self.store.add_event("backup_ok", self.node_id,
                                   {"file": str(path), "bytes": path.stat().st_size,
                                    "encrypted": path.suffix == ".enc",
                                    "pruned": [p.name for p in removed]})
        return path
