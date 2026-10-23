from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hub.history import HistoryFilter
from hub.models import ModbusTag, Quality, Sample

T0 = datetime(2026, 10, 1, tzinfo=UTC)
PATH = "kish.gate.meter_02.voltage"


def tag(**kw: Any) -> ModbusTag:
    return ModbusTag.model_validate({"name": "voltage", "data_type": "float32",
                                     "register_type": "input", "address": 0} | kw)


def run(t: ModbusTag, values: list[tuple[float, Any, Quality]], heartbeat: int = 300
        ) -> list[Any]:
    f = HistoryFilter()
    kept = []
    for seconds, value, quality in values:
        s = Sample(tag=PATH, value=value, timestamp=T0 + timedelta(seconds=seconds),
                   quality=quality)
        if f.should_store(s, t, heartbeat):
            kept.append(value)
    return kept


G, B = Quality.GOOD, Quality.BAD


def test_unchanged_values_are_not_stored() -> None:
    assert run(tag(), [(0, 230.0, G), (1, 230.0, G), (2, 230.0, G), (3, 231.0, G)]) == [
        230.0, 231.0]


def test_deadband() -> None:
    values = [(0, 230.0, G), (1, 230.3, G), (2, 230.6, G), (3, 230.4, G), (4, 229.9, G)]
    assert run(tag(history_deadband=0.5), values) == [230.0, 230.6, 229.9]


def test_quality_change_is_always_stored() -> None:
    values = [(0, 230.0, G), (1, None, B), (2, None, B), (3, 230.0, G)]
    assert run(tag(history_deadband=10), values) == [230.0, None, 230.0]


def test_heartbeat() -> None:
    values = [(0, 1.0, G), (100, 1.0, G), (301, 1.0, G), (400, 1.0, G), (602, 1.0, G)]
    assert run(tag(), values, heartbeat=300) == [1.0, 1.0, 1.0]


def test_history_false_never_stores() -> None:
    assert run(tag(history=False), [(0, 1.0, G), (1, 2.0, G)]) == []


def test_bool_changes() -> None:
    t = ModbusTag.model_validate({"name": "voltage", "data_type": "bool",
                                  "register_type": "coil", "address": 0})
    assert run(t, [(0, True, G), (1, True, G), (2, False, G), (3, False, G)]) == [True, False]


def test_late_samples_are_kept() -> None:
    assert run(tag(), [(10, 1.0, G), (5, 1.0, G)]) == [1.0, 1.0]
