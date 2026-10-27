"""Simulate every device of a config directory on this PC.

    hub-sim --config-dir config --out .sim

Writes ``.sim/config``: a copy of the configuration pointing at local simulators (Modbus TCP
servers on 127.0.0.1:15020+, an embedded MQTT broker, OPC UA servers on 127.0.0.1:14840+, S7
PLCs on 127.0.0.1:15102+, one HTTP server, SNMP agents on 127.0.0.1:16161+, Omron FINS / MELSEC
MC / Logix EtherNet/IP / IEC 104 stations on 127.0.0.1:15300+; security and
credentials removed, except SNMP, which needs a community: it is written to
``.sim/sim.env``), then runs the simulators until Ctrl+C. Start the hub against the copy in
another terminal, e.g.::

    hub-api --config-dir .sim/config --env-file .sim/sim.env --dev --embedded-db .sim/db

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

from hub.config_loader import (
    ALARMS_FILE,
    BUILDING_FILE,
    CONTROL_FILE,
    MODBUS_SERVER_FILE,
    NORTHBOUND_FILE,
    load_config,
)
from hub.models import (
    AlarmRule,
    EnipDevice,
    FinsDevice,
    HttpDevice,
    Iec104Device,
    McDevice,
    ModbusDevice,
    ModbusRtuDevice,
    MqttDevice,
    OpcUaDevice,
    S7Device,
    SnmpDevice,
)
from hub.runtime import run
from hub.validate_config import print_report
from simulators.enip_sim import EnipSimServer, SimulatedEnipDevice
from simulators.fins_sim import FinsSimServer, SimulatedFinsDevice
from simulators.http_sim import HttpSimServer, SimulatedHttpDevice
from simulators.iec104_sim import Iec104SimServer, SimulatedIec104Device
from simulators.mc_sim import McSimServer, SimulatedMcDevice
from simulators.modbus_sim import ModbusSimServer, SimulatedModbusDevice
from simulators.mqtt_sim import EmbeddedBroker, SimulatedMqttDevice
from simulators.opcua_sim import SimulatedOpcUaServer
from simulators.s7_sim import S7SimServer, SimulatedS7Device
from simulators.snmp_sim import SimulatedSnmpDevice, SnmpSimAgent

SIM_COMMUNITY_ENV = "HUB_SIM_SNMP_COMMUNITY"

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
                opcua_base: int, s7_base: int = 15102, http_port: int = 18080,
                snmp_base: int = 16161, plc_base: int = 15300) -> int:
    report = load_config(config_dir, env={})
    if report.has_errors:
        print_report(report, sys.stderr)
        return 1
    sim_config = out / "config"
    if sim_config.exists():
        shutil.rmtree(sim_config)
    (sim_config / "devices").mkdir(parents=True)
    for name in (ALARMS_FILE, CONTROL_FILE, NORTHBOUND_FILE, MODBUS_SERVER_FILE,
                 BUILDING_FILE):
        if (config_dir / name).exists():
            shutil.copy(config_dir / name, sim_config / name)

    modbus_servers: dict[tuple[str, int], ModbusSimServer] = {}
    modbus_devices: list[SimulatedModbusDevice] = []
    mqtt_devices: list[MqttDevice] = []
    opcua_groups: dict[str, list[OpcUaDevice]] = {}
    broker = EmbeddedBroker(mqtt_port)
    files: dict[str, dict[str, Any]] = {}
    s7_servers: dict[tuple[str, int], S7SimServer] = {}
    s7_devices: list[S7Device] = []
    http_server = HttpSimServer(http_port)
    http_devices: list[HttpDevice] = []
    snmp_agents: dict[str, SnmpSimAgent] = {}
    plc_ports: dict[tuple[str, str, int], int] = {}     # (protocol, host, port) -> local port
    plc_devices: list[Any] = []
    community = "hubsim"

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
        elif isinstance(d, S7Device):
            key = (d.connection.host.lower(), d.connection.port)
            if key not in s7_servers:
                s7_servers[key] = S7SimServer(s7_base + len(s7_servers))
            data["connection"].update(host="127.0.0.1", port=s7_servers[key].port)
            s7_devices.append(d)
        elif isinstance(d, HttpDevice):
            data["connection"].update(base_url=http_server.base_url(d.device_id),
                                      auth={"type": "none"}, verify_tls=True)
            data["connection"].pop("ca_file_env", None)
            http_devices.append(d)
        elif isinstance(d, SnmpDevice):
            agent = snmp_agents.setdefault(d.device_id, SnmpSimAgent(snmp_base + len(snmp_agents)))
            agent.add_community(community)
            data["connection"].update(host="127.0.0.1", port=agent.port,
                                      security={"version": "2c",
                                                "community_env": SIM_COMMUNITY_ENV})
        elif isinstance(d, FinsDevice | McDevice | EnipDevice | Iec104Device):
            key = (d.protocol, d.connection.host.lower(), d.connection.port)
            if key not in plc_ports:
                plc_ports[key] = plc_base + len(plc_ports)
            data["connection"].update(host="127.0.0.1", port=plc_ports[key])
            plc_devices.append(d)
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
    (out / "sim.env").write_text(f"# written by hub-sim: credentials of the simulators only\n"
                                 f"{SIM_COMMUNITY_ENV}={community}\n", encoding="utf-8")
    (sim_config / "brokers.yaml").write_text(yaml.safe_dump({"brokers": brokers}), "utf-8")

    check = load_config(sim_config, env={})
    if check.has_errors:  # pragma: no cover - would be a bug in this script
        print_report(check, sys.stderr)
        return 1

    rules = {r.tag: r for r in report.alarms.rules}
    for server in modbus_servers.values():
        await server.start()
    s7_sims = []
    sim_by_id = {loaded.device.device_id: loaded.device for loaded in check.devices}
    for d in s7_devices:
        server = s7_servers[(d.connection.host.lower(), d.connection.port)]
        sim7 = SimulatedS7Device(server, _normal(sim_by_id[d.device_id], rules))
        for name, value in _safe_bools(rules, d).items():
            sim7.set(name, value)
        s7_sims.append(sim7)
    for server7 in s7_servers.values():
        server7.start()
    http_sims = [http_server.add(SimulatedHttpDevice(_normal(sim_by_id[d.device_id], rules)))
                 for d in http_devices]
    if http_devices:
        await http_server.start()
    snmp_sims = []
    for device_id, agent in snmp_agents.items():
        snmp_sims.append(SimulatedSnmpDevice(agent, _normal(sim_by_id[device_id], rules)))
        agent.start()
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
    if mqtt_devices or report.northbound.targets:   # northbound publishes to it too
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

    plc_servers: dict[int, Any] = {}
    plc_sims: list[Any] = []
    for d in plc_devices:
        sim_dev_cfg = _normal(sim_by_id[d.device_id], rules)
        port = sim_dev_cfg.connection.port
        if port not in plc_servers:
            if isinstance(d, FinsDevice):
                plc_servers[port] = FinsSimServer(port, transport=d.connection.transport)
            elif isinstance(d, McDevice):
                plc_servers[port] = McSimServer(port, transport=d.connection.transport)
            elif isinstance(d, EnipDevice):
                plc_servers[port] = EnipSimServer(port)
            else:
                plc_servers[port] = Iec104SimServer(port)
            await plc_servers[port].start()
        server = plc_servers[port]
        sim_cls: Any = {FinsSimServer: SimulatedFinsDevice, McSimServer: SimulatedMcDevice,
                        EnipSimServer: SimulatedEnipDevice,
                        Iec104SimServer: SimulatedIec104Device}[type(server)]
        plc = sim_cls(server, sim_dev_cfg)
        for name, value in _safe_bools(rules, d).items():
            plc.set(name, value)
        plc_sims.append(plc)

    lines = [f"simulating {len(files)} device(s); config for the hub: {sim_config}",
             f"  Modbus TCP: {', '.join(f'127.0.0.1:{s.port}' for s in modbus_servers.values())}"]
    if mqtt_devices or report.northbound.targets:
        lines.append(f"  MQTT broker: 127.0.0.1:{broker.port}")
    lines += [f"  OPC UA: {sim.endpoint}" for sim in opcua_servers]
    lines += [f"  S7 PLC: 127.0.0.1:{s.port}" for s in s7_servers.values()]
    if http_devices:
        lines.append(f"  HTTP: http://127.0.0.1:{http_server.port}/<device_id>")
    lines += [f"  SNMP ({device_id}): 127.0.0.1:{a.port}/udp"
              for device_id, a in snmp_agents.items()]
    for (protocol, host, _), port in plc_ports.items():
        lines.append(f"  {protocol} {host} -> 127.0.0.1:{port}")
    lines.append(f"start the hub:  hub-api --config-dir {sim_config} --env-file "
                 f"{out / 'sim.env'} --dev --embedded-db {out / 'db'}")
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
            for sim in [*s7_sims, *http_sims, *snmp_sims]:
                sim.step()
            for plc in plc_sims:
                result = plc.step()
                if asyncio.iscoroutine(result):             # IEC 104 sends spontaneous data
                    await result
    finally:
        for plc_server in plc_servers.values():
            await plc_server.stop()
        for server7 in s7_servers.values():
            server7.stop()
        for agent in snmp_agents.values():
            agent.stop()
        await http_server.stop()
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
    parser.add_argument("--s7-port", type=int, default=15102)
    parser.add_argument("--http-port", type=int, default=18080)
    parser.add_argument("--snmp-port", type=int, default=16161)
    parser.add_argument("--plc-port", type=int, default=15300,
                        help="first port for FINS, MC, EtherNet/IP and IEC 104 stations")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        return run(_main(args.config_dir, args.out, args.modbus_port, args.mqtt_port,
                         args.opcua_port, args.s7_port, args.http_port, args.snmp_port,
                         args.plc_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
