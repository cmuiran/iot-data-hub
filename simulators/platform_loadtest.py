"""Load test of the platform (phase 8): registration of many devices, then many native devices
sending telemetry over HTTP at the same time to a real hub-api with PostgreSQL.

    python -m simulators.platform_loadtest --register 10000 --devices 500 --period 1 --seconds 60

A throw-away PostgreSQL (pgserver) is started unless --database-url is given. The simulated
devices run in this same process (so the numbers are conservative: client and server share
one CPU core of the event loop). Reported: registration time, requests per second, latency
percentiles, errors, samples the hub received and stored, buffer drops, memory and CPU.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import csv
import io
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from backend.app import create_app
from backend.rbac import Principal
from backend.security import hash_password
from backend.services import Services
from backend.settings import Settings
from hub.config_loader import load_config
from hub.platform.devices import ProductIn
from hub.platform.template import ProductTemplate
from hub.runtime import Hub, run
from hub.storage.migrate import migrate
from hub.storage.postgres import PostgresStore


def template() -> dict[str, Any]:
    tags = [{"key": f"relay_{i}", "kind": "digital_output", "display_name": {"en": f"R{i}"},
             "data_type": "bool", "access": "read_write"} for i in range(1, 5)]
    tags += [{"key": k, "kind": "analog_input", "display_name": {"en": k},
              "data_type": "float32", "unit": u} for k, u in (("power", "kW"),
                                                              ("voltage", "V"),
                                                              ("temperature", "°C"))]
    return {"tags": tags, "bindings": {"native": {"connection": {"stale_after_s": 120},
                                                  "tags": {t["key"]: {} for t in tags}}}}


async def main(args: argparse.Namespace) -> dict[str, Any]:
    import psutil

    proc = psutil.Process()
    data = Path(tempfile.mkdtemp(prefix="hub-load-"))
    server = None
    url = args.database_url
    if url is None:
        import pgserver

        server = pgserver.get_server(str(data / "db"), cleanup_mode="delete")
        url = server.get_uri()
    migrate(url)
    (data / "config" / "devices").mkdir(parents=True)
    report = load_config(data / "config", env={})
    settings = Settings.model_validate({
        "jwt_secret": "l" * 40, "control_enabled": True, "config_dir": data / "config",
        "frontend_dir": None, "app_dir": None, "spool_dir": None,
        "rate_limit_per_min": 100_000})
    store = PostgresStore(url, max_size=10)
    await store.open()
    hub = Hub(report, store=store, flush_interval_s=0.5)
    svc = Services.build(settings, report, store, hub)
    await svc.start()
    result: dict[str, Any] = {"devices_registered": args.register,
                              "devices_sending": args.devices, "period_s": args.period,
                              "seconds": args.seconds}
    try:
        p = store.platform
        await p.create_organization("maker", "Maker", "manufacturer")
        product = await p.create_product({"maker"}, "maker", ProductIn(
            code="LOAD-1", name="Load", identifier_pattern="LD-{000000}"), None)
        await p.save_draft({"maker"}, product.product_id,
                           ProductTemplate.model_validate(template()), None, None)
        await p.publish({"maker"}, product.product_id, 1, None)
        t0 = time.perf_counter()
        done = 0
        while done < args.register:                  # batches of at most 10 000
            n = min(10_000, args.register - done)
            entries = [(f"LD-{i:06d}", None) for i in range(done + 1, done + n + 1)]
            await p.register_devices({"maker"}, product.product_id, 1, protocol="native",
                                     entries=entries, batch=None, warranty_months=None,
                                     connection={}, options={}, enabled=False, user_id=None)
            done += n
        result["register_s"] = round(time.perf_counter() - t0, 2)

        admin = await store.create_user("load.admin", "Load", hash_password("Load-Pass-2026"),
                                        must_change_password=False)
        await store.grant_role(admin.user_id, "admin", None, None, None)
        principal = Principal(user=admin, session_id=None,
                              grants=tuple(await store.grants(admin.user_id)),
                              role_permissions=await store.role_permissions())
        t0 = time.perf_counter()
        rows = list(csv.DictReader(io.StringIO(await svc.claims.labels(
            principal, product.product_id, device_ids=None, batch=None, credentials=True,
            client_addr=None))))
        result["labels_s"] = round(time.perf_counter() - t0, 2)
        senders = rows[:args.devices]
        t0 = time.perf_counter()
        for r in senders:
            _, model = await p.update_device(None, r["device_id"], enabled=True)
            await hub.apply_platform_device(r["device_id"], model)
        result["enable_s"] = round(time.perf_counter() - t0, 2)

        web = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=args.port,
                                            log_level="error"))
        web_task = asyncio.create_task(web.serve())
        while not web.started:
            await asyncio.sleep(0.05)
        cpu0 = proc.cpu_times()
        wall0 = time.perf_counter()
        groups = [senders[i::args.workers] for i in range(args.workers)]
        loop = asyncio.get_running_loop()
        with concurrent.futures.ProcessPoolExecutor(args.workers) as pool:
            parts = await asyncio.gather(*(loop.run_in_executor(
                pool, _worker, f"http://127.0.0.1:{args.port}", g, args.period, args.seconds,
                max(1, args.connections // args.workers)) for g in groups if g))
        latencies = [x for part in parts for x in part[0]]
        errors: dict[str, int] = {}
        for _, errs in parts:
            for k, v in errs.items():
                errors[k] = errors.get(k, 0) + v
        wall = time.perf_counter() - wall0
        cpu1 = proc.cpu_times()            # the hub process only (devices elsewhere)
        await asyncio.sleep(2)                       # let the writer flush
        await hub.flush()
        async with store._conn() as conn:
            stored = (await (await conn.execute(
                "SELECT count(*) AS n FROM samples s JOIN tags t USING (tag_id) JOIN devices d "
                "USING (device_id) WHERE d.source='platform'")).fetchone())["n"]
        lat = sorted(latencies)

        def pct(q: float) -> float:
            return round(lat[min(len(lat) - 1, int(q * len(lat)))] * 1000, 1) if lat else 0.0

        result |= {
            "requests_ok": len(lat), "errors": errors,
            "requests_per_s": round(len(lat) / wall, 1),
            "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99),
                           "mean": round(statistics.mean(lat) * 1000, 1) if lat else 0.0},
            "samples_received": hub.stats.samples_received, "samples_stored": stored,
            "samples_written": hub.stats.samples_written,
            "samples_filtered": hub.stats.samples_filtered,
            "samples_dropped": hub.stats.samples_dropped,
            "memory_mb": round(proc.memory_info().rss / 2**20, 1),
            "cpu_percent_of_one_core": round(100 * ((cpu1.user + cpu1.system)
                                                    - (cpu0.user + cpu0.system)) / wall, 1),
        }
        web.should_exit = True
        await web_task
    finally:
        await svc.stop()
        await store.close()
        if server is not None:
            server.cleanup()
    return result


def _worker(base_url: str, rows: list[dict[str, str]], period: float, seconds: float,
            connections: int) -> tuple[list[float], dict[str, int]]:
    """Simulated devices in a process of their own, so they do not slow the hub down."""
    return asyncio.run(_devices(base_url, rows, period, seconds, connections))


async def _devices(base_url: str, rows: list[dict[str, str]], period: float, seconds: float,
                   connections: int) -> tuple[list[float], dict[str, int]]:
    latencies: list[float] = []
    errors: dict[str, int] = {}
    stop_at = time.perf_counter() + seconds
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    async with httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits) as client:
        async def device(row: dict[str, str], k: int) -> None:
            headers = {"Authorization": f"Bearer {row['device_secret']}"}
            url = f"/ingest/v1/devices/{row['device_id']}/telemetry"
            await asyncio.sleep(period * k / max(1, len(rows)))      # spread out
            i = 0
            while time.perf_counter() < stop_at:
                i += 1
                body = {"values": {"power": 1 + (i % 50) / 10, "voltage": 230 + i % 7,
                                   "temperature": 30 + i % 10, "relay_1": i % 2 == 0}}
                t = time.perf_counter()
                try:
                    r = await client.post(url, headers=headers, json=body)
                    if r.status_code == 200:
                        latencies.append(time.perf_counter() - t)
                    else:
                        errors[str(r.status_code)] = errors.get(str(r.status_code), 0) + 1
                except httpx.HTTPError as exc:
                    errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                await asyncio.sleep(max(0.0, period - (time.perf_counter() - t)))

        await asyncio.gather(*(device(r, k) for k, r in enumerate(rows)))
    return latencies, errors


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulators.platform_loadtest")
    parser.add_argument("--register", type=int, default=10_000)
    parser.add_argument("--devices", type=int, default=500)
    parser.add_argument("--period", type=float, default=1.0, help="seconds between messages")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--connections", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4, help="processes for the devices")
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--database-url")
    args = parser.parse_args(argv)
    print(json.dumps(run(main(args)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
