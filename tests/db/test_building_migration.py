"""0008_building_model: up, down, up again; store sync of the building model and calc versions;
quality reasons and receive time in the store; maintenance records; collection errors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hub.config_loader import load_config
from hub.models import CalcDevice, Quality, QualityReason, Sample
from hub.storage.migrate import RollbackError, migrate, rollback

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.postgres
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
NEW_TABLES = ("organizations", "organization_sites", "locations", "building_systems",
              "equipment", "equipment_points", "maintenance_records", "calc_versions",
              "collection_errors")


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        return {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}


def _permissions(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        return {r[0] for r in conn.execute("SELECT permission_id FROM permissions")}


def test_up_down_up(database_url: str) -> None:
    assert set(NEW_TABLES) <= _tables(database_url)
    assert {"control.setpoint", "control.command", "calc.publish"} <= _permissions(database_url)

    rollback(database_url, "0017_switches_sms")

    rollback(database_url, "0016_home_maker_plus")

    rollback(database_url, "0015_maker_home")

    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")

    rollback(database_url, "0011_claims")              # the newer migrations first
    rollback(database_url, "0010_accounts_sharing")
    rollback(database_url, "0009_platform_core")
    rollback(database_url, "0008_building_model")
    assert not set(NEW_TABLES) & _tables(database_url)
    assert "control.setpoint" not in _permissions(database_url)
    with psycopg.connect(database_url) as conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='samples'")}
        assert "reason" not in cols
        assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version="
                            "'0008_building_model'").fetchone()[0] == 0

    assert migrate(database_url) == ["0008_building_model", "0009_platform_core",
                                     "0010_accounts_sharing", "0011_claims",
                                     "0012_commands_ingest",
                                     "0013_automations",
                                     "0014_versions_firmware",
                                     "0015_maker_home",
                                     "0016_home_maker_plus",
                                     "0017_switches_sms"]
    assert set(NEW_TABLES) <= _tables(database_url)


def test_rollback_rules(database_url: str) -> None:
    with pytest.raises(RollbackError, match="no rollback script"):
        rollback(database_url, "0007_directory_users")
    rollback(database_url, "0017_switches_sms")
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    rollback(database_url, "0011_claims")
    rollback(database_url, "0010_accounts_sharing")
    rollback(database_url, "0009_platform_core")
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO devices (device_id, name, site, area, protocol) "
                     "VALUES ('calc_x', 'C', 'kish', 'energy', 'calc')")
    with pytest.raises(psycopg.Error, match="calculated devices exist"):
        rollback(database_url, "0008_building_model")
    assert "calc_versions" in _tables(database_url)          # nothing changed


async def test_store_syncs_building_model_and_calc_versions(store: Any) -> None:
    report = load_config(REPO_CONFIG, env={})
    assert not report.has_errors
    devices = [d.device for d in report.devices]
    await store.sync_config(devices)
    await store.sync_building(report.building)
    await store.sync_building(report.building)               # idempotent
    async with store._conn() as conn:
        n_eq = (await (await conn.execute("SELECT count(*) AS n FROM equipment WHERE "
                                          "retired_at IS NULL")).fetchone())["n"]
        n_pts = (await (await conn.execute("SELECT count(*) AS n FROM equipment_points"
                                           )).fetchone())["n"]
        site = (await (await conn.execute("SELECT org_id FROM organization_sites WHERE "
                                          "site='kish'")).fetchone())["org_id"]
    assert n_eq == len(report.building.equipment)
    assert n_pts == sum(len(e.points) for e in report.building.equipment)
    assert site == "gate_kish"

    # an equipment removed from the YAML is retired, not deleted
    trimmed = report.building.model_copy(update={"equipment": report.building.equipment[1:]})
    await store.sync_building(trimmed)
    async with store._conn() as conn:
        row = await (await conn.execute(
            "SELECT retired_at FROM equipment WHERE equipment_id=%s",
            (report.building.equipment[0].id,))).fetchone()
    assert row["retired_at"] is not None

    # calc versions: one row per definition in force; unchanged definitions add nothing
    path = "kish.energy.calc_kish.site_power"
    assert len(await store.calc_versions(path)) == 1
    await store.sync_config(devices)
    assert len(await store.calc_versions(path)) == 1
    calc = next(d for d in devices if isinstance(d, CalcDevice))
    tags = [t.model_copy(update={"formula": "grid + solar + 0"}) if t.name == "site_power" else t
            for t in calc.tags]
    await store.sync_config([d if d is not calc else calc.model_copy(update={"tags": tags})
                             for d in devices])
    versions = await store.calc_versions(path)
    assert len(versions) == 2 and versions[0]["definition"]["formula"] == "grid + solar + 0"


async def test_reason_and_receive_time_are_stored(store: Any) -> None:
    report = load_config(REPO_CONFIG, env={})
    await store.sync_config([d.device for d in report.devices])
    t0 = datetime(2026, 10, 1, 8, tzinfo=UTC)
    path = "kish.substation.meter_01.active_power"
    await store.write_samples([
        Sample(tag=path, value=None, timestamp=t0, quality=Quality.BAD,
               reason=QualityReason.COMM_FAILURE, received_at=t0 + timedelta(seconds=2))])
    (latest,) = await store.latest([path])
    assert latest.quality is Quality.BAD and latest.reason is QualityReason.COMM_FAILURE
    assert latest.received_at == t0 + timedelta(seconds=2) and latest.ts == t0
    async with store._conn() as conn:
        row = await (await conn.execute("SELECT reason FROM samples")).fetchone()
    assert row["reason"] == 1


async def test_maintenance_records_are_append_only(store: Any) -> None:
    report = load_config(REPO_CONFIG, env={})
    await store.sync_config([d.device for d in report.devices])
    await store.sync_building(report.building)
    async with store._conn() as conn:
        user = (await (await conn.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES "
            "('tech.one', 'Tech', 'x') RETURNING user_id")).fetchone())["user_id"]
    rec = await store.add_maintenance_record(
        equipment_id="transfer_pump_01", performed_at=datetime(2026, 9, 1, tzinfo=UTC),
        kind="preventive", description="Bearing greased", performed_by="Contractor A",
        work_order="WO-17", created_by=user)
    assert rec.record_id > 0
    (listed,) = await store.maintenance_records("transfer_pump_01")
    assert listed.created_by_name == "tech.one" and listed.work_order == "WO-17"
    with pytest.raises(psycopg.Error, match="append-only"):
        async with store._conn() as conn:
            await conn.execute("UPDATE maintenance_records SET description='x'")
    assert await store.equipment_known("transfer_pump_01")
    assert not await store.equipment_known("nope")


async def test_collection_errors(store: Any) -> None:
    report = load_config(REPO_CONFIG, env={})
    await store.sync_config([d.device for d in report.devices])
    t0 = datetime(2026, 10, 1, 8, tzinfo=UTC)
    await store.add_collection_errors([
        (t0, "meter_01", "offline", "TimeoutError: no answer"),
        (t0 + timedelta(minutes=1), "ghost", "error", "unknown device is skipped")])
    errors = await store.collection_errors("meter_01")
    assert [e.message for e in errors] == ["TimeoutError: no answer"]
    assert await store.delete_collection_errors_before(t0 + timedelta(days=1)) == 1
