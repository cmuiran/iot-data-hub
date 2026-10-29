"""Product templates (hub.platform.template) and device identifiers (hub.platform.devices).

The central check: every device of the repository configuration (one per protocol) can be
expressed as a product template + binding + per-device connection, and building the device
from it gives the same tags and connection as the YAML. So a new product of any supported
protocol needs no code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from hub.config_loader import load_config
from hub.models import Access, CalcDevice, DataType
from hub.platform.devices import (
    PLATFORM_AREA,
    PLATFORM_SITE,
    ProductIn,
    build_device,
    identifiers,
    is_platform_device_id,
    new_device_id,
    platform_tag_path,
    validate_identifier,
    validate_identifier_pattern,
)
from hub.platform.template import (
    RESERVED_DEVICE_FIELDS,
    TEMPLATE_TAG_FIELDS,
    ProductTemplate,
    device_classes,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _kind(tag: Any) -> str:
    writable = tag.access is Access.READ_WRITE
    if tag.data_type is DataType.BOOL:
        return "digital_output" if writable else "digital_input"
    if tag.data_type is DataType.STRING:
        return "variable"
    return "parameter" if writable else "analog_input"


def _as_template(device: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(template, connection, options) equivalent to a YAML device."""
    tags, addresses = [], {}
    for t in device.tags:
        fields = t.model_dump(mode="json", exclude_unset=True)
        definition = {k: v for k, v in fields.items() if k in TEMPLATE_TAG_FIELDS - {"name"}}
        tags.append({"key": t.name, "kind": _kind(t), "display_name": {"en": t.name}}
                    | definition)
        addresses[t.name] = {k: v for k, v in fields.items() if k not in TEMPLATE_TAG_FIELDS}
    dumped = device.model_dump(mode="json", exclude_unset=True)
    options = {k: v for k, v in dumped.items() if k not in RESERVED_DEVICE_FIELDS}
    template = {"tags": tags, "bindings": {device.protocol: {"tags": addresses}}}
    return template, dumped["connection"], options


def _repo_devices() -> list[Any]:
    report = load_config(REPO_CONFIG, env={})
    assert not report.has_errors, report.issues
    return [d.device for d in report.devices if not isinstance(d.device, CalcDevice)]


@pytest.mark.parametrize("device", _repo_devices(), ids=lambda d: d.protocol)
def test_every_repo_device_can_be_a_product(device: Any) -> None:
    raw, connection, options = _as_template(device)
    template = ProductTemplate.model_validate(raw)
    device_id = new_device_id()
    built = build_device(device_id=device_id, name="Product X", template=template,
                         protocol=device.protocol, connection=connection, options=options)
    assert type(built) is type(device)
    assert built.tags == device.tags
    assert built.connection == device.connection
    assert (built.site, built.area, built.device_id) == (PLATFORM_SITE, PLATFORM_AREA, device_id)
    assert built.tag_path(device.tags[0].name) == platform_tag_path(device_id,
                                                                   device.tags[0].name)


def test_repo_covers_most_protocols() -> None:
    covered = {d.protocol for d in _repo_devices()}
    assert covered >= set(device_classes()) - {"modbus_rtu", "native"}


RELAYS: list[dict[str, Any]] = [
    {"key": f"relay_{i}", "kind": "digital_output", "display_name": {"fa": f"رله {i}"},
     "data_type": "bool", "access": "read_write", "write_class": "command"} for i in range(1, 9)]
POWER = {"key": "power", "kind": "analog_input", "display_name": {"fa": "توان", "en": "Power"},
         "data_type": "float32", "unit": "kW", "min": 0, "max": 10,
         "ui": {"chart": True, "order": 9}}


def relay_template(**changes: Any) -> dict[str, Any]:
    modbus = {f"relay_{i}": {"register_type": "coil", "address": i - 1} for i in range(1, 9)}
    mqtt = {f"relay_{i}": {"json_path": f"$.relays[{i - 1}]"} for i in range(1, 9)}
    return {"tags": [*RELAYS, POWER], "bindings": {
        "modbus_tcp": {"connection": {"port": 502, "unit_id": 1},
                       "device": {"poll_interval_ms": 500},
                       "tags": modbus | {"power": {"register_type": "input", "address": 0}}},
        "mqtt": {"connection": {"broker_ref": "main",
                                "telemetry_topic": "d/{device_id}/telemetry",
                                "command_topic": "d/{device_id}/cmd"},
                 "tags": mqtt | {"power": {"json_path": "$.power"}}},
    }} | changes


def test_one_template_two_protocols() -> None:
    template = ProductTemplate.model_validate(relay_template())
    device_id = new_device_id()
    modbus = build_device(device_id=device_id, name="R", template=template,
                          protocol="modbus_tcp", connection={"host": "10.0.0.5"})
    assert modbus.connection.port == 502 and modbus.poll_interval_ms == 500
    assert [t.name for t in modbus.tags] == [f"relay_{i}" for i in range(1, 9)] + ["power"]
    mqtt = build_device(device_id=device_id, name="R", template=template, protocol="mqtt",
                        connection={})
    assert mqtt.connection.telemetry_topic == f"d/{device_id}/telemetry"
    assert mqtt.tags[0].write_class == "command"
    with pytest.raises(ValueError, match="no opcua binding"):
        build_device(device_id=device_id, name="R", template=template, protocol="opcua",
                     connection={})
    with pytest.raises(ValidationError, match="host"):        # the device's own part missing
        build_device(device_id=device_id, name="R", template=template, protocol="modbus_tcp",
                     connection={})


def test_any_number_of_tags() -> None:
    n = 40
    tags = [{"key": f"out_{i}", "kind": "digital_output", "display_name": {"en": f"Out {i}"},
             "data_type": "bool", "access": "read_write"} for i in range(n)]
    binding = {f"out_{i}": {"register_type": "coil", "address": i} for i in range(n)}
    template = ProductTemplate.model_validate(
        {"tags": tags, "bindings": {"modbus_tcp": {"tags": binding}}})
    assert len(template.tags) == n


@pytest.mark.parametrize(("change", "message"), [
    ({"tags": [*RELAYS, POWER, POWER]}, "duplicate tag keys: power"),
    ({"tags": [*RELAYS, POWER | {"access": "read_write"}]}, "read-only"),
    ({"tags": [*RELAYS, POWER | {"kind": "digital_input"}]}, "bool, not float32"),
    ({"tags": [*RELAYS, POWER | {"default": 50}]}, "outside min/max"),
    ({"tags": [*RELAYS, POWER | {"default": "high"}]}, "not a float32"),
    ({"tags": [*RELAYS, POWER | {"kind": "parameter", "access": "read_write", "min": None}]},
     "min and max"),
    ({"tags": [*RELAYS, POWER | {"display_name": {}}]}, "at least 1"),
    ({"tags": [*RELAYS, POWER | {"key": "Power"}]}, "snake_case"),
])
def test_invalid_tags(change: dict[str, Any], message: str) -> None:
    raw = relay_template(**change)
    if any(t["key"] == "Power" for t in raw["tags"]):  # keep bindings consistent with keys
        for binding in raw["bindings"].values():
            binding["tags"]["Power"] = binding["tags"].pop("power")
    with pytest.raises(ValidationError, match=message):
        ProductTemplate.model_validate(raw)


def test_invalid_bindings() -> None:
    raw = relay_template()
    del raw["bindings"]["modbus_tcp"]["tags"]["power"]
    with pytest.raises(ValidationError, match="no address for power"):
        ProductTemplate.model_validate(raw)

    raw = relay_template()
    raw["bindings"]["modbus_tcp"]["tags"]["extra"] = {"register_type": "coil", "address": 9}
    with pytest.raises(ValidationError, match="unknown or calculated tags extra"):
        ProductTemplate.model_validate(raw)

    raw = relay_template()
    raw["bindings"]["modbus_tcp"]["tags"]["power"]["data_type"] = "int16"
    with pytest.raises(ValidationError, match="come from the template"):
        ProductTemplate.model_validate(raw)

    raw = relay_template()                          # protocol rules: bool needs a coil/bit
    raw["bindings"]["modbus_tcp"]["tags"]["relay_1"] = {"register_type": "input", "address": 0}
    with pytest.raises(ValidationError, match="binding modbus_tcp, tag relay_1"):
        ProductTemplate.model_validate(raw)

    raw = relay_template()
    raw["bindings"]["modbus_tcp"]["device"] = {"site": "x"}
    with pytest.raises(ValidationError, match="may not set site"):
        ProductTemplate.model_validate(raw)

    for protocol in ("calc", "bacnet"):
        raw = relay_template()
        raw["bindings"][protocol] = raw["bindings"].pop("mqtt")
        with pytest.raises(ValidationError, match="unknown protocol"):
            ProductTemplate.model_validate(raw)


def test_device_ids() -> None:
    ids = {new_device_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(is_platform_device_id(i) and len(i) == 21 for i in ids)
    assert not is_platform_device_id("meter_01")


def test_identifiers() -> None:
    assert identifiers("CTRL-8R-{0000}", 1, 3) == ["CTRL-8R-0001", "CTRL-8R-0002",
                                                   "CTRL-8R-0003"]
    assert identifiers("{000}-X", 998, 2) == ["998-X", "999-X"]
    assert len(set(identifiers("CTRL-8R-{0000}", 1, 1000))) == 1000
    with pytest.raises(ValueError, match="does not fit"):
        identifiers("CTRL-{00}", 99, 2)
    for bad in ("ctrl-{0000}", "CTRL-0000", "CTRL-{0000}-{00}", "CTRL {0000}", "{0}"):
        with pytest.raises(ValueError):
            validate_identifier_pattern(bad)
    assert validate_identifier("  ctrl-8r-0001 ") == "CTRL-8R-0001"
    with pytest.raises(ValueError):
        validate_identifier("CTRL_8R")
    with pytest.raises(ValidationError):
        ProductIn(code="ctrl", name="x", identifier_pattern="CTRL-{0000}")


def test_yaml_may_not_use_platform_names(tmp_path: Path) -> None:
    from tests.hub.builders import modbus_device

    for name, device, message in (
            ("a", modbus_device("meter_9", site="platform"), "reserved for platform devices"),
            ("b", modbus_device(new_device_id()), "generated platform device id")):
        devices = tmp_path / name / "devices"
        devices.mkdir(parents=True)
        (devices / "d.yaml").write_text(yaml.safe_dump(device), encoding="utf-8")
        report = load_config(tmp_path / name, env={})
        assert any(message in i.message for i in report.issues), report.issues
