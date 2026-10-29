"""0012_commands_ingest: up/down/up, rollback refused with native devices, and command
statuses that only move forward (a late or foreign ack changes nothing)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from hub.config_loader import load_config
from hub.storage.migrate import migrate, rollback
from tests.db.test_building_migration import REPO_CONFIG

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.postgres


def test_up_down_up(database_url: str) -> None:
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('commands')").fetchone()[0] is None
    assert migrate(database_url) == ["0012_commands_ingest",
                                     "0013_automations",
                                     "0014_versions_firmware",
                                     "0015_maker_home",
                                     "0016_home_maker_plus"]
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO devices (device_id, name, site, area, protocol) "
                     "VALUES ('nat_1', 'N', 'kish', 'a', 'native')")
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    with pytest.raises(psycopg.Error, match="native devices exist"):
        rollback(database_url, "0012_commands_ingest")


async def test_status_only_moves_forward(store: Any) -> None:
    report = load_config(REPO_CONFIG, env={})
    await store.sync_config([d.device for d in report.devices])
    path = next(p for p in await store.tags() if p.startswith("kish."))
    device_id = path.split(".")[2]
    now = datetime.now(UTC)
    cid = uuid.uuid4()
    await store.commands.create(command_id=cid, device_id=device_id, tag_path=path, value=1,
                                created_by=None, expires_at=now + timedelta(minutes=1))
    c = store.commands
    assert (await c.advance(cid, "executed", now)).status == "executed"
    rec = await c.get(cid)
    assert rec.sent_at == rec.delivered_at == rec.executed_at        # skipped steps stamped
    assert await c.advance(cid, "delivered", now) is None             # never backwards
    assert await c.advance(cid, "confirmed", now, device_id="other") is None  # not its own
    assert (await c.advance(cid, "confirmed", now)).finished_at is not None
    assert await c.advance(cid, "failed", now) is None                # ended stays ended

    late = uuid.uuid4()
    await store.commands.create(command_id=late, device_id=device_id, tag_path=path, value=2,
                                created_by=None, expires_at=now - timedelta(seconds=1))
    assert await c.expire(now) == 1
    assert (await c.get(late)).status == "expired"
