"""Scheduled and sequenced commands, executed through the control-safety layer.

A schedule has a cron expression (in its time zone) and a list of steps:

* ``{"write": {"tag": "...", "value": 1500}}`` — a write request, exactly as if the schedule's
  owner had sent it: RBAC, enablement, range/step/rate, interlocks and audit all apply, with the
  reason ``schedule <name>``. A tag whose policy needs a second person cannot be scheduled.
* ``{"delay_s": 30}`` — wait (max 1 h)
* ``{"wait": {"tag": "...", "op": "ge", "value": 1400, "timeout_s": 120}}`` — wait until a good,
  fresh value satisfies the condition; fail on timeout

A run stops at the first step that does not succeed (``failed``); every run is recorded in
``schedule_runs`` with the result of each step. The owner's permissions are checked at run time:
a disabled owner or a revoked role makes the run fail. Runs missed while the hub was down are
not caught up. Only the active node runs schedules (see backend/ha.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import operator
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, field_validator

from backend.cron import Cron, CronError
from backend.rbac import Principal
from hub.drivers.base import utcnow
from hub.models import Quality

log = logging.getLogger(__name__)
TICK_S = 5.0

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le,
    "gt": operator.gt, "ge": operator.ge,
}
Value = StrictBool | StrictInt | StrictFloat


class WriteSpec(BaseModel):
    tag: str = Field(min_length=7, max_length=263)
    value: Value


class WaitSpec(BaseModel):
    tag: str = Field(min_length=7, max_length=263)
    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    value: Value
    timeout_s: float = Field(default=60, gt=0, le=3600)


class WriteStep(BaseModel):
    write: WriteSpec


class DelayStep(BaseModel):
    delay_s: float = Field(gt=0, le=3600)


class WaitStep(BaseModel):
    wait: WaitSpec


Step = WriteStep | DelayStep | WaitStep


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    cron: str
    timezone: str = "Asia/Tehran"
    enabled: bool = True
    steps: Annotated[list[Step], Field(min_length=1, max_length=50)]

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        try:
            return Cron.parse(value).text
        except CronError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    def next_run(self, after: datetime) -> datetime | None:
        return Cron.parse(self.cron).next_after(after, self.timezone) if self.enabled else None


class Scheduler:
    def __init__(self, services: Any, clock: Callable[[], datetime] = utcnow) -> None:
        self.services = services
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._running: dict[int, asyncio.Task[None]] = {}

    async def start(self) -> None:
        # recompute next runs (missed runs are not caught up)
        now = self.clock()
        for s in await self.services.store.list_schedules():
            spec = ScheduleIn.model_validate(s)
            await self.services.store.set_schedule_times(s["schedule_id"], None,
                                                         spec.next_run(now))
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for task in list(self._running.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        now = self.clock()
        for s in await self.services.store.list_schedules():
            due = s["enabled"] and s["next_run_at"] is not None and s["next_run_at"] <= now
            if not due or s["schedule_id"] in self._running:
                continue
            spec = ScheduleIn.model_validate(s)
            await self.services.store.set_schedule_times(s["schedule_id"], now,
                                                         spec.next_run(now))
            self.launch(s, "timer")

    def launch(self, schedule: dict[str, Any], trigger: str) -> asyncio.Task[None]:
        task = asyncio.create_task(self.run(schedule, trigger),
                                   name=f"schedule:{schedule['schedule_id']}")
        self._running[schedule["schedule_id"]] = task
        task.add_done_callback(lambda _: self._running.pop(schedule["schedule_id"], None))
        return task

    async def _owner(self, owner_id: int) -> Principal | None:
        store = self.services.store
        user = await store.get_user(owner_id)
        if user is None or not user.is_active:
            return None
        return Principal(user=user, session_id=None, grants=tuple(await store.grants(owner_id)),
                         role_permissions=await store.role_permissions())

    async def run(self, schedule: dict[str, Any], trigger: str) -> dict[str, Any]:
        store = self.services.store
        run_id = await store.start_schedule_run(schedule["schedule_id"], trigger)
        spec = ScheduleIn.model_validate(schedule)
        detail: list[dict[str, Any]] = []
        status = "ok"
        owner = await self._owner(schedule["owner_id"])
        if owner is None:
            detail.append({"step": 0, "result": "failed", "message": "owner disabled or gone"})
            status = "failed"
        else:
            for i, step in enumerate(spec.steps, start=1):
                ok, info = await self._step(owner, spec, step)
                detail.append({"step": i, **info})
                if not ok:
                    status = "failed"
                    break
        await store.finish_schedule_run(run_id, status, detail)
        level = logging.INFO if status == "ok" else logging.WARNING
        log.log(level, "schedule %r (%s) %s", spec.name, trigger, status)
        self.services.hub.bus.publish({"type": "schedule", "schedule_id": schedule["schedule_id"],
                                       "name": spec.name, "status": status})
        return {"run_id": run_id, "status": status, "detail": detail}

    async def _step(self, owner: Principal, spec: ScheduleIn, step: Step
                    ) -> tuple[bool, dict[str, Any]]:
        if isinstance(step, DelayStep):
            await asyncio.sleep(step.delay_s)
            return True, {"delay_s": step.delay_s, "result": "ok"}
        if isinstance(step, WaitStep):
            return await self._wait(step.wait)
        w = step.write
        policy = self.services.control.control.policy_for(w.tag)
        if policy.confirm:
            return False, {"tag": w.tag, "result": "failed",
                           "message": "tag needs a second person; it cannot be scheduled"}
        try:
            result = await self.services.control.request_write(
                owner, w.tag, w.value, f"schedule {spec.name}", None)
        except Exception as exc:  # noqa: BLE001 - e.g. tag removed from config
            return False, {"tag": w.tag, "result": "failed", "message": str(exc)}
        return result.status == "applied", {"tag": w.tag, "value": w.value,
                                             "result": result.result, "message": result.message,
                                             "request_id": str(result.request_id)}

    async def _wait(self, w: WaitSpec) -> tuple[bool, dict[str, Any]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + w.timeout_s
        while True:
            sample = self.services.hub.latest.get(w.tag)
            if sample is not None and sample.quality is Quality.GOOD and sample.value is not None:
                with contextlib.suppress(TypeError):
                    if _OPS[w.op](sample.value, w.value):
                        return True, {"wait": w.tag, "result": "ok", "value": sample.value}
            if loop.time() >= deadline:
                return False, {"wait": w.tag, "result": "failed",
                               "message": f"condition {w.op} {w.value} not met in "
                                          f"{w.timeout_s:g}s"}
            await asyncio.sleep(0.2)
