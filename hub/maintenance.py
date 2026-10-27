"""Database housekeeping, run by the active hub node: hourly roll-up, retention, backups.

* Roll-up (every 5 min): complete hours (in ``timezone``) of good numeric samples ->
  ``samples_hourly`` (avg/min/max/first/last/n). The last 2 hours are recomputed so late samples
  are included.
  Averages are over *stored* samples (see hub/history.py), not time-weighted.
* Retention (hourly): raw samples older than ``retention_raw_days`` and hourly rows older than
  ``retention_hourly_days`` are removed. Raw data is never removed before it was rolled up.
  With TimescaleDB whole chunks are dropped, otherwise rows are deleted in batches.
* Backup (daily at ``backup_time`` in ``timezone``, when ``backup_dir`` is set): ``pg_dump
  --format=custom``; the newest ``backup_keep`` files are kept. With ``data_key``
  (HUB_DATA_KEY) the dump is encrypted (``.dump.enc``, AES-256-GCM, see backend/crypto.py) and
  the plain file is removed. Restore: ``hub-admin restore-backup <file>`` (decrypts as needed).
* Restore test (``restore_test``): ``archive`` reads the table of contents of every new backup
  (``pg_restore --list``: the file is complete and readable, with the key when encrypted);
  ``full`` additionally restores it into a temporary database every ``restore_test_days``,
  compares the schema version, counts the key tables and verifies both audit hash chains in the
  copy, then drops it (the database user needs the CREATEDB privilege). Results are
  ``restore_ok`` / ``restore_failed`` system events; a failure raises the system alarm
  ``system:restore`` (see backend/health_watch.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from hub.drivers.base import utcnow

log = logging.getLogger(__name__)
COLLECTION_ERRORS_DAYS = 90             # history of collection errors

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
    restore_test: str = "archive"    # off | archive | full
    restore_test_days: int = 7       # how often the full restore test runs


def hour_floor(ts: datetime, tz: str = "UTC") -> datetime:
    """Start of the hour containing ts, in time zone tz (returned in ts's zone)."""
    local = ts.astimezone(ZoneInfo(tz)).replace(minute=0, second=0, microsecond=0)
    return local.astimezone(ts.tzinfo)


def find_pg_tool(name: str, pg_dump: str | None = None) -> str:
    """A PostgreSQL client tool (pg_dump, pg_restore): next to HUB_PG_DUMP, on PATH, in a
    standard Windows install, or from pgserver (development)."""
    candidates: list[str] = []
    if pg_dump:
        explicit = Path(pg_dump)
        candidates.append(str(explicit if name == "pg_dump" else
                              explicit.with_name(explicit.name.replace("pg_dump", name))))
    found = shutil.which(name)
    if found:
        candidates.append(found)
    candidates += sorted(glob.glob(rf"C:\Program Files\PostgreSQL\*\bin\{name}.exe"), reverse=True)
    with contextlib.suppress(ImportError):
        import pgserver

        base = Path(pgserver.__file__).parent
        candidates += [str(p) for p in base.rglob(f"{name}*") if p.stem == name]
    for c in candidates:
        if Path(c).is_file():
            return c
    raise FileNotFoundError(f"{name} not found: install the PostgreSQL client tools or set "
                            "HUB_PG_DUMP to the full path of pg_dump")


def find_pg_dump(explicit: str | None = None) -> str:
    return find_pg_tool("pg_dump", explicit)


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


@contextlib.contextmanager
def plain_backup(path: Path, data_key: str | None) -> Iterator[Path]:
    """The backup as a plain dump: the file itself, or a decrypted temporary copy (removed
    afterwards). An encrypted file without the right key raises."""
    if not path.name.endswith(".enc"):
        yield path
        return
    if not data_key:
        raise RuntimeError(f"{path.name} is encrypted: HUB_DATA_KEY is needed to read it")
    from backend.crypto import decrypt_file

    with tempfile.TemporaryDirectory(prefix="hub-restore-") as tmp:
        plain = Path(tmp) / path.name.removesuffix(".enc")
        decrypt_file(data_key, path, plain)
        yield plain


REQUIRED_TABLES = ("schema_migrations", "devices", "tags", "users", "write_audit", "auth_audit")


def _run(args: list[str], env: dict[str, str] | None = None, timeout: int = 3600
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout,
                          check=False)


def check_backup_archive(path: Path, data_key: str | None = None,
                         pg_dump: str | None = None) -> dict[str, Any]:
    """The archive is complete and readable: its table of contents lists the hub's tables."""
    with plain_backup(path, data_key) as plain:
        result = _run([find_pg_tool("pg_restore", pg_dump), "--list", str(plain)], timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore --list failed: {result.stderr.strip()[:400]}")
    tables = sorted({line.split()[-2] for line in result.stdout.splitlines()
                     if " TABLE DATA " in line and len(line.split()) >= 2})
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise RuntimeError(f"backup has no data for {', '.join(missing)}")
    return {"mode": "archive", "file": path.name, "tables": len(tables)}


def _counts(conn: psycopg.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in ("devices", "tags", "users", "write_audit", "auth_audit"):  # fixed names, no input
        row = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()  # nosec B608
        out[t] = int(row[0])  # type: ignore[index]
    return out


def restore_test(database_url: str, path: Path, data_key: str | None = None,
                 pg_dump: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Restore the backup into a temporary database on the same server and check it."""
    stamp = (now or utcnow()).strftime("%Y%m%d%H%M%S")
    scratch = f"hub_restore_check_{stamp}"
    params = conninfo_to_dict(database_url)
    env = dict(os.environ)
    if params.get("password"):
        env["PGPASSWORD"] = str(params["password"])
    scratch_url = make_conninfo(database_url, dbname=scratch)
    with psycopg.connect(database_url, autocommit=True) as source:
        versions = sorted(r[0] for r in source.execute("SELECT version FROM schema_migrations"))
        timescale = source.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone() is not None
        source.execute(f'CREATE DATABASE "{scratch}"')
    try:
        if timescale:
            with psycopg.connect(scratch_url, autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                conn.execute("SELECT timescaledb_pre_restore()")
        with plain_backup(path, data_key) as plain:
            args = [find_pg_tool("pg_restore", pg_dump), "--no-owner", "--exit-on-error",
                    f"--dbname={scratch}", str(plain)]
            for key, flag in (("host", "--host"), ("port", "--port"), ("user", "--username")):
                if params.get(key):
                    args.insert(-1, f"{flag}={params[key]}")
            result = _run(args, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"pg_restore failed: {result.stderr.strip()[:400]}")
        with psycopg.connect(scratch_url, autocommit=True) as conn:
            if timescale:
                conn.execute("SELECT timescaledb_post_restore()")
            restored = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
            counts = _counts(conn)
            chains = {t: conn.execute("SELECT * FROM audit_verify(%s)", (t,)).fetchone()
                      for t in ("write_audit", "auth_audit")}
        problems = []
        if restored != versions:
            problems.append(f"schema {restored[-1:]} differs from the database {versions[-1:]}")
        for table, row in chains.items():
            if row is not None and row[1] is not None:
                problems.append(f"{table} chain broken at id {row[1]}: {row[2]}")
        if counts["users"] == 0:
            problems.append("no users in the restored copy")
        if problems:
            raise RuntimeError("; ".join(problems))
        return {"mode": "full", "file": path.name, "schema": restored[-1] if restored else None,
                "rows": counts, "timescale": timescale}
    finally:
        with psycopg.connect(database_url, autocommit=True) as source:
            source.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


def restore_backup(database_url: str, path: Path, data_key: str | None = None,
                   pg_dump: str | None = None) -> None:
    """Replace the contents of ``database_url`` with the backup (pg_restore --clean)."""
    params = conninfo_to_dict(database_url)
    env = dict(os.environ)
    if params.get("password"):
        env["PGPASSWORD"] = str(params.pop("password"))
    with plain_backup(path, data_key) as plain:
        args = [find_pg_tool("pg_restore", pg_dump), "--clean", "--if-exists", "--no-owner",
                "--single-transaction", "--exit-on-error"]
        for key, flag in (("host", "--host"), ("port", "--port"), ("user", "--username"),
                          ("dbname", "--dbname")):
            if params.get(key):
                args.append(f"{flag}={params[key]}")
        result = _run([*args, str(plain)], env=env)
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {result.stderr.strip()[:500]}")


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
        self.last_full_test: datetime | None = None

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
        delete_errors = getattr(self.store, "delete_collection_errors_before", None)
        if delete_errors is not None:
            await delete_errors(now - timedelta(days=COLLECTION_ERRORS_DAYS))
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
        await self.run_restore_test(path)
        return path

    async def _last_full_test(self) -> datetime | None:
        if self.last_full_test is None:
            for event in await self.store.events("restore_ok", 50):
                if event["detail"].get("mode") == "full":
                    self.last_full_test = event["ts"]
                    break
        return self.last_full_test

    async def run_restore_test(self, path: Path, mode: str | None = None) -> dict[str, Any] | None:
        """Check a backup (``archive``) or restore it into a scratch database (``full``)."""
        mode = mode or self.settings.restore_test
        if mode == "off":
            return None
        if mode == "full" and mode == self.settings.restore_test:
            last = await self._last_full_test()
            if last is not None and self.clock() - last < timedelta(
                    days=self.settings.restore_test_days):
                mode = "archive"
        try:
            if mode == "full":
                if not self.database_url:
                    raise RuntimeError("no database URL for the restore test")
                result = await asyncio.to_thread(restore_test, self.database_url, path,
                                                 self.settings.data_key, self.settings.pg_dump,
                                                 self.clock())
                self.last_full_test = self.clock()
            else:
                result = await asyncio.to_thread(check_backup_archive, path,
                                                 self.settings.data_key, self.settings.pg_dump)
        except Exception as exc:  # noqa: BLE001
            log.error("restore test (%s) of %s failed: %s", mode, path.name, exc)
            await self.store.add_event("restore_failed", self.node_id,
                                       {"mode": mode, "file": path.name, "error": str(exc)[:500]})
            return {"ok": False, "mode": mode, "error": str(exc)}
        log.info("restore test (%s) of %s passed", mode, path.name)
        await self.store.add_event("restore_ok", self.node_id, result)
        return {"ok": True, **result}
