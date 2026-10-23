"""Which samples go into the database (the live cache and WebSocket still get every sample).

A sample is stored when, compared with the last *stored* sample of the tag:

* it is the first one, or the quality changed (good -> bad is always recorded), or
* the value changed: numeric tags by more than ``history_deadband`` (any change when unset),
  bool/string tags on any change, or
* ``history_heartbeat_s`` of the device has passed (so charts and "last seen" stay honest).

Tags with ``history: false`` are never stored. A meter polled every second whose voltage moves
by ±0.1 V with ``history_deadband: 0.5`` then stores a few rows per minute instead of 60.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hub.models import NUMERIC_TYPES, Quality, Sample, TagBase


@dataclass
class _Stored:
    value: object
    quality: Quality
    ts: datetime


class HistoryFilter:
    def __init__(self) -> None:
        self._last: dict[str, _Stored] = {}
        self.passed = 0
        self.filtered = 0

    def should_store(self, sample: Sample, tag: TagBase, heartbeat_s: int) -> bool:
        keep = self._decide(sample, tag, heartbeat_s)
        if keep:
            self._last[sample.tag] = _Stored(sample.value, sample.quality, sample.timestamp)
            self.passed += 1
        else:
            self.filtered += 1
        return keep

    def _decide(self, sample: Sample, tag: TagBase, heartbeat_s: int) -> bool:
        if not tag.history:
            return False
        last = self._last.get(sample.tag)
        if last is None or sample.quality != last.quality:
            return True
        if sample.timestamp < last.ts:
            return True  # late (device-timestamped) data: keep it, the store ignores duplicates
        if sample.timestamp - last.ts >= timedelta(seconds=heartbeat_s):
            return True
        if sample.value is None or last.value is None:
            return sample.value is not last.value
        if tag.data_type in NUMERIC_TYPES and not isinstance(sample.value, bool | str):
            deadband = tag.history_deadband
            if deadband is None:
                return sample.value != last.value
            return abs(sample.value - last.value) > deadband  # type: ignore[operator]
        return sample.value != last.value
