"""The hub's own health checks raise and clear system alarms."""

from __future__ import annotations

import asyncio
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.health_watch import HealthWatch, sntp_offset, volume_of
from simulators.mqtt_sim import free_port
from tests.api.conftest import Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


async def watch(backend: Backend, **settings: Any) -> HealthWatch:
    svc = backend.services
    svc.settings = svc.settings.model_copy(update=settings)
    w = HealthWatch(svc)
    w._dropped_seen = svc.hub.stats.samples_dropped  # noqa: SLF001
    return w


async def open_sources(backend: Backend) -> set[str]:
    return set(await backend.store.open_alarm_sources())


def by_name(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["check"]: r for r in results}


async def test_all_ok_on_a_healthy_hub(backend: Backend) -> None:
    w = await watch(backend)
    results = by_name(await w.run_once())
    assert results["database"]["status"] == "ok"
    assert results["storage"]["status"] == "ok" and results["loop"]["status"] == "ok"
    assert results["clock"]["status"] == "ok"
    assert abs(results["clock"]["details"]["database_skew_s"]) < 5
    assert results["backup"]["status"] == "unknown"           # HUB_BACKUP_DIR not set
    assert any(name.startswith("disk:") and r["status"] == "ok" for name, r in results.items())
    assert not {s for s in await open_sources(backend) if s.startswith("system:")}


async def test_disk_alarm_raises_and_clears(backend: Backend) -> None:
    w = await watch(backend, disk_warn_percent=99.99, disk_critical_percent=99.98)
    results = by_name(await w.run_once())
    disk = next(r for n, r in results.items() if n.startswith("disk:"))
    assert disk["status"] == "critical"
    sources = await open_sources(backend)
    assert any(s.startswith("system:disk:") for s in sources)
    backend.services.settings = backend.services.settings.model_copy(
        update={"disk_warn_percent": 0.02, "disk_critical_percent": 0.01})
    await w.run_once()
    assert not any(s.startswith("system:disk:") for s in await open_sources(backend))


async def test_storage_loss_backlog_and_loop(backend: Backend) -> None:
    w = await watch(backend)
    hub = backend.services.hub
    hub.stats.samples_dropped += 25
    results = by_name(await w.run_once())
    assert results["storage"]["status"] == "critical"
    assert "25 samples" in results["storage"]["message"]
    alarm = (await backend.store.open_alarm_sources())["system:storage"]
    assert alarm.severity == "critical"
    results = by_name(await w.run_once())                     # no new loss, buffer empty: clear
    assert results["storage"]["status"] == "ok"
    assert "system:storage" not in await open_sources(backend)
    w._loop_lag = 3.5  # noqa: SLF001
    results = by_name(await w.run_once())
    assert results["loop"]["status"] == "warning" and "3.5 s" in results["loop"]["message"]
    assert "system:loop" in await open_sources(backend)


async def test_backup_and_restore_failures(backend: Backend, tmp_path: Path) -> None:
    w = await watch(backend, backup_dir=tmp_path, backup_max_age_hours=1)
    store = backend.store
    await store.add_event("backup_ok", "n", {"file": "hub-1.dump"})
    assert by_name(await w.run_once())["backup"]["status"] == "ok"
    await store.add_event("backup_failed", "n", {"error": "pg_dump: disk full"})
    results = by_name(await w.run_once())
    assert results["backup"]["status"] == "warning" and "disk full" in results["backup"]["message"]
    # too old: an hour and a half after the last good backup
    later = datetime.now(UTC) + timedelta(hours=1.5)
    await store.add_event("backup_ok", "n", {"file": "hub-2.dump"})
    results = by_name(await w.run_once(later))
    assert "no successful backup since" in results["backup"]["message"]
    await store.add_event("restore_failed", "n", {"mode": "archive", "file": "hub-2.dump",
                                                  "error": "pg_restore: invalid archive"})
    results = by_name(await w.run_once())
    assert results["restore"]["status"] == "critical"
    alarm = (await store.open_alarm_sources())["system:restore"]
    assert alarm.severity == "critical" and "invalid archive" in alarm.message
    await store.add_event("restore_ok", "n", {"mode": "archive", "file": "hub-3.dump"})
    await w.run_once()
    assert "system:restore" not in await open_sources(backend)


async def test_clock_skew(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await watch(backend, clock_max_skew_s=30)

    async def skewed() -> datetime:
        return datetime.now(UTC) + timedelta(seconds=120)    # database 2 min ahead

    monkeypatch.setattr(backend.store, "server_time", skewed)
    results = by_name(await w.run_once())
    assert results["clock"]["status"] == "warning"
    assert results["clock"]["details"]["database_skew_s"] == pytest.approx(-120, abs=2)
    assert "system:clock" in await open_sources(backend)


async def test_database_outage_is_reported_and_recorded(backend: Backend,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    w = await watch(backend)
    sent: list[dict[str, Any]] = []

    async def listener(event: dict[str, Any]) -> None:
        sent.append(event)

    backend.services.hub.alarm_listeners.append(listener)
    real_ping = backend.store.ping

    async def down() -> bool:
        raise OSError("connection refused")

    monkeypatch.setattr(backend.store, "ping", down)
    for _ in range(3):
        results = by_name(await w.run_once())
        assert results["database"]["status"] == "critical"
        assert results["backup"]["status"] == "unknown"
    raised = [e for e in sent if e.get("source") == "system:database" and e["action"] == "raised"]
    assert len(raised) == 1 and raised[0]["severity"] == "critical"     # notified once
    monkeypatch.setattr(backend.store, "ping", real_ping)
    await w.run_once()
    cleared = [e for e in sent if e.get("source") == "system:database" and e["action"] == "cleared"]
    assert cleared
    history = [a for a in await backend.store.alarm_history(None, 50)
               if a.source == "system:database"]
    assert history and history[0].cleared_at is not None


async def test_api_and_metrics(backend: Backend) -> None:
    await backend.user("eng.one", ("engineer", None, None))
    headers = await backend.headers("eng.one")
    r = await backend.client.get("/api/system/health-checks", params={"run": "true"},
                                 headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["last_run"] and {c["check"] for c in body["checks"]} >= {"database", "clock"}
    token = "m" * 32
    backend.services.settings = backend.services.settings.model_copy(
        update={"metrics_token": token})
    m = await backend.client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert m.status_code == 200
    assert 'hub_health_check{check="database"} 0' in m.text
    assert "hub_disk_free_percent" in m.text


async def test_sntp_offset() -> None:
    """A fake NTP server 5 s ahead: the local clock is reported 5 s behind."""
    port = free_port()
    loop = asyncio.get_running_loop()

    class Server(asyncio.DatagramProtocol):
        def connection_made(self, transport: Any) -> None:
            self.transport = transport

        def datagram_received(self, data: bytes, addr: Any) -> None:
            now = time.time() + 5 + 2_208_988_800
            stamp = struct.pack("!II", int(now), int((now % 1) * 2**32))
            self.transport.sendto(b"\x24" + bytes(31) + stamp + stamp, addr)

    transport, _ = await loop.create_datagram_endpoint(Server, local_addr=("127.0.0.1", port))
    try:
        offset = await sntp_offset(f"127.0.0.1:{port}")
        assert offset == pytest.approx(-5, abs=0.5)
    finally:
        transport.close()
    with pytest.raises((TimeoutError, OSError)):
        await sntp_offset(f"127.0.0.1:{free_port()}", timeout=0.5)


def test_volume_of(tmp_path: Path) -> None:
    assert volume_of(tmp_path / "not" / "there")
