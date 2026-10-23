from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from hub.drivers.base import DeviceState, WriteGrant
from hub.drivers.mqtt_json import MqttJsonDriver, extract, parse_status, topic_matches
from hub.models import Broker, MqttDevice, Quality
from simulators.mqtt_sim import EmbeddedBroker, SimulatedMqttDevice
from tests.drivers.helpers import Recorder, device

pytestmark = pytest.mark.integration


def sensor(**conn: Any) -> MqttDevice:
    return device({
        "device_id": "env_sensor_02", "name": "Gate sensor", "site": "kish", "area": "gate",
        "protocol": "mqtt",
        "connection": {"broker_ref": "local", "telemetry_topic": "kish/gate/th-01/data",
                       "status_topic": "kish/gate/th-01/status",
                       "command_topic": "kish/gate/th-01/cmd",
                       "timestamp_path": "$.ts", "timestamp_format": "unix_s"} | conn,
        "tags": [
            {"name": "temperature", "unit": "degC", "data_type": "float32",
             "json_path": "$.data.t", "min": -20, "max": 60},
            {"name": "humidity", "unit": "%", "data_type": "float32", "json_path": "$.data.h"},
            {"name": "battery", "unit": "%", "data_type": "uint16", "json_path": "$.bat"},
            {"name": "fan_on", "data_type": "bool", "json_path": "$['fan state']",
             "access": "read_write"},
        ],
    })


# --- pure helpers ---------------------------------------------------------------------------


def test_extract() -> None:
    doc = {"data": {"t": 1.5}, "list": [{"v": 2}], "a b": 3}
    assert extract(doc, "$.data.t") == 1.5
    assert extract(doc, "$.list[0].v") == 2
    assert extract(doc, "$['a b']") == 3
    assert extract(doc, "$.data.x") is not None and extract(doc, "$.data.x") != 1.5
    assert extract(doc, "$.list[3]") is extract(doc, "$.missing")


@pytest.mark.parametrize(
    "flt,topic,expected",
    [("a/+/c", "a/b/c", True), ("a/#", "a/b/c", True), ("a/b", "a/b/c", False),
     ("a/+", "a", False), ("#", "x", True)],
)
def test_topic_matches(flt: str, topic: str, expected: bool) -> None:
    assert topic_matches(flt, topic) is expected


@pytest.mark.parametrize(
    "payload,expected",
    [(b"online", True), (b"OFFLINE", False), (b'{"status": "online"}', True),
     (b'{"state": "lost"}', False), (b"true", True), (b"maybe", None)],
)
def test_parse_status(payload: bytes, expected: bool | None) -> None:
    assert parse_status(payload) is expected


# --- telemetry mapping (no broker needed) ---------------------------------------------------


async def test_telemetry_mapping_and_device_timestamp() -> None:
    rec = Recorder()
    driver = MqttJsonDriver(sensor(), rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host="x"))
    await driver.handle_telemetry(json.dumps(
        {"ts": 1767225600, "data": {"t": 24.6, "h": 51}, "bat": 87, "fan state": True}).encode())
    latest = rec.latest()
    assert latest["temperature"].value == pytest.approx(24.6)
    assert latest["humidity"].value == 51.0 and isinstance(latest["humidity"].value, float)
    assert latest["battery"].value == 87
    assert latest["fan_on"].value is True
    assert latest["temperature"].timestamp == datetime(2026, 1, 1, tzinfo=UTC)
    assert rec.state is DeviceState.ONLINE


async def test_partial_message_and_bad_types() -> None:
    rec = Recorder()
    driver = MqttJsonDriver(sensor(), rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host="x"))
    await driver.handle_telemetry(b'{"ts": 1767225600, "data": {"t": "hot"}, "bat": 90}')
    latest = rec.latest()
    assert set(latest) == {"temperature", "battery"}         # humidity absent: skipped
    assert latest["temperature"].quality is Quality.BAD       # wrong type
    assert latest["battery"].value == 90


async def test_bad_timestamp_falls_back_to_receive_time() -> None:
    rec = Recorder()
    fixed = datetime(2026, 10, 1, 12, tzinfo=UTC)
    driver = MqttJsonDriver(sensor(timestamp_format="iso8601"), rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host="x"), clock=lambda: fixed)
    await driver.handle_telemetry(b'{"ts": "2026-01-01T00:00:00", "data": {"t": 20}}')
    sample = rec.latest()["temperature"]
    assert sample.timestamp == fixed and sample.quality is Quality.UNCERTAIN


async def test_invalid_json_is_error() -> None:
    rec = Recorder()
    driver = MqttJsonDriver(sensor(), rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host="x"))
    await driver.handle_telemetry(b"not json")
    assert rec.samples == [] and rec.state is DeviceState.ERROR


# --- with a real broker ---------------------------------------------------------------------


@pytest.fixture
async def broker() -> Any:
    async with EmbeddedBroker() as b:
        yield b


async def test_end_to_end_with_broker(broker: EmbeddedBroker) -> None:
    dev = sensor()
    rec = Recorder()
    driver = MqttJsonDriver(dev, rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host=broker.host, port=broker.port))
    driver._backoff.initial_s = driver._backoff.max_s = 0.2
    sim = SimulatedMqttDevice(dev, broker.host, broker.port)
    await driver.start()
    try:
        await rec.wait_for(lambda: driver._client is not None)
        await asyncio.sleep(0.2)  # let the subscription settle
        await sim.connect()
        sim.values.update(temperature=21.5, battery=77)
        await sim.publish()
        await rec.wait_for(lambda: "temperature" in rec.latest())
        assert rec.latest()["temperature"].value == pytest.approx(21.5)
        assert rec.latest()["battery"].value == 77
        assert rec.state is DeviceState.ONLINE

        # command + read-back: the simulator applies it and publishes again
        write = asyncio.create_task(driver.read_back("fan_on", timeout_s=5))
        await asyncio.sleep(0.05)
        await driver.write_tag("fan_on", WriteGrant("r1", dev.tag_path("fan_on"), True))
        assert await write is True
        assert sim.commands == [{"fan_on": True}]

        # graceful device shutdown -> status topic says offline
        await sim.disconnect()
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        assert rec.latest()["temperature"].quality is Quality.BAD
    finally:
        await sim.disconnect()
        await driver.stop()


async def test_broker_down_is_offline_and_reconnects() -> None:
    port_holder = EmbeddedBroker()
    dev = sensor()
    rec = Recorder()
    driver = MqttJsonDriver(dev, rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host="127.0.0.1",
                                          port=port_holder.port))
    driver._backoff.initial_s = driver._backoff.max_s = 0.2
    await driver.start()
    try:
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE)
        await port_holder.start()
        sim = SimulatedMqttDevice(dev, port_holder.host, port_holder.port)
        await rec.wait_for(lambda: driver._client is not None, timeout=15)
        await asyncio.sleep(0.2)
        await sim.connect()
        await sim.publish()
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE, timeout=15)
        await sim.disconnect()
    finally:
        await driver.stop()
        await port_holder.stop()


async def test_silent_device_goes_stale_then_recovers(broker: EmbeddedBroker) -> None:
    dev = sensor(status_topic=None, stale_after_s=0.6)
    rec = Recorder()
    driver = MqttJsonDriver(dev, rec.on_samples, rec.on_status,
                            broker=Broker(broker_id="local", host=broker.host, port=broker.port))
    sim = SimulatedMqttDevice(dev, broker.host, broker.port)
    await driver.start()
    try:
        await rec.wait_for(lambda: driver._client is not None)
        await asyncio.sleep(0.2)
        await sim.connect()
        await sim.publish()
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        # the device stops talking without an LWT
        await rec.wait_for(lambda: rec.state is DeviceState.OFFLINE, timeout=5)
        assert "no telemetry" in (rec.statuses[-1].last_error or "")
        assert rec.latest()["temperature"].quality is Quality.BAD
        await sim.publish()
        await rec.wait_for(lambda: rec.state is DeviceState.ONLINE)
        assert rec.latest()["temperature"].quality is Quality.GOOD
    finally:
        await sim.disconnect()
        await driver.stop()
