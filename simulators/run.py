"""Simulate every device of a config directory on this PC.

    hub-sim --config-dir config --out .sim

Writes ``.sim/config``: a copy of the configuration pointing at local simulators (Modbus TCP
servers on 127.0.0.1:15020+, an embedded MQTT broker, OPC UA servers on 127.0.0.1:14840+;
security and credentials removed), then runs the simulators until Ctrl+C. Start the hub
against the copy in another terminal, e.g.::

    hub-api --config-dir .sim/config --dev --embedded-db .sim/db

The original config is never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from hub.config_loader import ALARMS_FILE, CONTROL_FILE, load_config
from hub.models import AlarmRule, ModbusDevice, ModbusRtuDevice, MqttDevice, OpcUaDevice
from hub.runtime import run
from hub.validate_config import print_report
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from simulators.mqtt_sim import EmbeddedBroker, SimulatedMqttDevice
from simulators.opcua_sim import SimulatedOpcUaServer

log = logging.getLogger("hub.sim")


def _dump(device: Any) -> dict[str, Any]:
    return device.model_dump(mode="json", exclude_none=True)


def _normal(device: Any, rules: dict[str, AlarmRule]) -> Any:
    """Copy of a device whose tags carry a *normal* min/max derived from its alarm limits, so
    the simulation starts without alarms. Only the simulators see this copy."""
    tags = []
    for tag in device.tags:
        rule = rules.get(device.tag_path(tag.name))
        if rule is None or rule.equals is not None or tag.data_type in ("bool", "string"):
            tags.append(tag)
            continue
        lo = rule.lo if rule.lo is not None else rule.lolo
        hi = rule.hi if rule.hi is not None else rule.hihi
        if lo is None and hi is not None:
            lo = hi * 0.5 if hi > 0 else hi * 1.5
        if hi is None and lo is not None:
            hi = lo * 2 if lo > 0 else lo * 0.5
        span = (hi - lo) * 0.2  # type: ignore[operator]
        tags.append(tag.model_copy(update={"min": lo + span, "max": hi - span}))  # type: ignore[operator]
    return device.model_copy(update={"tags": tags})


def _safe_bools(rules: dict[str, AlarmRule], device: Any) -> dict[str, bool]:
    """bool tags with an ``equals`` alarm start in the non-alarm state."""
    return {t.name: not rules[device.tag_path(t.name)].equals for t in device.tags
            if device.tag_path(t.name) in rules
            and rules[device.tag_path(t.name)].equals is not None}


async def _main(config_dir: Path, out: Path, modbus_base: int, mqtt_port: int,
                opcua_base: int) -> int:
    report = load_config(config_dir, env={})
    if report.has_errors:
        print_report(report, sys.stderr)
        return 1
    sim_config = out / "config"
    if sim_config.exists():
        shutil.rmtree(sim_config)
    (sim_config / "devices").mkdir(parents=True)
    for name in (ALARMS_FILE, CONTROL_FILE):
        if (config_dir / name).exists():
            shutil.copy(config_dir / name, sim_config / name)

    modbus_servers: dict[tuple[str, int], ModbusSimServer] = {}
    modbus_devices: list[SimulatedModbusDevice] = []
    mqtt_devices: list[MqttDevice] = []
    opcua_groups: dict[str, list[OpcUaDevice]] = {}
    broker = EmbeddedBroker(mqtt_port)
    files: dict[str, dict[str, Any]] = {}

    for loaded in report.devices:
        d = loaded.device
        data = _dump(d)
        if isinstance(d, ModbusDevice):
            key = (d.connection.host.lower(), str(d.connection.port))
            if key not in modbus_servers:
                modbus_servers[key] = ModbusSimServer("127.0.0.1",
                                                      modbus_base + len(modbus_servers),
                                                      framer="rtu" if d.connection.framer == "rtu"
                                                      else "socket")
            server = modbus_servers[key]
            data["connection"].update(host="127.0.0.1", port=server.port)
        elif isinstance(d, ModbusRtuDevice):
            # a serial line becomes an RTU-framed TCP server; the hub reaches it via socket://
            key = ("serial", d.connection.port.lower())
            if key not in modbus_servers:
                modbus_servers[key] = ModbusSimServer("127.0.0.1",
                                                      modbus_base + len(modbus_servers),
                                                      framer="rtu")
            server = modbus_servers[key]
            data["connection"]["port"] = f"socket://127.0.0.1:{server.port}"
        elif isinstance(d, MqttDevice):
            mqtt_devices.append(d)
        elif isinstance(d, OpcUaDevice):
            opcua_groups.setdefault(d.connection.endpoint_url, []).append(d)
        files[d.device_id] = data

    opcua_servers = []
    for i, (endpoint, group) in enumerate(opcua_groups.items()):
        sim = SimulatedOpcUaServer(group, port=opcua_base + i)
        opcua_servers.append(sim)
        for d in group:
            conn = files[d.device_id]["connection"]
            conn.update(endpoint_url=sim.endpoint, security_policy="None", security_mode="None",
                        auth={"type": "anonymous"})
            conn.pop("client_certificate_env", None)
            conn.pop("client_private_key_env", None)
        log.info("OPC UA %s -> %s", endpoint, sim.endpoint)

    for device_id, data in files.items():
        (sim_config / "devices" / f"{device_id}.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    brokers = [{"broker_id": b, "host": "127.0.0.1", "port": broker.port}
               for b in report.brokers]
    (sim_config / "brokers.yaml").write_text(yaml.safe_dump({"brokers": brokers}), "utf-8")

    check = load_config(sim_config, env={})
    if check.has_errors:  # pragma: no cover - would be a bug in this script
        print_report(check, sys.stderr)
        return 1

    rules = {r.tag: r for r in report.alarms.rules}
    for server in modbus_servers.values():
        await server.start()
    for loaded in check.devices:
        if isinstance(loaded.device, ModbusDevice | ModbusRtuDevice):
            c = loaded.device.connection
            port = (int(c.port.rsplit(":", 1)[1]) if isinstance(loaded.device, ModbusRtuDevice)
                    else c.port)
            server = next(s for s in modbus_servers.values() if s.port == port)
            sim_dev = SimulatedModbusDevice(server, _normal(loaded.device, rules))
            for name, value in _safe_bools(rules, loaded.device).items():
                sim_dev.set(name, value)
            sim_dev.step(0)
            modbus_devices.append(sim_dev)
    if mqtt_devices:
        await broker.start()
    mqtt_sims = []
    for d in mqtt_devices:
        q = SimulatedMqttDevice(_normal(d, rules), broker.host, broker.port)
        q.values.update(_safe_bools(rules, d))
        q.step(0)
        mqtt_sims.append(q)
    for sim in mqtt_sims:
        await sim.connect()
    for sim in opcua_servers:
        sim.devices = [_normal(d, rules) for d in sim.devices]
        await sim.start()
        for d in sim.devices:
            for name, value in _safe_bools(rules, d).items():
                await sim.set(d.device_id, name, value)
        await sim.step(0)

    lines = [f"simulating {len(files)} device(s); config for the hub: {sim_config}",
             f"  Modbus TCP: {', '.join(f'127.0.0.1:{s.port}' for s in modbus_servers.values())}"]
    if mqtt_devices:
        lines.append(f"  MQTT broker: 127.0.0.1:{broker.port}")
    lines += [f"  OPC UA: {sim.endpoint}" for sim in opcua_servers]
    lines.append(f"start the hub:  hub-api --config-dir {sim_config} --dev "
                 f"--embedded-db {out / 'db'}")
    print("\n".join(lines), flush=True)
    try:
        while True:
            await asyncio.sleep(1.0)
            for m in modbus_devices:
                m.step()
            for q in mqtt_sims:
                q.step()
                await q.publish()
            for o in opcua_servers:
                await o.step()
    finally:
        for q in mqtt_sims:
            await q.disconnect()
        await broker.stop()
        for o in opcua_servers:
            await o.stop()
        for server in modbus_servers.values():
            await server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-sim", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--out", type=Path, default=Path(".sim"))
    parser.add_argument("--modbus-port", type=int, default=15020)
    parser.add_argument("--mqtt-port", type=int, default=11883)
    parser.add_argument("--opcua-port", type=int, default=14840)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        return run(_main(args.config_dir, args.out, args.modbus_port, args.mqtt_port,
                         args.opcua_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
