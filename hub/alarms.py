"""Alarm engine: limit alarms from config/alarms.yaml and automatic device-offline alarms.

Per source (a tag path, or ``device:<id>`` for offline) at most one alarm is open. Rules:

* numeric: ``hihi`` > ``hi`` and ``lolo`` < ``lo``; the most severe band wins (hihi before hi).
  An open alarm clears only when the value is back inside its limit by more than ``deadband``;
  moving to another band (hi -> hihi) closes the old alarm and raises the new one.
* bool: kind ``state`` while ``value == equals``.
* ``delay_s``: the condition must hold that long before the alarm is raised (checked on every
  sample and on :meth:`AlarmEngine.tick`).
* bad-quality samples neither raise nor clear (a dead sensor is the offline alarm's job).

The engine only decides; ``on_raise`` / ``on_clear`` persist and publish.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from hub.drivers.base import DeviceState, DeviceStatus
from hub.models import AlarmRule, AlarmsFile, AlarmSeverity, Quality, Sample

log = logging.getLogger(__name__)

_SEVERITY_UP = {AlarmSeverity.LOW: AlarmSeverity.MEDIUM, AlarmSeverity.MEDIUM: AlarmSeverity.HIGH,
                AlarmSeverity.HIGH: AlarmSeverity.CRITICAL,
                AlarmSeverity.CRITICAL: AlarmSeverity.CRITICAL}


@dataclass(frozen=True)
class AlarmRaise:
    source: str
    kind: str
    severity: AlarmSeverity
    message: str
    value: object
    ts: datetime
    tag_path: str | None = None
    device_id: str | None = None


RaiseSink = Callable[[AlarmRaise], Awaitable[None]]
ClearSink = Callable[[str, datetime], Awaitable[None]]


@dataclass
class _State:
    active: str | None = None          # kind of the open alarm
    candidate: str | None = None       # condition seen but not yet held for delay_s
    since: datetime | None = None
    value: object = None


def _kind_for(rule: AlarmRule, value: object, active: str | None) -> str | None:
    if rule.equals is not None:
        return "state" if value == rule.equals else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    db = rule.deadband
    # Keep the open alarm while inside the hysteresis band of its own limit.
    hold = {
        "hihi": rule.hihi is not None and value > rule.hihi - db,
        "hi": rule.hi is not None and value > rule.hi - db,
        "lo": rule.lo is not None and value < rule.lo + db,
        "lolo": rule.lolo is not None and value < rule.lolo + db,
    }
    if rule.hihi is not None and value >= rule.hihi:
        return "hihi"
    if active == "hihi" and hold["hihi"]:
        return "hihi"
    if rule.lolo is not None and value <= rule.lolo:
        return "lolo"
    if active == "lolo" and hold["lolo"]:
        return "lolo"
    if rule.hi is not None and value >= rule.hi:
        return "hi"
    if active in ("hi", "hihi") and hold["hi"]:
        return "hi"
    if rule.lo is not None and value <= rule.lo:
        return "lo"
    if active in ("lo", "lolo") and hold["lo"]:
        return "lo"
    return None


def _message(rule: AlarmRule, kind: str, value: object) -> str:
    if rule.message:
        return rule.message
    limit = {"hihi": rule.hihi, "hi": rule.hi, "lo": rule.lo, "lolo": rule.lolo}.get(kind)
    if kind == "state":
        return f"{rule.tag} is {value}"
    return f"{rule.tag} {kind} ({value} vs limit {limit})"


class AlarmEngine:
    def __init__(self, config: AlarmsFile, on_raise: RaiseSink, on_clear: ClearSink) -> None:
        self.config = config
        self._rules = {r.tag: r for r in config.rules}
        self._on_raise = on_raise
        self._on_clear = on_clear
        self._states: dict[str, _State] = {}
        self._offline_since: dict[str, datetime] = {}
        self._offline_error: dict[str, str | None] = {}
        self._offline_open: set[str] = set()

    def restore(self, open_sources: dict[str, str]) -> None:
        """Alarms still open in the database after a restart: {source: kind}."""
        for source, kind in open_sources.items():
            if source.startswith("device:"):
                self._offline_open.add(source.removeprefix("device:"))
            else:
                self._states.setdefault(source, _State()).active = kind

    # --- limit alarms -------------------------------------------------------------------------

    async def on_samples(self, samples: list[Sample]) -> None:
        for sample in samples:
            rule = self._rules.get(sample.tag)
            if rule is None or sample.quality is Quality.BAD or sample.value is None:
                continue
            await self._evaluate(rule, sample.value, sample.timestamp)

    async def _evaluate(self, rule: AlarmRule, value: object, now: datetime) -> None:
        state = self._states.setdefault(rule.tag, _State())
        state.value = value
        kind = _kind_for(rule, value, state.active)
        if kind == state.active:
            state.candidate, state.since = None, None
            return
        if kind is None:
            await self._close(rule.tag, state, now)
            state.candidate, state.since = None, None
            return
        if state.candidate != kind:
            state.candidate, state.since = kind, now
        if now - (state.since or now) >= timedelta(seconds=rule.delay_s):
            await self._close(rule.tag, state, now)
            severity = rule.severity
            if kind in ("hihi", "lolo"):
                severity = _SEVERITY_UP[severity]
            await self._on_raise(AlarmRaise(source=rule.tag, kind=kind, severity=severity,
                                            message=_message(rule, kind, value), value=value,
                                            ts=now, tag_path=rule.tag))
            state.active, state.candidate, state.since = kind, None, None

    async def _close(self, source: str, state: _State, now: datetime) -> None:
        if state.active is not None:
            await self._on_clear(source, now)
            state.active = None

    # --- offline alarms -----------------------------------------------------------------------

    async def on_status(self, status: DeviceStatus, now: datetime) -> None:
        if not self.config.offline_alarm:
            return
        device = status.device_id
        if status.state is DeviceState.OFFLINE:
            self._offline_since.setdefault(device, now)
            self._offline_error[device] = status.last_error
            await self._check_offline(device, now)
        else:
            self._offline_since.pop(device, None)
            self._offline_error.pop(device, None)
            if device in self._offline_open and status.state is DeviceState.ONLINE:
                self._offline_open.discard(device)
                await self._on_clear(f"device:{device}", now)

    async def _check_offline(self, device: str, now: datetime) -> None:
        since = self._offline_since.get(device)
        error = self._offline_error.get(device)
        if since is None or device in self._offline_open:
            return
        if now - since >= timedelta(seconds=self.config.offline_delay_s):
            self._offline_open.add(device)
            await self._on_raise(AlarmRaise(
                source=f"device:{device}", kind="offline", severity=self.config.offline_severity,
                message=f"{device} offline" + (f": {error}" if error else ""), value=None,
                ts=now, device_id=device))

    async def tick(self, now: datetime) -> None:
        """Re-check delayed conditions without waiting for the next sample."""
        for device in list(self._offline_since):
            await self._check_offline(device, now)
        for source, state in list(self._states.items()):
            if state.candidate is not None and state.since is not None:
                await self._evaluate(self._rules[source], state.value, now)
