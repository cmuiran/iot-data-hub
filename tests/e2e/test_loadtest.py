"""hub-loadtest: the generated configuration is valid, and a small run completes and passes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hub.config_loader import load_config
from simulators import loadtest


def test_mix_and_split() -> None:
    mix = loadtest.parse_mix("modbus=7,mqtt=2,opcua=1")
    assert mix == pytest.approx({"modbus": 0.7, "mqtt": 0.2, "opcua": 0.1})
    counts = loadtest.split_devices(301, mix)
    assert sum(counts.values()) == 301 and counts["modbus"] == 211
    with pytest.raises(SystemExit):
        loadtest.parse_mix("bacnet=1")


def test_generated_config_is_valid(tmp_path: Path) -> None:
    counts = {"modbus": 25, "mqtt": 6, "opcua": 12}
    dirs = loadtest.generate(tmp_path, counts, tags=15, poll_ms=500)
    tcp = load_config(dirs["tcp"], env={})
    mqtt = load_config(dirs["mqtt"], env={})
    assert not tcp.has_errors, tcp.issues
    assert not mqtt.has_errors, mqtt.issues
    assert len(tcp.devices) == 37 and len(mqtt.devices) == 6
    assert sum(len(d.device.tags) for d in tcp.devices) == 37 * 15
    # one address per Modbus device, ten OPC UA devices per server
    hosts = {d.device.connection.host for d in tcp.devices if d.device.protocol == "modbus_tcp"}
    endpoints = {d.device.connection.endpoint_url for d in tcp.devices
                 if d.device.protocol == "opcua"}
    assert len(hosts) == 25 and len(endpoints) == 2


@pytest.mark.integration
@pytest.mark.postgres
async def test_small_run_passes(tmp_path: Path, database_url: str) -> None:
    args = argparse.Namespace(
        devices=8, tags=5, mix="modbus=0.5,mqtt=0.25,opcua=0.25", poll_ms=1000, seconds=6.0,
        warmup=6.0, out=tmp_path / "load", database_url=database_url,
        embedded_db=tmp_path / "unused", base_port=24000, sim_timeout=120.0,
        json=tmp_path / "result.json", profile=None)
    assert await loadtest._main(args) == 0
    doc = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert doc["ok"] and doc["online_end"] == 8 and doc["samples_dropped"] == 0
    assert doc["samples_stored"] > 0 and doc["checks"]["devices online"]
