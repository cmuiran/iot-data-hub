"""hub-commission: the pure helpers, and every command against the simulators (hub-sim)."""

from __future__ import annotations

import asyncio
import re
import struct
from pathlib import Path

import pytest
import yaml

from hub import commission
from hub.codec import encode_registers
from hub.commission import (
    flatten,
    interpretations,
    parse_ref,
    parse_units,
    s7_interpretations,
    scale_for,
    tag_name,
)
from hub.models import DataType, parse_json_path, validate_segment
from simulators import run as sim_run
from simulators.mqtt_sim import free_port

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


# --- helpers -----------------------------------------------------------------------------------


def test_tag_names_are_valid_and_unique() -> None:
    taken: set[str] = set()
    names = [tag_name(t, taken) for t in ("MotorSpeed", "Motor Speed", "1st value", "Druck (bar)",
                                          "ÜberTemp", "", "x" * 100)]
    assert names[:2] == ["motor_speed", "motor_speed_2"]
    assert names[2] == "t_1st_value" and names[3] == "druck_bar"
    for name in names:
        validate_segment(name)
    assert len(set(names)) == len(names)


def test_flatten_gives_paths_the_hub_accepts() -> None:
    doc = {"data": {"temp": 21.5, "list": [1, {"x": True}]}, "key with space": 3, "a-b": "s"}
    leaves = dict(flatten(doc))
    assert leaves == {"$.data.temp": 21.5, "$.data.list[0]": 1, "$.data.list[1].x": True,
                      "$['key with space']": 3, "$.a-b": "s"}
    for path in leaves:
        parse_json_path(path)


def test_datasheet_references_and_units() -> None:
    assert parse_ref(40001) == ("holding", 0)
    assert parse_ref(30101) == ("input", 100)
    assert parse_ref(400101) == ("holding", 100)
    assert parse_ref(10005) == ("discrete_input", 4)
    for bad in (40000, 123, 90001):
        with pytest.raises(commission.UsageError):
            parse_ref(bad)
    assert parse_units("1-3, 10") == [1, 2, 3, 10]
    with pytest.raises(commission.UsageError):
        parse_units("1-300")


def test_interpretations_find_the_right_word_order_and_scale() -> None:
    regs = [0x1234] + encode_registers(230.4, DataType.FLOAT32, "big", "little")
    found = interpretations(99, regs)
    hits = [x for x in found if x.data_type is DataType.FLOAT32 and x.address == 100
            and scale_for(float(x.value), 230.4, 0.001) == 1]
    assert [(x.byte_order, x.word_order) for x in hits] == [("big", "little")]
    assert scale_for(2304, 230.4, 0.01) == 0.1
    assert scale_for(23040, 230.4, 0.01) == 0.01
    assert scale_for(0, 230.4, 0.01) is None


def test_s7_interpretations() -> None:
    data = struct.pack(">fhH", 41.5, -7, 500)
    rows = {(off, dt): v for off, _, dt, v in s7_interpretations(data, 10)}
    assert rows[(10, DataType.FLOAT32)] == pytest.approx(41.5)
    assert rows[(14, DataType.INT16)] == -7 and rows[(16, DataType.UINT16)] == 500
    assert commission.s7_address("DB", 3, 10, "D") == "DB3.DBD10"
    assert commission.s7_address("M", 0, 20, "W") == "MW20"


def test_the_tool_never_writes() -> None:
    """Commissioning is read-only: no write primitive of any protocol may appear in it."""
    source = (Path(commission.__file__)).read_text(encoding="utf-8")
    forbidden = [r"\bwrite_(coil|register|registers|tag|area|value|values)\b", r"\bset_cmd\b",
                 r"\.publish\(", r"db_write", r"write_raw", r"\.put\(", r"\.post\("]
    for pattern in forbidden:
        assert not re.search(pattern, source), pattern


# --- against the simulators ----------------------------------------------------------------


async def _run(argv: list[str]) -> int:
    args = commission._parser().parse_args(argv)
    result: int = await args.func(args)
    return result


async def test_commands_against_the_simulators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    ports = [free_port() for _ in range(6)]
    task = asyncio.create_task(sim_run._main(REPO_CONFIG, tmp_path, *ports))
    try:
        cfg = tmp_path / "config"
        for _ in range(200):
            if (cfg / "brokers.yaml").exists() and not task.done():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(2.0)
        assert not task.done(), task.exception() if task.done() else None
        for line in (tmp_path / "sim.env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                monkeypatch.setenv(key, value)
        c = ["--config-dir", str(cfg)]
        capsys.readouterr()

        # read: every device online, every tag good, a SAT sheet written
        out_md = tmp_path / "sat" / "all.md"
        assert await _run(c + ["read", "--seconds", "4", "--out", str(out_md)]) == 0
        text = capsys.readouterr().out
        assert "6/6 device(s) passed" in text
        sheet = out_md.read_text(encoding="utf-8")
        assert "| Tag | Value |" in sheet and "PASS" in sheet
        assert await _run(c + ["read", "meter_01", "--seconds", "2", "--out",
                               str(tmp_path / "m.json")]) == 0
        assert '"ok": true' in (tmp_path / "m.json").read_text(encoding="utf-8")
        with pytest.raises(commission.UsageError):
            await _run(c + ["read", "no_such_device"])
        capsys.readouterr()

        # modbus-probe with the configured meter: voltage_l1 is float32 at input 0
        assert await _run(c + ["modbus-probe", "--device", "meter_01", "--register", "input",
                               "--address", "0", "--count", "2"]) == 0
        text = capsys.readouterr().out
        assert "input 0..1" in text and "float32=" in text and "<=" in text
        # a wrong address answers with an exception, explained
        assert await _run(c + ["modbus-probe", "--device", "meter_01", "--address", "9000"]) == 1
        assert "illegal data address" in capsys.readouterr().out
        # scan: unit 1 answers, 2..3 do not
        assert await _run(c + ["modbus-scan", "--device", "meter_01", "--units", "1-3",
                               "--register", "input"]) == 0
        assert "1 unit(s) answered: 1" in capsys.readouterr().out

        # opcua-browse lists the PLC's variables and suggests valid tags
        assert await _run(c + ["opcua-browse", "--device", "plc_01", "--yaml"]) == 0
        text = capsys.readouterr().out
        assert "Pump1.MotorSpeed" in text
        tags = yaml.safe_load(text.split("check names, units, scale):", 1)[1])["tags"]
        assert {"node_id", "data_type", "name"} <= set(tags[0])

        # mqtt-listen on the device's telemetry topic
        assert await _run(c + ["mqtt-listen", "--device", "env_sensor_01", "--seconds", "3",
                               "--yaml"]) == 0
        text = capsys.readouterr().out
        assert "$.data.temp" in text and "telemetry_topic:" in text

        # snmp-walk of the UPS (community from the simulator's env file), ends cleanly
        assert await _run(c + ["snmp-walk", "--device", "ups_01", "--oid", "1.3.6.1.2.1.33",
                               "--yaml"]) == 0
        text = capsys.readouterr().out
        assert "1.3.6.1.2.1.33.1.2.4.0" in text and "charge_remaining" in text
        assert "stopped after" not in text

        # s7-dump: the chlorine setpoint (REAL at DB2.DBD0) is found from the value shown
        dump = c + ["s7-dump", "--device", "plc_s7_01", "--db", "2", "--size", "8"]
        assert await _run(dump) == 0
        row = next(line for line in capsys.readouterr().out.splitlines()
                   if re.match(r"\s+0\s+-?\d+\s+\d+\s+-?\d+\s+\S+$", line))
        setpoint = row.split()[-1]
        assert await _run(dump + ["--expect", setpoint]) == 0
        assert "DB2.DBD0 float32" in capsys.readouterr().out

        # http-get reads every endpoint of the inverter's tags
        assert await _run(c + ["http-get", "--device", "inverter_01", "--yaml"]) == 0
        text = capsys.readouterr().out
        assert "/api/v1/status" in text and "$.ac.power_kw" in text
    finally:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task


def test_cli_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert commission.main(["--config-dir", str(REPO_CONFIG), "modbus-probe", "--address",
                            "1"]) == 2
    assert "give --host" in capsys.readouterr().err
    assert commission.main(["--env-file", "missing.env", "read"]) == 2
    assert commission.main(["s7-dump", "--host", "x", "--size", "4096"]) == 2
