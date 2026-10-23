from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from hub.validate_config import main
from tests.hub.builders import WriteConfig, modbus_device, mqtt_device

REPO_ROOT = Path(__file__).resolve().parents[2]


def run(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(args), out=out)
    return code, out.getvalue()


def test_ok_config_exits_zero(write_config: WriteConfig) -> None:
    code, output = run("--config-dir", str(write_config(modbus_device())))
    assert code == 0
    assert "OK       meter_01" in output
    assert "Devices: 1 (1 OK, 0 WARNING, 0 ERROR)" in output


def test_errors_exit_non_zero(write_config: WriteConfig) -> None:
    bad = mqtt_device()
    bad["connection"]["broker_ref"] = "missing"
    code, output = run("--config-dir", str(write_config(modbus_device(), bad)))
    assert code == 1
    assert "ERROR    sensor_01" in output
    assert "connection.broker_ref: unknown broker_ref 'missing'" in output
    assert "Devices: 2 (1 OK, 0 WARNING, 1 ERROR)" in output


def test_warnings_pass_unless_strict(write_config: WriteConfig) -> None:
    config = str(write_config(modbus_device(tags=[])))
    code, output = run("--config-dir", config)
    assert code == 0
    assert "WARNING  meter_01" in output
    assert run("--config-dir", config, "--strict")[0] == 1


def test_missing_config_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--config-dir", str(tmp_path / "nope"))[0] == 2
    assert "config directory not found" in capsys.readouterr().err


def test_module_entry_point_on_repository_config() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "hub.validate_config"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "meter_01" in result.stdout
