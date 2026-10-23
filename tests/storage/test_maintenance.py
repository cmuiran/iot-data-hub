from __future__ import annotations

import subprocess
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from hub.maintenance import (
    Maintenance,
    MaintenanceSettings,
    backup_database,
    find_pg_dump,
    next_backup_at,
    prune_backups,
)
from hub.models import DEVICE_ADAPTER, Quality, Sample
from tests.hub.builders import modbus_device

pytestmark = pytest.mark.postgres
V = "kish.substation.meter_01.voltage"
NOW = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)


async def _seed(store: Any) -> None:
    await store.sync_config([DEVICE_ADAPTER.validate_python(modbus_device())])
    samples = []
    # 3 days ago, one sample per 10 minutes for 2 hours; and the current hour
    old = datetime(2026, 10, 7, 8, tzinfo=UTC)
    for i in range(12):
        samples.append(Sample(tag=V, value=float(i), timestamp=old + timedelta(minutes=10 * i)))
    samples.append(Sample(tag=V, value=None, timestamp=old + timedelta(minutes=5),
                          quality=Quality.BAD))
    samples.append(Sample(tag=V, value=500.0, timestamp=NOW - timedelta(minutes=5)))
    await store.write_samples(samples)


async def test_rollup_and_retention(store: Any) -> None:
    await _seed(store)
    m = Maintenance(store, MaintenanceSettings(retention_raw_days=1, timezone="UTC"),
                    database_url=None, clock=lambda: NOW)
    assert await m.run_rollup() > 0
    hist = await store.history(V, datetime(2026, 10, 7, tzinfo=UTC),
                               datetime(2026, 10, 8, tzinfo=UTC), max_points=24)
    by_hour = {p.ts.hour: p for p in hist}
    assert by_hour[8].avg == pytest.approx(2.5) and by_hour[8].count == 6   # bad sample excluded
    assert by_hour[9].min == 6 and by_hour[9].max == 11

    deleted = await m.run_retention()
    assert deleted["raw"] == 13            # everything older than 1 day (12 good + 1 bad)
    # long-range history still works from the roll-up after raw data is gone
    again = await store.history(V, datetime(2026, 10, 7, tzinfo=UTC), NOW, max_points=50)
    assert sum(p.count for p in again) == 13   # 12 rolled-up + the recent raw sample
    assert [e["kind"] for e in await store.events("retention", 10)] == ["retention"]


async def test_retention_never_deletes_unrolled_data(store: Any) -> None:
    await _seed(store)
    m = Maintenance(store, MaintenanceSettings(retention_raw_days=1, timezone="UTC"),
                    database_url=None, clock=lambda: NOW)
    assert (await m.run_retention())["raw"] == 0      # no roll-up yet: keep everything
    assert await store.oldest_sample() is not None


async def test_rollup_is_idempotent(store: Any) -> None:
    await _seed(store)
    m = Maintenance(store, MaintenanceSettings(timezone="UTC"), database_url=None,
                    clock=lambda: NOW)
    await m.run_rollup()
    await m.run_rollup()
    hist = await store.history(V, datetime(2026, 10, 7, tzinfo=UTC),
                               datetime(2026, 10, 8, tzinfo=UTC), max_points=24)
    assert sum(p.count for p in hist) == 12


async def test_backup_with_pg_dump(store: Any, database_url: str, tmp_path: Path) -> None:
    await _seed(store)
    m = Maintenance(store, MaintenanceSettings(backup_dir=tmp_path, backup_keep=2),
                    database_url=database_url, clock=lambda: NOW)
    path = await m.run_backup()
    assert path is not None and path.stat().st_size > 1000
    restore = str(Path(find_pg_dump()).with_name(Path(find_pg_dump()).name.replace(
        "pg_dump", "pg_restore")))
    listing = subprocess.run([restore, "--list", str(path)], capture_output=True, text=True,
                             check=True).stdout
    assert "TABLE DATA public samples" in listing and "write_audit" in listing
    # every backup is read back right away (HUB_RESTORE_TEST=archive, the default)
    assert [e["kind"] for e in await store.events(None, 5)] == ["restore_ok", "backup_ok"]


async def test_backup_failure_is_recorded(store: Any, tmp_path: Path) -> None:
    m = Maintenance(store, MaintenanceSettings(backup_dir=tmp_path),
                    database_url="postgresql://nobody:x@127.0.0.1:1/nope", clock=lambda: NOW)
    assert await m.run_backup() is None
    events = await store.events("backup_failed", 5)
    assert events and "pg_dump failed" in events[0]["detail"]["error"]


def test_backup_password_not_on_command_line(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_run(args: list[str], env: dict[str, str], **_: Any) -> Any:
        seen.update(args=args, env=env)
        (tmp_path / [a for a in args if a.startswith("--file=")][0][7:]).touch()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("hub.maintenance.subprocess.run", fake_run)
    monkeypatch.setattr("hub.maintenance.find_pg_dump", lambda explicit=None: "pg_dump")
    backup_database("postgresql://hub:s3cret@db:5432/hub", tmp_path, now=NOW)
    assert not any("s3cret" in a for a in seen["args"])
    assert seen["env"]["PGPASSWORD"] == "s3cret"
    assert "--host=db" in seen["args"] and "--dbname=hub" in seen["args"]


def test_prune_and_schedule(tmp_path: Path) -> None:
    for day in range(1, 5):
        (tmp_path / f"hub-2026100{day}-023000.dump").touch()
    removed = prune_backups(tmp_path, keep=2)
    assert sorted(p.name for p in removed) == ["hub-20261001-023000.dump",
                                               "hub-20261002-023000.dump"]
    # 02:30 Tehran (UTC+3:30) = 23:00 UTC the day before
    at = next_backup_at(datetime(2026, 10, 10, 12, tzinfo=UTC), time(2, 30), "Asia/Tehran")
    assert at == datetime(2026, 10, 10, 23, 0, tzinfo=UTC)


def test_hub_admin_backup_command(database_url: str, tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    from backend import admin

    monkeypatch.setenv("HUB_DATABASE_URL", database_url)
    assert admin.main(["backup", "--out", str(tmp_path), "--keep", "3"]) == 0
    assert "backup written" in capsys.readouterr().out
    assert len(list(tmp_path.glob("hub-*.dump"))) == 1



async def test_rollup_uses_local_hours(store: Any) -> None:
    """Tehran is UTC+3:30: its hours start at :30 UTC, so a local day splits cleanly."""
    await store.sync_config([DEVICE_ADAPTER.validate_python(modbus_device())])
    t0 = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)          # 23:30 Tehran
    await store.write_samples([Sample(tag=V, value=float(i), timestamp=t0 + timedelta(
        minutes=15 * i)) for i in range(4)])               # 23:30, 23:45, 00:00, 00:15 local
    m = Maintenance(store, MaintenanceSettings(timezone="Asia/Tehran"), database_url=None,
                    clock=lambda: NOW)
    await m.run_rollup()
    rows = await store.hourly_series(V, t0 - timedelta(hours=1), t0 + timedelta(hours=2),
                                     "Asia/Tehran")
    assert [(r["bucket"].astimezone(UTC).strftime("%H:%M"), r["n"]) for r in rows] == [
        ("19:30", 2), ("20:30", 2)]
