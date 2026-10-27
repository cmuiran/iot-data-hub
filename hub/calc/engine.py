"""Runtime of calculated tags.

``compile_calcs`` checks every calculated tag of the configuration (formula, units, references,
dependency cycles) and orders them so that a calculation runs after the calculations it reads.
``CalcEngine`` keeps the latest input values (fed by the hub with every sample), and evaluates a
calculation when one of its inputs changed (at most every ``min_interval_s``) and at least every
``period_s`` (so stale inputs and running totals are noticed without new data). Results are
ordinary samples: history, alarms, dashboards, reports and outputs treat them like any tag.

Quality of a result: bad when it cannot be computed (an input missing / bad / older than
``stale_s``: reason ``input_bad`` / ``stale``; division by zero: ``math_error``), uncertain when an
input is uncertain or an aggregate left a missing input out (``missing: skip``), else good.

Versions: every definition has a hash (:func:`definition_version`); the store records when each
version came into force, so every stored result can be traced to the formula that produced it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hub.building import BuildingModel
from hub.calc.expr import (
    MISSING,
    CalcError,
    CalcMathError,
    Compiled,
    Context,
    Output,
    compile_formula,
    output_for,
)
from hub.models import CalcDevice, CalcTag, Quality, QualityReason, Sample, TagBase

log = logging.getLogger(__name__)

SampleSink = Callable[[list[Sample]], Awaitable[None]]
Clock = Callable[[], datetime]


def definition_version(tag: CalcTag) -> str:
    """Hash of everything that changes the result (not the description)."""
    data = tag.model_dump(mode="json", include={
        "formula", "inputs", "unit", "data_type", "missing", "stale_s", "max_gap_s"})
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class CalcDef:
    path: str
    device_id: str
    tag: CalcTag
    compiled: Compiled
    output: Output
    version: str

    @property
    def refs(self) -> set[str]:
        return self.compiled.refs


@dataclass
class CalcIssue:
    source: str                       # device_id of the calc device
    location: str                     # tags[i] (name)
    message: str
    error: bool = True


def compile_calcs(devices: Iterable[CalcDevice], catalogue: Mapping[str, TagBase],
                  building: BuildingModel | None = None
                  ) -> tuple[dict[str, CalcDef], list[str], list[CalcIssue]]:
    """(definitions by tag path, evaluation order, issues)."""
    defs: dict[str, CalcDef] = {}
    where: dict[str, str] = {}
    issues: list[CalcIssue] = []
    for device in devices:
        for i, tag in enumerate(device.tags):
            path = device.tag_path(tag.name)
            where[path] = f"tags[{i}] ({tag.name})"
            try:
                compiled = compile_formula(tag.formula, inputs=tag.inputs, catalogue=catalogue,
                                           building=building, skip_missing=tag.missing == "skip")
                output = output_for(compiled, tag)
            except CalcError as exc:
                issues.append(CalcIssue(device.device_id, where[path], f"formula: {exc}"))
                continue
            if path in compiled.refs:
                issues.append(CalcIssue(device.device_id, where[path],
                                        "formula reads its own result"))
                continue
            issues.extend(CalcIssue(device.device_id, where[path], w, error=False)
                          for w in compiled.warnings)
            defs[path] = CalcDef(path, device.device_id, tag, compiled, output,
                                 definition_version(tag))
    order, cycles = _order(defs)
    for cycle in cycles:
        first = cycle[0]
        issues.append(CalcIssue(defs[first].device_id, where[first],
                                "circular dependency: " + " -> ".join(cycle)))
    in_cycle = {p for cycle in cycles for p in cycle}
    for path in in_cycle:
        defs.pop(path, None)
    return defs, [p for p in order if p in defs], issues


def _order(defs: dict[str, CalcDef]) -> tuple[list[str], list[list[str]]]:
    """Topological order (inputs first) and the dependency cycles found."""
    order: list[str] = []
    state: dict[str, int] = {}          # 1 = visiting, 2 = done
    cycles: list[list[str]] = []

    def visit(path: str, stack: list[str]) -> None:
        if state.get(path) == 2:
            return
        if state.get(path) == 1:
            cycle = stack[stack.index(path):] + [path]
            if not any(set(cycle) == set(c) for c in cycles):
                cycles.append(cycle)
            return
        state[path] = 1
        for ref in sorted(defs[path].refs):
            if ref in defs:
                visit(ref, [*stack, path])
        state[path] = 2
        order.append(path)

    for path in sorted(defs):
        visit(path, [])
    return order, cycles


# --------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------


class ValuesContext(Context):
    """Reads inputs from a dict of samples (the engine's cache or the preview's snapshot)."""

    def __init__(self, values: Mapping[str, Sample], now: datetime, stale_s: float) -> None:
        super().__init__()
        self.values = values
        self.now = now
        self.stale_s = stale_s
        self.used: dict[str, Sample | None] = {}

    def raw(self, path: str) -> Any:
        s = self.values.get(path)
        self.used[path] = s
        if s is None or s.value is None or s.quality is Quality.BAD:
            self.reason = self.reason or QualityReason.INPUT_BAD
            return MISSING
        if (self.now - s.timestamp).total_seconds() > self.stale_s:
            self.reason = self.reason or QualityReason.STALE
            return MISSING
        if s.quality is Quality.UNCERTAIN:
            self.uncertain = True
        return s.value


@dataclass
class _State:
    last_eval: float | None = None    # loop time
    acc: float = 0.0                  # integrate/runtime total, SI
    prev: Any = None                  # previous integrand / condition (None = none yet)
    prev_ts: datetime | None = None


def evaluate_once(d: CalcDef, values: Mapping[str, Sample], now: datetime,
                  state: _State | None = None) -> tuple[Sample, ValuesContext]:
    """One evaluation; updates ``state`` for integrate/runtime."""
    ctx = ValuesContext(values, now, d.tag.stale_s)
    reason: QualityReason | None = None
    value: Any = None
    try:
        raw = d.compiled.fn(ctx)
        if d.compiled.stateful is not None:
            st = state if state is not None else _State()
            raw = _accumulate(d, st, raw, now, ctx)
        if raw is MISSING:
            reason = QualityReason(ctx.reason or QualityReason.INPUT_BAD)
        else:
            value = d.output.convert(raw)
    except (CalcMathError, ZeroDivisionError, OverflowError, ValueError) as exc:
        reason = QualityReason.MATH_ERROR
        log.debug("%s: %s", d.path, exc)
    if reason is not None:
        quality = Quality.BAD
    elif ctx.uncertain:
        quality = Quality.UNCERTAIN
    else:
        quality = Quality.GOOD
    return Sample(tag=d.path, value=value, timestamp=now, quality=quality, reason=reason,
                  received_at=now), ctx


def _accumulate(d: CalcDef, st: _State, current: Any, now: datetime, ctx: Context) -> Any:
    if st.prev is not None and st.prev_ts is not None:
        dt = (now - st.prev_ts).total_seconds()
        if 0 < dt <= d.tag.max_gap_s:
            if d.compiled.stateful == "integrate":
                st.acc += float(st.prev) * dt
            elif st.prev:
                st.acc += dt
        elif dt > d.tag.max_gap_s:
            ctx.uncertain = True              # a gap was not counted
    if current is MISSING:
        st.prev, st.prev_ts = None, None
        ctx.uncertain = True                  # total so far is right, but not advancing
    else:
        st.prev, st.prev_ts = current, now
    return st.acc


class CalcEngine:
    def __init__(self, defs: dict[str, CalcDef], order: list[str], emit: SampleSink,
                 clock: Clock, tick_s: float = 0.25) -> None:
        self.defs = defs
        self.order = order
        self._emit = emit
        self._clock = clock
        self.tick_s = tick_s
        self.values: dict[str, Sample] = {}
        self.states: dict[str, _State] = {p: _State() for p in defs}
        self.dependents: dict[str, list[str]] = {}
        for path in order:
            for ref in defs[path].refs:
                self.dependents.setdefault(ref, []).append(path)
        self.dirty: set[str] = set(defs)
        self.evaluations = 0
        self.errors = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- state across restarts and reloads -----------------------------------------------------

    def seed(self, latest: Iterable[Sample]) -> None:
        """Input values, and the running totals of integrate/runtime from their last result."""
        for s in latest:
            d = self.defs.get(s.tag)
            if d is None:
                self.values.setdefault(s.tag, s)
                continue
            self.values[s.tag] = s
            if d.compiled.stateful and isinstance(s.value, int | float) and \
                    not isinstance(s.value, bool) and d.output.unit is not None:
                self.states[s.tag].acc = d.output.unit.to_si(float(s.value), difference=True)

    def adopt(self, old: CalcEngine) -> None:
        """Keep inputs and the running totals of unchanged definitions (configuration reload)."""
        self.values.update({k: v for k, v in old.values.items()})
        for path, d in self.defs.items():
            before = old.defs.get(path)
            if before is not None and before.version == d.version:
                self.states[path] = old.states[path]

    # --- input -------------------------------------------------------------------------------

    def feed(self, samples: Iterable[Sample]) -> None:
        woke = False
        for s in samples:
            if s.tag in self.defs:            # own results are cached when computed
                continue
            self.values[s.tag] = s
            for dep in self.dependents.get(s.tag, ()):
                self.dirty.add(dep)
                woke = True
        if woke:
            self._wake.set()

    # --- loop --------------------------------------------------------------------------------

    async def start(self) -> None:
        if self.defs and self._task is None:
            self._task = asyncio.create_task(self._run(), name="hub:calc")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.tick_s)
            self._wake.clear()
            try:
                samples = self.evaluate_due(loop.time())
                if samples:
                    await self._emit(samples)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - calculations must never stop data collection
                self.errors += 1
                log.exception("calculation pass failed")

    def evaluate_due(self, now_loop: float) -> list[Sample]:
        now = self._clock()
        out: list[Sample] = []
        for path in self.order:
            d = self.defs[path]
            st = self.states[path]
            since = None if st.last_eval is None else now_loop - st.last_eval
            due = since is None or since >= d.tag.period_s or (
                path in self.dirty and since >= d.tag.min_interval_s)
            if not due:
                continue
            self.dirty.discard(path)
            st.last_eval = now_loop
            sample, _ = evaluate_once(d, self.values, now, st)
            self.evaluations += 1
            self.values[path] = sample
            for dep in self.dependents.get(path, ()):
                self.dirty.add(dep)
            out.append(sample)
        return out

    def current(self, path: str) -> Sample | None:
        return self.values.get(path)


@dataclass
class Preview:
    sample: Sample
    inputs: dict[str, Sample | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def preview(tag: CalcTag, path: str, catalogue: Mapping[str, TagBase],
            values: Mapping[str, Sample], now: datetime,
            building: BuildingModel | None = None) -> Preview:
    """Evaluate a definition once against the given values without publishing anything.
    integrate/runtime show 0 (they need two evaluations). Raises CalcError when invalid."""
    compiled = compile_formula(tag.formula, inputs=tag.inputs, catalogue=catalogue,
                               building=building, skip_missing=tag.missing == "skip")
    if path in compiled.refs:
        raise CalcError("formula reads its own result")
    d = CalcDef(path, path.split(".")[2], tag, compiled, output_for(compiled, tag),
                definition_version(tag))
    sample, ctx = evaluate_once(d, values, now)
    return Preview(sample, ctx.used, compiled.warnings)
