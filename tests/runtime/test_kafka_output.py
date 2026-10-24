"""Kafka output: message format, store & forward through an outage, partial batch failures and
the producer settings. A fake producer stands in for Kafka (no broker in CI); the settings are
checked against aiokafka's real producer class."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from backend.northbound import KafkaPublisher
from hub.models import KafkaTarget
from hub.runtime import LiveBus
from tests.runtime.test_hub import wait_until

TAG = "kish.gate.meter_02.voltage_l1"


class FakeKafka:
    """Records what reaches "Kafka"; ``down`` refuses connections, ``fail_at`` fails one send."""

    def __init__(self) -> None:
        self.down = False
        self.fail_at: int | None = None
        self.received: list[tuple[str, str, dict[str, Any]]] = []
        self.sends = 0

    def factory(self) -> Any:
        kafka = self

        class Producer:
            async def start(self) -> None:
                if kafka.down:
                    raise ConnectionError("KafkaConnectionError: unable to bootstrap")

            async def send(self, topic: str, value: bytes, key: bytes) -> asyncio.Future[None]:
                kafka.sends += 1
                fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                if kafka.down or kafka.sends == kafka.fail_at:
                    fut.set_exception(ConnectionError("NotLeaderForPartition"))
                else:
                    kafka.received.append((topic, key.decode(), json.loads(value)))
                    fut.set_result(None)
                return fut

            async def stop(self) -> None: ...

        return Producer()


def target(**extra: Any) -> KafkaTarget:
    return KafkaTarget.model_validate({
        "name": "datalake", "bootstrap_servers": ["kafka1.kish.local:9093"],
        "topic": "gate.samples", "username_env": "HUB_KAFKA_USER",
        "password_env": "HUB_KAFKA_PASSWORD", "heartbeat_s": 3600} | extra)


def services(tmp_path: Path) -> Any:
    unit = SimpleNamespace(unit="V")
    return SimpleNamespace(hub=SimpleNamespace(bus=LiveBus(), latest={}, tags={TAG: (None, unit)}),
                           settings=SimpleNamespace(node_id="node_a",
                                                    spool_dir=tmp_path / "spool"))


def sample(i: int) -> dict[str, Any]:
    return {"tag": TAG, "value": float(i),
            "ts": datetime(2026, 10, 24, 8, 0, i % 60, tzinfo=UTC).isoformat(), "quality": "good"}


async def test_messages_have_the_tag_as_key(tmp_path: Path) -> None:
    kafka = FakeKafka()
    pub = KafkaPublisher(services(tmp_path), target(), producer_factory=kafka.factory)
    await pub.start()
    try:
        pub.offer(sample(1))
        await wait_until(lambda: len(kafka.received) == 1)
        topic, key, value = kafka.received[0]
        assert (topic, key) == ("gate.samples", TAG)
        assert value == {"tag": TAG, "v": 1.0, "ts": "2026-10-24T08:00:01Z", "q": "good",
                         "u": "V", "node": "node_a"}
        assert pub.describe()["connected"] is True and pub.describe()["type"] == "kafka"
    finally:
        await pub.stop()
        pub.spool.close()                                       # type: ignore[union-attr]


async def test_outage_keeps_everything_in_order(tmp_path: Path) -> None:
    kafka = FakeKafka()
    kafka.down = True
    pub = KafkaPublisher(services(tmp_path), target(queue_max=20),
                         producer_factory=kafka.factory)
    await pub.start()
    try:
        for i in range(80):
            pub.offer(sample(i))
            await asyncio.sleep(0)
        await wait_until(lambda: pub.spool.count >= 60, timeout=10)  # type: ignore[union-attr]
        kafka.down = False
        await wait_until(lambda: len(kafka.received) == 80, timeout=20)
        assert [v["v"] for _, _, v in kafka.received] == [float(i) for i in range(80)]
        assert pub.stats["dropped"] == 0 and pub.spool.count == 0  # type: ignore[union-attr]
    finally:
        await pub.stop()
        pub.spool.close()                                       # type: ignore[union-attr]


async def test_a_failed_message_resends_its_batch(tmp_path: Path) -> None:
    kafka = FakeKafka()
    kafka.fail_at = 3                                           # the 3rd send fails once
    pub = KafkaPublisher(services(tmp_path), target(), producer_factory=kafka.factory)
    await pub.start()
    try:
        for i in range(5):
            pub.offer(sample(i))
        await wait_until(lambda: [v["v"] for _, _, v in kafka.received][-5:]
                         == [0.0, 1.0, 2.0, 3.0, 4.0], timeout=20)
        values = [v["v"] for _, _, v in kafka.received]
        assert set(values) == {0.0, 1.0, 2.0, 3.0, 4.0}         # at least once: none missing
        assert pub.stats["reconnects"] == 1
    finally:
        await pub.stop()
        pub.spool.close()                                       # type: ignore[union-attr]


async def test_producer_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aiokafka

    seen: dict[str, Any] = {}

    class Capture:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", Capture)
    pub = KafkaPublisher(services(tmp_path), target())
    with pytest.raises(RuntimeError, match="HUB_KAFKA_USER"):
        pub._producer()                                         # noqa: SLF001
    monkeypatch.setenv("HUB_KAFKA_USER", "hub")
    monkeypatch.setenv("HUB_KAFKA_PASSWORD", "secret-from-env")
    pub._producer()                                             # noqa: SLF001
    assert seen["security_protocol"] == "SASL_SSL" and seen["sasl_mechanism"] == "SCRAM-SHA-512"
    assert seen["acks"] == "all" and seen["enable_idempotence"] is True
    assert seen["ssl_context"] is not None and seen["compression_type"] == "gzip"
    assert seen["client_id"] == "hub-node_a-datalake"
    pub.spool.close()                                           # type: ignore[union-attr]


def test_config_rules() -> None:
    with pytest.raises(ValidationError, match="unencrypted"):
        target(security_protocol="PLAINTEXT")
    assert target(security_protocol="PLAINTEXT", allow_plaintext=True).allow_plaintext
    with pytest.raises(ValidationError, match="username_env"):
        KafkaTarget.model_validate({"name": "x", "bootstrap_servers": ["k:9093"]})
    assert KafkaTarget.model_validate({"name": "x", "bootstrap_servers": ["k:9093"],
                                       "security_protocol": "SSL"}).security_protocol == "SSL"
    with pytest.raises(ValidationError, match="host:port"):
        target(bootstrap_servers=["kafka1"])
