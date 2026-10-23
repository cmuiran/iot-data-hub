"""Shared helpers for driver tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from hub.drivers.base import DeviceState, DeviceStatus
from hub.models import DEVICE_ADAPTER, Sample


class Recorder:
    """Collects what a driver emits."""

    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self.statuses: list[DeviceStatus] = []
        self._changed = asyncio.Event()

    async def on_samples(self, samples: list[Sample]) -> None:
        self.samples.extend(samples)
        self._changed.set()

    async def on_status(self, status: DeviceStatus) -> None:
        self.statuses.append(status)
        self._changed.set()

    def latest(self) -> dict[str, Sample]:
        out: dict[str, Sample] = {}
        for s in self.samples:
            out[s.tag.rsplit(".", 1)[1]] = s
        return out

    @property
    def state(self) -> DeviceState | None:
        return self.statuses[-1].state if self.statuses else None

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        async def _wait() -> None:
            while not predicate():
                self._changed.clear()
                try:  # wake on new samples/status, and re-check now and then for other state
                    await asyncio.wait_for(self._changed.wait(), 0.05)
                except TimeoutError:
                    pass

        await asyncio.wait_for(_wait(), timeout)


def device(data: dict[str, Any]) -> Any:
    return DEVICE_ADAPTER.validate_python(data)
