"""Shared parts of the drivers for PLCs with addressed word memory (Omron FINS, MELSEC MC).

Tags are grouped into spans of neighbouring addresses in one area so a poll needs few requests
(like the S7 driver's blocks). A span that fails is retried tag by tag once, so one bad address
does not blank the whole device. Values are 16-bit words; 32/64-bit values span several words
(``word_order``); a bool is either a bit of a word or, for bit devices, one point.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from hub.codec import CodecError, Value, decode_registers, encode_registers
from hub.drivers.base import DeviceState, Driver, DriverError, sleep_until_next
from hub.models import DataType, Order, TagBase

log = logging.getLogger(__name__)
D = TypeVar("D")


class PlcError(DriverError):
    """The PLC answered with an error code (bad address, protected area): the connection is
    fine, only this request failed."""


@dataclass
class Span:
    area: str
    start: int
    count: int
    tags: list[Any] = field(default_factory=list)
    split: bool = False


def plan_spans(items: Iterable[tuple[str, int, int, Any]], max_count: int,
               max_gap: int) -> list[Span]:
    """Group (area, start, count, tag) into spans of at most ``max_count`` units."""
    groups: dict[str, list[tuple[int, int, Any]]] = {}
    for area, start, count, tag in items:
        groups.setdefault(area, []).append((start, count, tag))
    spans: list[Span] = []
    for area in sorted(groups):
        current: Span | None = None
        for start, count, tag in sorted(groups[area], key=lambda x: x[0]):
            end = start + count
            if current is not None:
                cur_end = current.start + current.count
                new_end = max(cur_end, end)
                if start <= cur_end + max_gap and new_end - current.start <= max_count:
                    current.count = new_end - current.start
                    current.tags.append(tag)
                    continue
                spans.append(current)
            current = Span(area, start, count, [tag])
        if current is not None:
            spans.append(current)
    return spans


def words_to_value(tag: TagBase, words: list[int], bit: int | None, word_order: Order) -> Value:
    """Raw value of a tag from its words (bool: the bit, or the point for bit devices)."""
    if tag.data_type is DataType.BOOL:
        if bit is None:
            return bool(words[0])
        return bool((words[0] >> bit) & 1)
    return decode_registers(words, tag.data_type, "big", word_order)


def value_to_words(tag: TagBase, raw: Value, word_order: Order) -> list[int]:
    return encode_registers(raw, tag.data_type, "big", word_order)


class WordPlcDriver(Driver[D], Generic[D]):
    """Poll loop over spans; subclasses talk the protocol."""

    def _setup_spans(self, max_count: int, max_gap: int) -> None:
        self._spans = plan_spans(((*self._location(t), t) for t in self.device.tags),  # type: ignore[attr-defined]
                                 max_count, max_gap)

    @abstractmethod
    def _location(self, tag: Any) -> tuple[str, int, int]:
        """(span key, start, count) of a tag."""

    @abstractmethod
    def _decode(self, tag: Any, units: list[int]) -> Value:
        """Raw value from the tag's own units of a span."""

    @abstractmethod
    async def _read_units(self, area: str, start: int, count: int) -> list[int]: ...

    async def _run_connected(self) -> None:
        period = self.device.poll_interval_ms / 1000  # type: ignore[attr-defined]
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.poll_once()
            await sleep_until_next(period, started, self._stopping)

    async def poll_once(self) -> None:
        samples = []
        failures = 0
        for span in self._spans:
            values: dict[str, Value | None] = {}
            try:
                values = await self._read_span(span)
            except PlcError as exc:
                failures += 1
                if not span.split and len(span.tags) > 1:
                    span.split = True
                    log.info("%s: reading %s%d.. tag by tag after: %s",
                             self.device.device_id, span.area, span.start, exc)  # type: ignore[attr-defined]
                    try:
                        values = await self._read_span(span)
                        failures -= 1
                    except PlcError as exc2:
                        exc = exc2
                if failures:
                    await self.mark_error(DeviceState.ERROR, f"{span.area}{span.start}: {exc}")
            for tag in span.tags:
                samples.append(self.make_sample(tag, values.get(tag.name)))
        await self.emit(samples)
        if failures == 0:
            await self.mark_seen()

    async def _read_span(self, span: Span) -> dict[str, Value | None]:
        out: dict[str, Value | None] = {}
        if span.split:
            for tag in span.tags:
                try:
                    out[tag.name] = await self._read_raw(tag)
                except (PlcError, CodecError) as exc:
                    log.debug("%s: %s: %s", self.device.device_id, tag.name, exc)  # type: ignore[attr-defined]
                    out[tag.name] = None
            if all(v is None for v in out.values()):
                raise PlcError(f"no tag of {span.area}{span.start}.. is readable")
            return out
        units = await self._read_units(span.area, span.start, span.count)
        for tag in span.tags:
            _, start, count = self._location(tag)
            try:
                out[tag.name] = self._decode(tag, units[start - span.start:
                                                       start - span.start + count])
            except CodecError as exc:
                log.warning("%s: %s: %s", self.device.device_id, tag.name, exc)  # type: ignore[attr-defined]
                out[tag.name] = None
        return out

    async def _read_raw(self, tag: TagBase) -> Value:
        area, start, count = self._location(tag)
        return self._decode(tag, await self._read_units(area, start, count))
