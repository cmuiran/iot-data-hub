from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hub.alarms import AlarmEngine, AlarmRaise
from hub.drivers.base import DeviceState, DeviceStatus
from hub.models import AlarmsFile, Quality, Sample

T0 = datetime(2026, 10, 1, tzinfo=UTC)
TEMP = "kish.warehouse.env_sensor_01.temperature"
DOOR = "kish.warehouse.env_sensor_01.door_open"


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []  # (op, source, kind)
        self.raised: list[AlarmRaise] = []

    async def raise_(self, alarm: AlarmRaise) -> None:
        self.events.append(("raise", alarm.source, alarm.kind))
        self.raised.append(alarm)

    async def clear(self, source: str, ts: datetime) -> None:
        self.events.append(("clear", source, None))


def engine(**config: Any) -> tuple[AlarmEngine, Log]:
    log = Log()
    cfg = AlarmsFile.model_validate({"rules": [
        {"tag": TEMP, "lo": 0, "hi": 35, "hihi": 45, "deadband": 1, "severity": "medium"},
        {"tag": DOOR, "equals": True, "severity": "low", "message": "Door open"},
    ]} | config)
    return AlarmEngine(cfg, log.raise_, log.clear), log


async def feed(e: AlarmEngine, tag: str, value: Any, seconds: float = 0,
               quality: Quality = Quality.GOOD) -> None:
    await e.on_samples([Sample(tag=tag, value=value, timestamp=T0 + timedelta(seconds=seconds),
                               quality=quality)])


async def test_hi_raise_deadband_and_clear() -> None:
    e, log = engine()
    await feed(e, TEMP, 30)
    await feed(e, TEMP, 36, 1)
    await feed(e, TEMP, 34.5, 2)          # inside the deadband (35 - 1): still active
    await feed(e, TEMP, 33.9, 3)          # below 34: clears
    assert log.events == [("raise", TEMP, "hi"), ("clear", TEMP, None)]
    assert log.raised[0].severity == "medium"


async def test_escalation_to_hihi_raises_severity() -> None:
    e, log = engine()
    await feed(e, TEMP, 36)
    await feed(e, TEMP, 46, 1)
    await feed(e, TEMP, 44.5, 2)          # within hihi deadband
    await feed(e, TEMP, 40, 3)            # back to hi band
    assert log.events == [("raise", TEMP, "hi"), ("clear", TEMP, None), ("raise", TEMP, "hihi"),
                          ("clear", TEMP, None), ("raise", TEMP, "hi")]
    assert log.raised[1].severity == "high"     # one step above the rule's medium


async def test_lo_alarm() -> None:
    e, log = engine()
    await feed(e, TEMP, -2)
    await feed(e, TEMP, 0.5, 1)           # within deadband of lo=0
    await feed(e, TEMP, 1.5, 2)
    assert log.events == [("raise", TEMP, "lo"), ("clear", TEMP, None)]


async def test_delay_needs_condition_to_persist() -> None:
    e, log = engine(rules=[{"tag": TEMP, "hi": 35, "delay_s": 60}])
    await feed(e, TEMP, 36, 0)
    await feed(e, TEMP, 36, 30)
    assert log.events == []
    await feed(e, TEMP, 30, 40)           # dropped before the delay: never raised
    await feed(e, TEMP, 36, 50)
    await e.tick(T0 + timedelta(seconds=111))
    assert log.events == [("raise", TEMP, "hi")]


async def test_bool_state_alarm_and_bad_quality_ignored() -> None:
    e, log = engine()
    await feed(e, DOOR, True)
    await feed(e, DOOR, None, 1, Quality.BAD)   # ignored: neither clears nor raises
    await feed(e, DOOR, False, 2)
    assert log.events == [("raise", DOOR, "state"), ("clear", DOOR, None)]
    assert log.raised[0].message == "Door open"


async def test_offline_alarm_after_delay_and_clear_on_online() -> None:
    e, log = engine(offline_delay_s=30)
    off = DeviceStatus("meter_01", DeviceState.OFFLINE, last_error="timeout")
    await e.on_status(off, T0)
    await e.tick(T0 + timedelta(seconds=10))
    assert log.events == []
    await e.tick(T0 + timedelta(seconds=31))
    assert log.events == [("raise", "device:meter_01", "offline")]
    assert "timeout" in log.raised[0].message and log.raised[0].severity == "high"
    await e.on_status(DeviceStatus("meter_01", DeviceState.ONLINE), T0 + timedelta(seconds=40))
    assert log.events[-1] == ("clear", "device:meter_01", None)


async def test_short_outage_raises_nothing() -> None:
    e, log = engine(offline_delay_s=30)
    await e.on_status(DeviceStatus("meter_01", DeviceState.OFFLINE), T0)
    await e.on_status(DeviceStatus("meter_01", DeviceState.ONLINE), T0 + timedelta(seconds=5))
    await e.tick(T0 + timedelta(seconds=60))
    assert log.events == []


async def test_restore_open_alarms_avoids_duplicates() -> None:
    e, log = engine()
    e.restore({TEMP: "hi", "device:meter_01": "offline"})
    await feed(e, TEMP, 36)                  # already open: no second raise
    await feed(e, TEMP, 20, 1)
    await e.on_status(DeviceStatus("meter_01", DeviceState.ONLINE), T0)
    assert log.events == [("clear", TEMP, None), ("clear", "device:meter_01", None)]


async def test_offline_alarm_can_be_disabled() -> None:
    e, log = engine(offline_alarm=False, offline_delay_s=0)
    await e.on_status(DeviceStatus("meter_01", DeviceState.OFFLINE), T0)
    await e.tick(T0 + timedelta(seconds=60))
    assert log.events == []
