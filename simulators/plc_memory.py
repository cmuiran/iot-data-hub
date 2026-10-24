"""Word memory of a simulated PLC (Omron FINS, MELSEC MC) and the device that animates it.

Areas are sparse dicts word -> 16-bit value; bit devices (MELSEC X/Y/M/...) hold 0/1 per point.
:class:`SimulatedWordDevice` stores a device's tag values there exactly as the hub's drivers
read them, and moves read-only numbers like the other simulators do.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from hub.codec import Value, to_raw
from hub.drivers.word_plc import value_to_words
from hub.models import Access, DataType, Order, TagBase, WordAddress


class WordMemory:
    def __init__(self) -> None:
        self.areas: dict[str, dict[int, int]] = {}
        self.writes: list[tuple[str, int, list[int]]] = []

    def read(self, area: str, start: int, count: int) -> list[int]:
        mem = self.areas.get(area, {})
        return [mem.get(start + i, 0) for i in range(count)]

    def write(self, area: str, start: int, values: list[int], *, record: bool = True) -> None:
        mem = self.areas.setdefault(area, {})
        for i, v in enumerate(values):
            mem[start + i] = v & 0xFFFF
        if record:
            self.writes.append((area, start, list(values)))

    def set_bit(self, area: str, word: int, bit: int, value: bool, *, record: bool = True) -> None:
        mem = self.areas.setdefault(area, {})
        old = mem.get(word, 0)
        mem[word] = (old | (1 << bit)) if value else (old & ~(1 << bit))
        if record:
            self.writes.append((area, word, [int(value)]))


def _initial(tag: TagBase) -> Value:
    if tag.data_type is DataType.BOOL:
        return False
    return animated(tag, 0.0)


def animated(tag: TagBase, t: float) -> float:
    lo = tag.min if tag.min is not None else 0.0
    hi = tag.max if tag.max is not None else 100.0
    if tag.data_type in (DataType.UINT32, DataType.UINT64, DataType.INT64) and tag.min is None:
        return round(1000 + t * 10)                       # counters count up
    value = (lo + hi) / 2 + (hi - lo) * 0.3 * math.sin(t / 10 + len(tag.name))
    if tag.data_type not in (DataType.FLOAT32, DataType.FLOAT64):
        value = round(value / tag.scale) * tag.scale
    return value


class SimulatedWordDevice:
    """``locate(tag)`` gives (area, word, bit or None, bit-device?) of a tag."""

    def __init__(self, memory: WordMemory, device: Any, word_order: Order,
                 locate: Callable[[Any], tuple[WordAddress, bool]]) -> None:
        self.memory = memory
        self.device = device
        self.word_order = word_order
        self._locate = locate
        self._tags = {t.name: t for t in device.tags}
        self._t = 0.0
        for tag in device.tags:
            self.set(tag.name, _initial(tag))

    def set(self, name: str, value: Value) -> None:
        tag = self._tags[name]
        a, bit_device = self._locate(tag)
        if tag.data_type is DataType.BOOL:
            if bit_device:
                self.memory.write(a.area, a.word, [1 if value else 0], record=False)
            else:
                self.memory.set_bit(a.area, a.word, a.bit or 0, bool(value), record=False)
            return
        words = value_to_words(tag, to_raw(tag, value), self.word_order)
        self.memory.write(a.area, a.word, words, record=False)

    def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for tag in self.device.tags:
            if tag.access is Access.READ_WRITE or tag.data_type is DataType.BOOL:
                continue                                    # setpoints keep what was written
            self.set(tag.name, animated(tag, self._t))
