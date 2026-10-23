"""Load test: hundreds of simulated devices, thousands of tags, the real hub and database.

    hub-loadtest --devices 300 --tags 20 --seconds 300 --embedded-db .load/db
    hub-loadtest --devices 400 --mix modbus=0.75,mqtt=0.15,opcua=0.10 --database-url postgresql://...

1. A configuration with the requested devices is generated under ``--out`` (default ``.load``):
   Modbus TCP devices each with their own address (one connection each, as in a plant with one
   IP per meter), MQTT devices on one broker, OPC UA devices grouped ten per server.
2. The simulators run in **separate processes** (Modbus/OPC UA on the proactor loop, the MQTT
   broker and its publishers on the selector loop), so they do not slow down what is measured.
3. This process runs the hub exactly as hub-api does (drivers, alarms, history filter, the
   PostgreSQL store) and records every second: samples received/stored, the write buffer,
   devices online, how stale the newest value of each device is, event-loop lag, CPU and memory.
   Under load it also times history queries, as the dashboard would run them.

The result is printed, and written as JSON (``--json``). The exit code is 0 when every limit
below held, 1 otherwise. Nothing here talks to real devices.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from hub.config_loader import load_config
from hub.drivers.base import DeviceState, utcnow
from hub.runtime import Hub, run
from hub.validate_config import print_report

log = logging.getLogger("hub.loadtest")
OPCUA_PER_SERVER = 10


@dataclass
class Limits:
    online_ratio: float = 0.99          # devices online at the end
    max_staleness_factor: float = 3.0   # newest value per device younger than 3 poll periods (+1 s)
    max_loop_lag_p99_ms: float = 250.0
    max_buffer_seconds: float = 5.0     # samples waiting for the database, in seconds of input
    max_query_p95_ms: float = 500.0


@dataclass
class Result:
    devices: dict[str, int]
    tags: int
    poll_ms: int
    seconds: float
    samples_received: int = 0
    samples_stored: int = 0
    samples_filtered: int = 0
    samples_dropped: int = 0
    write_errors: int = 0
    received_per_s: float = 0.0
    stored_per_s: float = 0.0
    online_end: int = 0
    online_min_after_warmup: int = 0
    max_staleness_s: float = 0.0
    loop_lag_p50_ms: float = 0.0
    loop_lag_p99_ms: float = 0.0
    loop_lag_max_ms: float = 0.0
    buffer_max: int = 0
    cpu_percent_avg: float = 0.0
    cpu_percent_max: float = 0.0
    rss_mb_max: float = 0.0
    sockets_hub: int = 0
    query_p50_ms: float = 0.0
    query_p95_ms: float = 0.0
    database_mb: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(self.checks.values())


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


def parse_mix(text: str) -> dict[str, float]:
    mix: dict[str, float] = {}
    for part in text.split(","):
        name, _, share = part.partition("=")
        if name.strip() not in ("modbus", "mqtt", "opcua"):
            raise SystemExit(f"--mix: unknown protocol {name!r} (modbus, mqtt, opcua)")
        mix[name.strip()] = float(share)
    total = sum(mix.values())
    if total <= 0:
        raise SystemExit("--mix: shares must add up to more than 0")
    return {k: v / total for k, v in mix.items()}


def split_devices(n: int, mix: dict[str, float]) -> dict[str, int]:
    counts = {k: int(n * v) for k, v in mix.items()}
    first = max(mix, key=lambda k: mix[k])
    counts[first] += n - sum(counts.values())
    return counts


def _modbus(i: int, tags: int, poll_ms: int) -> dict[str, Any]:
    # one address per device: 10.20.x.y (each becomes its own simulated server)
    host = f"10.20.{i // 250}.{i % 250 + 1}"
    device_tags: list[dict[str, Any]] = []
    for t in range(tags):
        if t % 10 == 9:
            device_tags.append({"name": f"status_{t:02d}", "data_type": "bool",
                                "register_type": "discrete_input", "address": t})
        else:
            device_tags.append({"name": f"value_{t:02d}", "data_type": "float32", "unit": "V",
                                "register_type": "input", "address": 2 * t, "min": 0,
                                "max": 400})
    return {"device_id": f"load_mb_{i:04d}", "name": f"Load meter {i}", "site": "load",
            "area": f"area_{i % 10}", "protocol": "modbus_tcp", "poll_interval_ms": poll_ms,
            "connection": {"host": host, "port": 502, "unit_id": 1, "timeout_ms": 2000,
                           "retries": 1},
            "tags": device_tags}


def _mqtt(i: int, tags: int) -> dict[str, Any]:
    device_id = f"load_mq_{i:04d}"
    return {"device_id": device_id, "name": f"Load sensor {i}", "site": "load",
            "area": f"area_{i % 10}", "protocol": "mqtt",
            "connection": {"broker_ref": "load_broker",
                           "telemetry_topic": f"load/{device_id}/telemetry",
                           "stale_after_s": 30},
            "tags": [{"name": f"value_{t:02d}", "data_type": "float64", "unit": "degC",
                      "json_path": f"$.v{t:02d}", "min": 0, "max": 100}
                     for t in range(tags)]}


def _opcua(i: int, tags: int, poll_ms: int) -> dict[str, Any]:
    group = i // OPCUA_PER_SERVER
    return {"device_id": f"load_ua_{i:04d}", "name": f"Load PLC {i}", "site": "load",
            "area": f"area_{i % 10}", "protocol": "opcua", "poll_interval_ms": poll_ms,
            "connection": {"endpoint_url": f"opc.tcp://10.30.0.{group + 1}:4840",
                           "security_policy": "None", "security_mode": "None"},
            "tags": [{"name": f"value_{t:02d}", "data_type": "float32", "unit": "bar",
                      "node_id": f"ns=2;s=Ua{i:04d}.V{t:02d}", "min": 0, "max": 10}
                     for t in range(tags)]}


def generate(out: Path, counts: dict[str, int], tags: int, poll_ms: int) -> dict[str, Path]:
    """Two source configs: the selector part (MQTT) and the proactor part (Modbus, OPC UA)."""
    dirs = {"tcp": out / "src_tcp", "mqtt": out / "src_mqtt"}
    for d in dirs.values():
        if d.exists():
            shutil.rmtree(d)
        (d / "devices").mkdir(parents=True)
    for i in range(counts.get("modbus", 0)):
        doc = _modbus(i, tags, poll_ms)
        (dirs["tcp"] / "devices" / f"{doc['device_id']}.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    for i in range(counts.get("opcua", 0)):
        doc = _opcua(i, tags, poll_ms)
        (dirs["tcp"] / "devices" / f"{doc['device_id']}.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    for i in range(counts.get("mqtt", 0)):
        doc = _mqtt(i, tags)
        (dirs["mqtt"] / "devices" / f"{doc['device_id']}.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (dirs["mqtt"] / "brokers.yaml").write_text(yaml.safe_dump({"brokers": [
        {"broker_id": "load_broker", "host": "load-broker.local", "port": 1883}]}),
        encoding="utf-8")
    return dirs


def merge_sim_configs(parts: list[Path], target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    (target / "devices").mkdir(parents=True)
    for part in parts:
        cfg = part / "config"
        for f in (cfg / "devices").glob("*.yaml"):
            shutil.copy(f, target / "devices" / f.name)
        if (cfg / "brokers.yaml").exists():
            shutil.copy(cfg / "brokers.yaml", target / "brokers.yaml")
    return target


# --------------------------------------------------------------------------------------------
# Simulator processes
# --------------------------------------------------------------------------------------------


def _sim_main(argv: list[str]) -> int:
    """``python -m simulators.loadtest --sim <config> <out> <loop> <base port>``."""
    from simulators import run as sim_run

    config, out, loop, base = Path(argv[0]), Path(argv[1]), argv[2], int(argv[3])
    logging.basicConfig(level=logging.WARNING)
    # one port range per process: Modbus base.., MQTT base-1, OPC UA base+5000..
    coro = sim_run._main(config, out, base, base - 1, base + 5000, base + 9000, base + 9500,
                         base + 9600)
    with contextlib.suppress(KeyboardInterrupt):
        if loop == "proactor" or sys.platform != "win32":
            return asyncio.run(coro)
        return run(coro)
    return 0


def start_simulator(config: Path, out: Path, loop: str, base_port: int) -> subprocess.Popen[str]:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    log_file = (out / "sim.log").open("w", encoding="utf-8")
    return subprocess.Popen(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "simulators.loadtest", "--sim", str(config), str(out), loop,
         str(base_port)], stdout=log_file, stderr=subprocess.STDOUT, text=True)


async def wait_for_sim(out: Path, proc: subprocess.Popen[str], timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while loop.time() < end:
        if proc.poll() is not None:
            raise SystemExit(f"simulator stopped: see {out / 'sim.log'}")
        text = (out / "sim.log").read_text(encoding="utf-8") if (out / "sim.log").exists() \
            else ""
        if "simulating" in text and (out / "config" / "devices").exists():
            return
        await asyncio.sleep(0.5)
    raise SystemExit(f"simulator not ready after {timeout_s:.0f} s: see {out / 'sim.log'}")


# --------------------------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return values[k]


async def _dashboard(queue: asyncio.Queue[Any], stop: asyncio.Event) -> None:
    while not stop.is_set():
        await queue.get()


async def _lag_probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        start = loop.time()
        await asyncio.sleep(0.1)
        samples.append(max(0.0, (loop.time() - start - 0.1) * 1000))


def _socket_count() -> int:
    try:
        import psutil

        return len(psutil.Process().net_connections(kind="inet"))
    except Exception:  # noqa: BLE001 - optional figure
        return 0


async def measure(config_dir: Path, database_url: str, seconds: float, warmup_s: float,
                  poll_ms: int, result: Result, limits: Limits) -> None:
    import psutil

    from hub.storage.migrate import migrate
    from hub.storage.postgres import PostgresStore

    report = load_config(config_dir, env={})
    if report.has_errors:
        print_report(report, sys.stderr)
        raise SystemExit("generated configuration has errors")
    await asyncio.to_thread(migrate, database_url)
    store = PostgresStore(database_url, max_size=10)
    await store.open()
    proc = psutil.Process()
    proc.cpu_percent(None)
    hub = Hub(report, store=store)
    lag: list[float] = []
    stop = asyncio.Event()
    cpu: list[float] = []
    rss: list[float] = []
    online_after_warmup: list[int] = []
    stale: list[float] = []
    queries: list[float] = []
    loop = asyncio.get_running_loop()
    try:
        async with hub:
            probe = asyncio.create_task(_lag_probe(lag, stop))
            # one open dashboard: a live subscriber that reads every event
            feed = asyncio.create_task(_dashboard(hub.bus.subscribe(), stop))
            started = loop.time()
            base_received = base_written = 0
            measured_from: float | None = None
            last_print = 0.0
            tags = list(hub.tags)
            while loop.time() - started < warmup_s + seconds:
                await asyncio.sleep(1.0)
                now_s = loop.time() - started
                cpu.append(proc.cpu_percent(None))
                rss.append(proc.memory_info().rss / 2**20)
                online = sum(s.state is DeviceState.ONLINE for s in hub.statuses.values())
                if now_s >= warmup_s:
                    if measured_from is None:
                        measured_from = loop.time()
                        base_received = hub.stats.samples_received
                        base_written = hub.stats.samples_written
                        lag.clear()
                        cpu.clear()
                        result.sockets_hub = _socket_count()
                    online_after_warmup.append(online)
                    result.buffer_max = max(result.buffer_max, hub.stats.buffered)
                    now = utcnow()
                    ages = [(now - s.last_seen).total_seconds() for s in hub.statuses.values()
                            if s.last_seen is not None]
                    if ages:
                        stale.append(max(ages))
                    if int(now_s) % 5 == 0:         # dashboard-like history reads under load
                        for path in random.sample(tags, min(5, len(tags))):  # nosec B311
                            t0 = time.perf_counter()
                            await store.history(path, now - timedelta(minutes=10), now,
                                                max_points=300)
                            queries.append((time.perf_counter() - t0) * 1000)
                if now_s - last_print >= 10:
                    last_print = now_s
                    print(f"  t={now_s:5.0f}s online={online}/{len(hub.statuses)} "
                          f"received={hub.stats.samples_received} "
                          f"stored={hub.stats.samples_written} buffer={hub.stats.buffered} "
                          f"cpu={cpu[-1] if cpu else 0:.0f}% rss={rss[-1]:.0f}MB "
                          f"lag_max={max(lag) if lag else 0:.0f}ms", flush=True)
            elapsed = loop.time() - (measured_from or started)
            stop.set()
            await probe
            feed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feed
            result.samples_received = hub.stats.samples_received - base_received
            result.samples_stored = hub.stats.samples_written - base_written
            result.samples_filtered = hub.stats.samples_filtered
            result.samples_dropped = hub.stats.samples_dropped
            result.write_errors = hub.stats.write_errors
            result.received_per_s = result.samples_received / elapsed
            result.stored_per_s = result.samples_stored / elapsed
            result.online_end = sum(s.state is DeviceState.ONLINE
                                    for s in hub.statuses.values())
        async with store._conn() as conn:  # noqa: SLF001 - one read-only figure
            row = await (await conn.execute(
                "SELECT pg_database_size(current_database()) AS b")).fetchone()
            result.database_mb = round(row["b"] / 2**20, 1)  # type: ignore[index]
    finally:
        await store.close()
    result.online_min_after_warmup = min(online_after_warmup, default=0)
    result.max_staleness_s = round(max(stale, default=0.0), 2)
    result.loop_lag_p50_ms = round(statistics.median(lag), 1) if lag else 0.0
    result.loop_lag_p99_ms = round(percentile(lag, 0.99), 1)
    result.loop_lag_max_ms = round(max(lag, default=0.0), 1)
    result.cpu_percent_avg = round(statistics.mean(cpu), 1) if cpu else 0.0
    result.cpu_percent_max = round(max(cpu, default=0.0), 1)
    result.rss_mb_max = round(max(rss, default=0.0), 1)
    result.query_p50_ms = round(percentile(queries, 0.5), 1)
    result.query_p95_ms = round(percentile(queries, 0.95), 1)
    total = sum(result.devices.values())
    per_s = result.received_per_s or 1
    result.checks = {
        "devices online": result.online_end >= limits.online_ratio * total,
        "no samples lost": result.samples_dropped == 0 and result.write_errors == 0,
        "values fresh": result.max_staleness_s <= limits.max_staleness_factor * poll_ms / 1000
        + 1 + (30 if result.devices.get("mqtt") and not result.devices.get("modbus") else 0),
        "event loop responsive": result.loop_lag_p99_ms <= limits.max_loop_lag_p99_ms,
        "database keeps up": result.buffer_max <= limits.max_buffer_seconds * per_s,
        "history queries fast": result.query_p95_ms <= limits.max_query_p95_ms,
    }


def render(result: Result, limits: Limits) -> str:
    total = sum(result.devices.values())
    mix = ", ".join(f"{k} {v}" for k, v in result.devices.items() if v)
    lines = [
        "",
        f"Load test: {total} devices ({mix}), {result.tags} tags, poll {result.poll_ms} ms, "
        f"{result.seconds:.0f} s measured",
        f"  throughput      {result.received_per_s:,.0f} samples/s received, "
        f"{result.stored_per_s:,.0f}/s stored; {result.samples_filtered:,} not stored "
        "(unchanged, history filter)",
        f"  losses          dropped {result.samples_dropped}, write errors {result.write_errors}",
        f"  devices online  {result.online_end}/{total} at the end, "
        f"min {result.online_min_after_warmup} during the test",
        f"  freshness       newest value at most {result.max_staleness_s:.1f} s old",
        f"  event loop lag  p50 {result.loop_lag_p50_ms} ms, p99 {result.loop_lag_p99_ms} ms, "
        f"max {result.loop_lag_max_ms} ms (limit p99 {limits.max_loop_lag_p99_ms:.0f})",
        f"  write buffer    max {result.buffer_max} samples",
        f"  history query   p50 {result.query_p50_ms} ms, p95 {result.query_p95_ms} ms",
        f"  hub process     CPU avg {result.cpu_percent_avg}% / max {result.cpu_percent_max}% "
        f"(100% = one core), memory max {result.rss_mb_max} MB, "
        f"{result.sockets_hub} sockets",
        f"  database        {result.database_mb} MB after the test",
        "",
    ]
    for name, ok in result.checks.items():
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    lines.append(f"\nresult: {'PASS' if result.ok else 'FAIL'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    counts = split_devices(args.devices, parse_mix(args.mix))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - once, before the test
    dirs = generate(out, counts, args.tags, args.poll_ms)
    print(f"generated {args.devices} devices x {args.tags} tags = {args.devices * args.tags} "
          f"tags ({counts}) in {out}", flush=True)
    sims: list[tuple[subprocess.Popen[str], Path]] = []
    try:
        parts = []
        if counts.get("modbus") or counts.get("opcua"):
            sim_out = out / "sim_tcp"
            sims.append((start_simulator(dirs["tcp"], sim_out, "proactor", args.base_port),
                         sim_out))
            parts.append(sim_out)
        if counts.get("mqtt"):
            sim_out = out / "sim_mqtt"
            sims.append((start_simulator(dirs["mqtt"], sim_out, "selector",
                                         args.base_port + 12000), sim_out))
            parts.append(sim_out)
        for proc, sim_out in sims:
            await wait_for_sim(sim_out, proc, args.sim_timeout)
        print("simulators running; starting the hub", flush=True)
        config = merge_sim_configs(parts, out / "hub_config")
        database_url = args.database_url
        if not database_url:
            from backend.server import embedded_database

            database_url = await asyncio.to_thread(embedded_database, args.embedded_db)
        result = Result(devices=counts, tags=args.devices * args.tags, poll_ms=args.poll_ms,
                        seconds=args.seconds)
        limits = Limits()
        if args.profile:
            import cProfile

            profiler = cProfile.Profile()
            profiler.enable()
        try:
            await measure(config, database_url, args.seconds, args.warmup, args.poll_ms, result,
                          limits)
        finally:
            if args.profile:
                profiler.disable()
                profiler.dump_stats(args.profile)
                print(f"profile written: {args.profile} (python -m pstats {args.profile})")
    finally:
        for proc, _ in sims:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(10)
    print(render(result, limits))
    if args.json:
        doc = asdict(result) | {"ok": result.ok, "limits": asdict(limits),
                                "platform": sys.platform, "python": sys.version.split()[0],
                                "cpu_count": os.cpu_count()}
        args.json.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(f"report written: {args.json}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--sim"]:
        return _sim_main(argv[1:])
    p = argparse.ArgumentParser(prog="hub-loadtest", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--devices", type=int, default=300)
    p.add_argument("--tags", type=int, default=20, help="tags per device")
    p.add_argument("--mix", default="modbus=0.7,mqtt=0.2,opcua=0.1")
    p.add_argument("--poll-ms", type=int, default=1000)
    p.add_argument("--seconds", type=float, default=120.0, help="measured time")
    p.add_argument("--warmup", type=float, default=20.0, help="seconds before measuring")
    p.add_argument("--out", type=Path, default=Path(".load"))
    p.add_argument("--database-url", default=os.environ.get("HUB_LOADTEST_DATABASE_URL"),
                   help="an empty test database (never production)")
    p.add_argument("--embedded-db", type=Path, default=Path(".load/db"),
                   help="throw-away PostgreSQL when no --database-url is given")
    p.add_argument("--base-port", type=int, default=31000)
    p.add_argument("--sim-timeout", type=float, default=180.0)
    p.add_argument("--json", type=Path)
    p.add_argument("--profile", type=Path, help="write a cProfile of the hub process")
    p.add_argument("--log-level", default="error")
    args = p.parse_args(argv)
    if args.devices < 1 or args.tags < 1:
        p.error("--devices and --tags must be at least 1")
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    with contextlib.suppress(KeyboardInterrupt):
        return int(run(_main(args)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
