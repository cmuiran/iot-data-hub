"""config/building.yaml: model validation and cross-checks against devices and tags."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from hub.config_loader import Severity, load_config
from hub.models import BuildingFile, Equipment, Location, TagBase
from tests.hub.builders import modbus_device

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def base_building() -> dict[str, Any]:
    return {
        "organizations": [{"id": "gate", "name": "GATE", "sites": ["kish"]}],
        "locations": [
            {"id": "campus", "kind": "campus", "name": "Campus", "site": "kish"},
            {"id": "b1", "kind": "building", "name": "B1", "parent": "campus"},
            {"id": "f1", "kind": "floor", "name": "F1", "parent": "b1", "level": 1},
            {"id": "z1", "kind": "zone", "name": "Z1", "parent": "f1"},
            {"id": "r101", "kind": "room", "name": "R101", "parent": "z1"},
            {"id": "r102", "kind": "room", "name": "R102", "parent": "f1"},
        ],
        "systems": [{"id": "hvac", "kind": "hvac", "name": "HVAC", "building": "b1"}],
        "equipment": [{
            "id": "meter_a", "name": "Meter A", "type": "meter", "location": "r101",
            "system": "hvac", "device": "meter_01", "manufacturer": "ACME", "model": "M1",
            "serial_number": "SN-1", "installed_on": "2024-01-01",
            "documents": [{"title": "Manual", "ref": "https://example.com/m1.pdf"}],
            "haystack": ["elec", "meter"], "brick_class": "Electrical_Meter",
            "points": [{"role": "voltage", "tag": "kish.substation.meter_01.voltage"}],
        }],
    }


def load(tmp_path: Path, building: dict[str, Any], devices: list[dict] | None = None) -> Any:
    root = tmp_path / "config"
    (root / "devices").mkdir(parents=True)
    for d in devices if devices is not None else [modbus_device()]:
        (root / "devices" / f"{d['device_id']}.yaml").write_text(yaml.safe_dump(d), "utf-8")
    (root / "building.yaml").write_text(yaml.safe_dump(building), "utf-8")
    return load_config(root, env={})


def errors(report: Any) -> list[str]:
    return [f"{i.location}: {i.message}" for i in report.issues
            if i.severity is Severity.ERROR]


def test_valid_model_and_lookups(tmp_path: Path) -> None:
    report = load(tmp_path, base_building())
    assert errors(report) == []
    model = report.building_model
    assert [loc.id for loc in model.ancestors("r101")] == ["r101", "z1", "f1", "b1", "campus"]
    assert model.site_of("r101") == "kish" and model.building_of("r101") == "b1"
    assert set(model.subtree("f1")) == {"f1", "z1", "r101", "r102"}
    assert model.points_in("b1", "voltage") == ["kish.substation.meter_01.voltage"]
    assert model.organization_of_site("kish") == "gate"
    assert model.point_of_tag["kish.substation.meter_01.voltage"] == ("meter_a", "voltage")


def test_repository_building_model_is_valid() -> None:
    report = load_config(REPO_CONFIG, env={})
    assert not [i for i in report.issues if i.source == "building.yaml"], report.issues
    assert report.building_model.points_in("kish_campus", "active_power")


@pytest.mark.parametrize(("change", "message"), [
    (lambda b: b["locations"].append({"id": "f9", "kind": "floor", "name": "F", "parent": "r101"}),
     "a floor goes under building"),
    (lambda b: b["locations"].append({"id": "b2", "kind": "building", "name": "B"}),
     "not under the top level"),
    (lambda b: b["locations"].append({"id": "x", "kind": "room", "name": "X", "parent": "nope"}),
     "unknown parent"),
    (lambda b: b["locations"].append(dict(b["locations"][1])), "duplicate location id"),
    (lambda b: b["systems"].append({"id": "s2", "kind": "water", "name": "W",
                                    "building": "f1"}), "is not a building"),
    (lambda b: b["equipment"][0].update(location="nowhere"), "unknown location"),
    (lambda b: b["equipment"][0].update(location="campus"), "not directly on a campus"),
    (lambda b: b["equipment"][0].update(system="nope"), "unknown system"),
    (lambda b: b["equipment"][0]["points"].append({"role": "x", "tag": "kish.a.b.c"}),
     "unknown tag"),
    (lambda b: b["equipment"].append({**b["equipment"][0], "id": "meter_b"}),
     "already a point of meter_a"),
    (lambda b: b["organizations"].append({"id": "o2", "name": "O2", "sites": ["kish"]}),
     "belongs to two organizations"),
    (lambda b: b["locations"].append({"id": "c2", "kind": "campus", "name": "C2",
                                      "site": "tehran"}), "is in no organization"),
])
def test_cross_checks(tmp_path: Path, change: Any, message: str) -> None:
    building = base_building()
    change(building)
    assert any(message in e for e in errors(load(tmp_path, building))), \
        errors(load(tmp_path / "again", building))


def test_fire_systems_are_monitoring_only(tmp_path: Path) -> None:
    building = base_building()
    building["systems"].append({"id": "fire", "kind": "fire", "name": "Fire", "building": "b1"})
    building["equipment"][0].update(system="fire")
    device = modbus_device()
    device["tags"].append({"name": "reset", "data_type": "bool", "register_type": "coil",
                           "address": 0, "access": "read_write"})
    building["equipment"][0]["points"].append({"role": "reset",
                                               "tag": "kish.substation.meter_01.reset"})
    assert any("monitoring only" in e for e in errors(load(tmp_path, building, [device])))


def test_unknown_device_and_unassigned_site_warn(tmp_path: Path) -> None:
    building = base_building()
    building["equipment"][0]["device"] = "ghost"
    building["organizations"][0]["sites"] = ["other"]
    building["locations"][0]["site"] = "other"
    report = load(tmp_path, building)
    warnings = [i.message for i in report.issues if i.severity is Severity.WARNING]
    assert any("unknown device 'ghost'" in w for w in warnings)
    assert any("site 'kish' belongs to no organization" in w for w in warnings)


@pytest.mark.parametrize(("model", "data", "message"), [
    (Location, {"id": "c", "kind": "campus", "name": "C"}, "needs site"),
    (Location, {"id": "b", "kind": "building", "name": "B", "site": "kish"}, "inherit"),
    (Location, {"id": "r", "kind": "room", "name": "R", "level": 2}, "only applies to floors"),
    (Location, {"id": "B-1", "kind": "room", "name": "R"}, "id"),
    (Equipment, {"id": "e", "name": "E", "type": "pump", "location": "r",
                 "points": [{"role": "a", "tag": "s.a.d.t"}, {"role": "a", "tag": "s.a.d.u"}]},
     "roles must be unique"),
    (Equipment, {"id": "e", "name": "E", "type": "pump", "location": "r",
                 "installed_on": "2025-01-01", "warranty_until": "2024-01-01"}, "warranty"),
    (Equipment, {"id": "e", "name": "E", "type": "pump", "location": "r",
                 "documents": [{"title": "x", "ref": "javascript:alert(1)"}]}, "http"),
    (Equipment, {"id": "e", "name": "E", "type": "pump", "location": "r",
                 "documents": [{"title": "x", "ref": "../../etc/passwd"}]}, "without '..'"),
    (Equipment, {"id": "e", "name": "E", "type": "pump", "location": "r",
                 "haystack": ["Bad Marker"]}, "Haystack"),
    (Equipment, {"id": "e", "name": "E", "type": "pump", "location": "r",
                 "brick_class": "pump"}, "Brick"),
    (BuildingFile, {"rooms": []}, "Extra inputs"),
])
def test_model_validation(model: Any, data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(data)


def test_write_class() -> None:
    with pytest.raises(ValidationError, match="only applies to read_write"):
        TagBase(name="t", data_type="float32", write_class="setpoint")
    tag = TagBase(name="t", data_type="float32", access="read_write", min=0, max=10,
                  write_class="setpoint")
    assert tag.write_class == "setpoint"
