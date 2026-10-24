"""Upgrade safety (schema newer than the code, dry run, upgrade-check) and backup restore tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from backend import admin
from hub.maintenance import (
    Maintenance,
    MaintenanceSettings,
    check_backup_archive,
    restore_backup,
    restore_test,
)
from hub.models import DEVICE_ADAPTER
from hub.storage.migrate import SchemaTooNew, discover, migrate, status
from tests.hub.builders import modbus_device

pytestmark = pytest.mark.postgres
KEY = "k" * 40
LATEST = discover()[-1].version
NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


async def seeded(store: Any) -> None:
    await store.sync_config([DEVICE_ADAPTER.validate_python(modbus_device())])
    user = await store.create_user("op.one", "Op", "h")
    await store.add_auth_audit("login_ok", "op.one", user_id=user.user_id)


# --- schema / upgrade ------------------------------------------------------------------------


def test_schema_newer_than_the_code_is_refused(database_url: str) -> None:
    state = status(database_url)
    assert state["pending"] == [] and state["unknown"] == []
    assert state["applied"][-1] == LATEST
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_migrations (version) VALUES ('0099_from_the_future')")
    assert status(database_url)["unknown"] == ["0099_from_the_future"]
    with pytest.raises(SchemaTooNew, match="0099_from_the_future"):
        migrate(database_url)


def test_migrate_dry_run_and_upgrade_check(database_url: str, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HUB_DATABASE_URL", database_url)
    assert admin.main(["migrate", "--dry-run"]) == 0
    assert "pending: none" in capsys.readouterr().out
    config = Path(__file__).resolve().parents[2] / "config"
    # a fresh database has no backup yet: not ready
    assert admin.main(["upgrade-check", "--config-dir", str(config)]) == 1
    out = capsys.readouterr().out
    assert "NOT READY" in out and "no backup recorded" in out
    assert "hub version installed" in out and "pg_restore" in out
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO system_events (kind, node_id, detail) VALUES "
                     "('backup_ok', 'n', '{\"file\": \"hub-x.dump\"}')")
    assert admin.main(["upgrade-check", "--config-dir", str(config)]) == 0
    assert "ready to upgrade" in capsys.readouterr().out
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_migrations (version) VALUES ('0099_future')")
    assert admin.main(["migrate", "--dry-run"]) == 1
    assert admin.main(["upgrade-check", "--config-dir", str(config)]) == 1
    assert "NEWER" in capsys.readouterr().out
    assert admin.main(["list-users"]) == 1        # commands that migrate refuse it too


# --- backups -----------------------------------------------------------------------------------


async def test_archive_check(store: Any, database_url: str, tmp_path: Path) -> None:
    await seeded(store)
    m = Maintenance(store, MaintenanceSettings(backup_dir=tmp_path, data_key=KEY),
                    database_url=database_url, clock=lambda: NOW)
    path = await m.run_backup()           # runs the archive check afterwards
    assert path is not None and path.name.endswith(".enc")
    events = await store.events("restore_ok", 5)
    assert events and events[0]["detail"]["mode"] == "archive"
    assert (await asyncio.to_thread(check_backup_archive, path, KEY))["tables"] >= 6
    with pytest.raises(Exception, match="HUB_DATA_KEY|tag"):
        await asyncio.to_thread(check_backup_archive, path, "x" * 40)   # wrong key
    with pytest.raises(RuntimeError, match="encrypted"):
        await asyncio.to_thread(check_backup_archive, path, None)
    broken = tmp_path / "hub-broken.dump"
    broken.write_bytes(b"PGDMP" + bytes(100))
    with pytest.raises(RuntimeError, match="pg_restore"):
        await asyncio.to_thread(check_backup_archive, broken, None)
    result = await m.run_restore_test(broken, "archive")
    assert result is not None and result["ok"] is False
    assert (await store.events("restore_failed", 1))[0]["detail"]["file"] == "hub-broken.dump"


async def test_full_restore_test_and_restore(store: Any, database_url: str,
                                            tmp_path: Path) -> None:
    await seeded(store)
    m = Maintenance(store, MaintenanceSettings(backup_dir=tmp_path, restore_test="full",
                                               restore_test_days=7),
                    database_url=database_url, clock=lambda: NOW)
    path = await m.run_backup()
    assert path is not None
    full = [e for e in await store.events("restore_ok", 5) if e["detail"]["mode"] == "full"]
    assert full and full[0]["detail"]["rows"]["users"] == 1
    assert full[0]["detail"]["schema"] == LATEST
    # the temporary database is gone again
    with psycopg.connect(database_url, autocommit=True) as conn:
        left = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE "
                            "'hub_restore_check_%'").fetchall()
    assert left == []
    # within restore_test_days the next backup gets only the archive check
    second = await m.run_restore_test(path)
    assert second is not None and second["mode"] == "archive"

    # a restore test catches a tampered audit chain inside the backup
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE auth_audit DISABLE TRIGGER USER")
        conn.execute("UPDATE auth_audit SET username = 'someone' WHERE event = 'login_ok'")
        conn.execute("ALTER TABLE auth_audit ENABLE TRIGGER USER")
    tampered = await m.run_backup()
    assert tampered is not None
    with pytest.raises(RuntimeError, match="chain broken"):
        await asyncio.to_thread(restore_test, database_url, tampered)

    # restore-backup puts the old contents back
    await store.create_user("late.user", "Late", "h")
    await asyncio.to_thread(restore_backup, database_url, path)
    assert await store.get_user_by_username("late.user") is None
    assert await store.get_user_by_username("op.one") is not None


def test_restore_backup_needs_confirmation(monkeypatch: pytest.MonkeyPatch, database_url: str,
                                          tmp_path: Path) -> None:
    monkeypatch.setenv("HUB_DATABASE_URL", database_url)
    assert admin.main(["restore-backup", str(tmp_path / "x.dump")]) == 2


async def test_hub_api_refuses_a_newer_database(database_url: str, tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    from backend.server import serve
    from backend.settings import Settings

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_migrations (version) VALUES ('0099_from_the_future')")
    config = Path(__file__).resolve().parents[2] / "config"
    settings = Settings(jwt_secret="s" * 40, database_url=database_url, config_dir=config)
    assert await serve(settings) == 1
    assert "refusing to start" in capsys.readouterr().err
