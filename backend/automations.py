"""Scenes and automations (platform phase 6), on the Jalali calendar.

An **automation** = trigger + conditions + actions of one organization:

* triggers: ``time`` (a time of day on chosen Jalali weekdays / months / days of the month,
  between Jalali dates, on or off holidays), ``interval`` (every N minutes, optionally between
  two times), ``once`` (a Jalali date and time), ``value`` (a device value meets a condition,
  optionally for some seconds), ``offline`` (a device is offline for some seconds);
* conditions (all must hold): device values, and a window (times, weekdays, holidays);
* actions: write a device tag, run a scene, wait, notify the creator by e-mail.

Every write goes through the control-safety layer as the automation's creator, whose rights
are checked at run time (a removed member's automations stop working). Priorities:

1. safety: the control layer (ranges, rate, interlocks, a second person) always applies;
2. a person: a tag a person changed in the last ``respect_manual_min`` minutes is left alone;
3. automations: of two writing one tag within a minute, the higher ``priority`` wins.

A run is stored once per occurrence (time ISO / value edge): two hub nodes or a restart never
run it twice. Occurrences missed while the hub was down are skipped (recorded as skipped), or
with ``missed: catch_up`` run once if at most ``catch_up_min`` minutes late. Time is the hub's
time zone (HUB_TIMEZONE, Asia/Tehran: no daylight saving since 1401; the zone database decides).
A **scene** is a named list of actions run on request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import operator
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

from backend.rbac import Principal
from hub import jalali
from hub.drivers.base import DeviceState, utcnow
from hub.models import Quality, Segment
from hub.platform.devices import platform_tag_path
from hub.sun import sun_times

log = logging.getLogger(__name__)
TICK_S = 5.0
GRACE = timedelta(seconds=30)          # an occurrence this late still counts as on time
PRIORITY_WINDOW = timedelta(seconds=60)
OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le,
    "gt": operator.gt, "ge": operator.ge}
Op = Literal["eq", "ne", "lt", "le", "gt", "ge"]
Value = StrictBool | StrictInt | StrictFloat


def _jdate(value: str) -> str:
    return jalali.fmt(*jalali.parse(value))


HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
JDate = Annotated[str, AfterValidator(_jdate)]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class Calendar(_M):
    """Which days count, on the Jalali calendar (weekday 0 = Saturday ... 6 = Friday)."""

    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(default=None,
                                                                     min_length=1)
    months: list[Annotated[int, Field(ge=1, le=12)]] | None = Field(default=None, min_length=1)
    month_days: list[Annotated[int, Field(ge=1, le=31)]] | None = Field(default=None,
                                                                        min_length=1)
    start: JDate | None = None
    end: JDate | None = None
    holidays: Literal["any", "skip", "only"] = "any"

    def day_ok(self, d: date, holiday: bool) -> bool:
        jy, jm, jd = jalali.to_jalali(d)
        if self.weekdays is not None and jalali.weekday(d) not in self.weekdays:
            return False
        if self.months is not None and jm not in self.months:
            return False
        if self.month_days is not None and jd not in self.month_days:
            return False
        if self.start is not None and d < jalali.to_gregorian(*jalali.parse(self.start)):
            return False
        if self.end is not None and d > jalali.to_gregorian(*jalali.parse(self.end)):
            return False
        if self.holidays == "skip" and holiday:
            return False
        return not (self.holidays == "only" and not holiday)


class TimeTrigger(Calendar):
    type: Literal["time"]
    at: HHMM


class IntervalTrigger(Calendar):
    type: Literal["interval"]
    every_min: int = Field(ge=1, le=1440)
    between: tuple[HHMM, HHMM] | None = None


class OnceTrigger(_M):
    type: Literal["once"]
    date: JDate
    at: HHMM


class ValueTrigger(_M):
    type: Literal["value"]
    device_id: Segment
    tag: Segment
    op: Op
    value: Value
    for_s: int = Field(default=0, ge=0, le=86_400)


class OfflineTrigger(_M):
    type: Literal["offline"]
    device_id: Segment
    for_s: int = Field(default=60, ge=10, le=86_400)


class SunTrigger(Calendar):
    """Sunrise or sunset at the home's place (the app fills in the coordinates), with an
    offset: -30 = half an hour before."""

    type: Literal["sun"]
    event: Literal["sunrise", "sunset"]
    offset_min: int = Field(default=0, ge=-180, le=180)
    latitude: float = Field(ge=-66, le=66)
    longitude: float = Field(ge=-180, le=180)


class PresenceTrigger(_M):
    """Someone of the home arrives or leaves (reported by the app: POST .../presence)."""

    type: Literal["presence"]
    event: Literal["arrive", "leave"]


Trigger = Annotated[TimeTrigger | IntervalTrigger | OnceTrigger | ValueTrigger | OfflineTrigger
                    | SunTrigger | PresenceTrigger, Field(discriminator="type")]
TIME_TRIGGERS = (TimeTrigger, IntervalTrigger, OnceTrigger, SunTrigger)


class Condition(_M):
    device_id: Segment
    tag: Segment
    op: Op
    value: Value


class Window(Calendar):
    between: tuple[HHMM, HHMM] | None = None


class WriteAction(_M):
    type: Literal["write"]
    device_id: Segment
    tag: Segment
    value: Value


class SceneAction(_M):
    type: Literal["scene"]
    scene_id: int


class DelayAction(_M):
    type: Literal["delay"]
    seconds: float = Field(gt=0, le=3600)


class NotifyAction(_M):
    type: Literal["notify"]
    text: str = Field(min_length=1, max_length=300)


Action = Annotated[WriteAction | SceneAction | DelayAction | NotifyAction,
                   Field(discriminator="type")]
SceneStep = Annotated[WriteAction | DelayAction, Field(discriminator="type")]


class AutomationIn(_M):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=10)
    window: Window | None = None
    actions: list[Action] = Field(min_length=1, max_length=30)
    priority: int = Field(default=50, ge=0, le=100)
    respect_manual_min: int = Field(default=60, ge=0, le=1440)
    missed: Literal["skip", "catch_up"] = "skip"
    catch_up_min: int = Field(default=30, ge=1, le=1440)
    local: bool = Field(default=False, description="also run on the device itself while it "
                        "has no connection to the hub (only rules about that one device)")
    timer: bool = Field(default=False, description="a countdown of the app: a once trigger, "
                        "removed after it ran")

    def definition(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"name", "enabled"})

    @classmethod
    def from_record(cls, record: Any) -> AutomationIn:
        return cls.model_validate(record.definition | {"name": record.name,
                                                       "enabled": record.enabled})


class SceneIn(_M):
    name: str = Field(min_length=1, max_length=80)
    icon: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    actions: list[SceneStep] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _writes(self) -> SceneIn:
        if not any(isinstance(a, WriteAction) for a in self.actions):
            raise ValueError("a scene needs at least one write")
        return self


def _scene_steps(scene: Any) -> list[Any]:
    """The actions of a stored scene (SceneRecord), validated again."""
    return SceneIn.model_validate(scene.model_dump(include={"name", "icon", "actions"})).actions


# --- the calendar ------------------------------------------------------------------------------


def _in_between(t: time, between: tuple[str, str] | None) -> bool:
    if between is None:
        return True
    start, end = _hhmm(between[0]), _hhmm(between[1])
    return start <= t < end if start <= end else (t >= start or t < end)   # may wrap midnight


def occurrences(trigger: Any, after: datetime, until: datetime, tz: ZoneInfo,
                holidays: set[date]) -> list[datetime]:
    """Occurrences of a time trigger with after < t <= until (UTC, sorted)."""
    out: list[datetime] = []
    first = after.astimezone(tz).date() - timedelta(days=1)
    last = until.astimezone(tz).date()
    day = first
    while day <= last:
        out += _day_times(trigger, day, tz, holidays)
        day += timedelta(days=1)
    return sorted(t for t in out if after < t <= until)


def _day_times(trigger: Any, day: date, tz: ZoneInfo, holidays: set[date]) -> list[datetime]:
    holiday = day in holidays or jalali.fixed_holiday(day) is not None
    if isinstance(trigger, OnceTrigger):
        if jalali.to_gregorian(*jalali.parse(trigger.date)) != day:
            return []
        return [datetime.combine(day, _hhmm(trigger.at), tz)]
    if not trigger.day_ok(day, holiday):
        return []
    if isinstance(trigger, TimeTrigger):
        return [datetime.combine(day, _hhmm(trigger.at), tz)]
    if isinstance(trigger, SunTrigger):
        rise, set_ = sun_times(day, trigger.latitude, trigger.longitude)
        at = rise if trigger.event == "sunrise" else set_
        return [] if at is None else [(at + timedelta(minutes=trigger.offset_min)).replace(
            second=0, microsecond=0)]
    times = []
    minute = 0
    while minute < 1440:
        t = time(minute // 60, minute % 60)
        if _in_between(t, trigger.between):
            times.append(datetime.combine(day, t, tz))
        minute += trigger.every_min
    return times


def next_runs(trigger: Any, now: datetime, tz: ZoneInfo, holidays: set[date],
              n: int = 5) -> list[datetime]:
    out: list[datetime] = []
    after = now
    for _ in range(40):                       # 40 x 10 days ahead at most
        until = after + timedelta(days=10)
        out += occurrences(trigger, after, until, tz, holidays)
        if len(out) >= n:
            break
        after = until
    return out[:n]


# --- the engine --------------------------------------------------------------------------------


class AutomationEngine:
    def __init__(self, services: Any, clock: Callable[[], datetime] = utcnow) -> None:
        self.svc = services
        self.clock = clock
        self.tz = ZoneInfo(services.settings.timezone)
        self._task: asyncio.Task[None] | None = None
        self._running: dict[int, asyncio.Task[Any]] = {}
        self._edges: dict[int, tuple[bool, datetime, bool]] = {}   # true?, since, fired
        self._recent: dict[str, tuple[int, datetime, int]] = {}    # tag -> prio, time, id

    @property
    def store(self) -> Any:
        return self.svc.store.automations

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="automations")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in list(self._running.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad automation must not stop the others
                log.exception("automation tick failed")
            try:
                await self.monthly()
            except Exception:  # noqa: BLE001 - billing must never stop the automations
                log.exception("monthly invoices failed")
            await asyncio.sleep(TICK_S)

    async def monthly(self) -> None:
        """Once per hour: the makers' invoices of the month that just ended (backend.api.maker)."""
        now = self.clock()
        hour = now.strftime("%Y%m%d%H")
        if getattr(self, "_billed_hour", None) == hour:
            return
        self._billed_hour = hour
        from backend.api.maker import close_month

        made = await close_month(self.svc, now)
        if made:
            log.info("issued %d monthly invoices", made)

    async def holidays(self, org_id: str, start: date, end: date) -> set[date]:
        return {h.day for h in await self.store.holidays(None, org_id, start, end)}

    async def tick(self) -> None:
        now = self.clock()
        for record in await self.store.automations(None):
            if not record.enabled or record.automation_id in self._running:
                continue
            try:
                spec = AutomationIn.from_record(record)
            except ValueError:
                log.error("automation %s has an invalid definition", record.automation_id)
                continue
            trigger = spec.trigger
            if isinstance(trigger, PresenceTrigger):
                continue                       # fired by presence() when the app reports
            if isinstance(trigger, TIME_TRIGGERS):
                await self._time(record, spec, now)
            else:
                key = self._edge(record.automation_id, trigger, now)
                if key is not None:
                    self.launch(record, spec, key)

    async def _time(self, record: Any, spec: AutomationIn, now: datetime) -> None:
        since = max(filter(None, (record.last_fired_at, record.updated_at)))
        after = max(since, now - timedelta(days=2))
        today = now.astimezone(self.tz).date()
        holidays = await self.holidays(record.org_id, today - timedelta(days=3), today)
        due = occurrences(spec.trigger, after, now, self.tz, holidays)
        if not due:
            return
        latest = due[-1]
        await self.store.set_fired(record.automation_id, latest)
        late = now - latest
        key = f"time:{latest.isoformat()}"
        if late <= GRACE or (spec.missed == "catch_up"
                             and late <= timedelta(minutes=spec.catch_up_min)):
            self.launch(record, spec, key)
            return
        run_id = await self.store.start_run(record.automation_id, key)
        if run_id is not None:
            await self.store.finish_run(run_id, "skipped", [{
                "result": "missed", "message": f"the hub was not running at {latest.isoformat()}"
            }])

    def _edge(self, automation_id: int, trigger: Any, now: datetime) -> str | None:
        """Value / offline triggers fire when the condition becomes true (and stays so for
        ``for_s``). At start-up an already-true condition does not fire."""
        if isinstance(trigger, ValueTrigger):
            met = self._value_ok(trigger.device_id, trigger.tag, trigger.op, trigger.value)
        else:
            status = self.svc.hub.statuses.get(trigger.device_id)
            met = status is not None and status.state is DeviceState.OFFLINE
        hold = timedelta(seconds=trigger.for_s)
        previous = self._edges.get(automation_id)
        if previous is None:
            self._edges[automation_id] = (met, now, met)      # fired=True: no start-up fire
            return None
        was, since, fired = previous
        if not met:
            self._edges[automation_id] = (False, now, False)
            return None
        if not was:
            since, fired = now, False
        if not fired and now - since >= hold:
            self._edges[automation_id] = (True, since, True)
            return f"{trigger.type}:{since.isoformat()}"
        self._edges[automation_id] = (True, since, fired)
        return None

    def _value_ok(self, device_id: str, tag: str, op: str, value: Any) -> bool:
        sample = self.svc.hub.latest.get(platform_tag_path(device_id, tag))
        if sample is None or sample.value is None or sample.quality is Quality.BAD:
            return False
        try:
            return bool(OPS[op](sample.value, value))
        except TypeError:
            return False

    async def presence(self, org_id: str, event: str, who: int) -> int:
        """Someone of the home arrived or left: run the home's presence automations."""
        now = self.clock()
        launched = 0
        for record in await self.store.automations(None, org_id):
            if not record.enabled or record.automation_id in self._running:
                continue
            try:
                spec = AutomationIn.from_record(record)
            except ValueError:
                continue
            if isinstance(spec.trigger, PresenceTrigger) and spec.trigger.event == event:
                self.launch(record, spec, f"presence:{event}:{who}:{now.isoformat()}")
                launched += 1
        return launched

    def launch(self, record: Any, spec: AutomationIn, key: str) -> None:
        task = asyncio.create_task(self.run(record, spec, key), name=f"automation:{key}")
        self._running[record.automation_id] = task
        task.add_done_callback(lambda _: self._running.pop(record.automation_id, None))

    # --- running ------------------------------------------------------------------------------

    async def _creator(self, user_id: int) -> Principal | None:
        store = self.svc.store
        user = await store.get_user(user_id)
        if user is None or not user.is_active:
            return None
        return Principal(user=user, session_id=None, grants=tuple(await store.grants(user_id)),
                         role_permissions=await store.role_permissions(),
                         site_orgs=self.svc.auth.site_orgs())

    def conditions_ok(self, spec: AutomationIn, now: datetime, holidays: set[date]
                      ) -> tuple[bool, str]:
        if spec.window is not None:
            local = now.astimezone(self.tz)
            day = local.date()
            holiday = day in holidays or jalali.fixed_holiday(day) is not None
            if not spec.window.day_ok(day, holiday):
                return False, "outside the days of the window"
            if not _in_between(local.time(), spec.window.between):
                return False, "outside the hours of the window"
        for c in spec.conditions:
            if not self._value_ok(c.device_id, c.tag, c.op, c.value):
                return False, f"condition {c.tag} {c.op} {c.value} not met"
        return True, ""

    async def run(self, record: Any, spec: AutomationIn, key: str, *, dry: bool = False
                  ) -> dict[str, Any]:
        run_id = None if dry else await self.store.start_run(record.automation_id, key)
        if run_id is None and not dry:
            return {"status": "duplicate"}
        now = self.clock()
        today = now.astimezone(self.tz).date()
        ok, why = self.conditions_ok(spec, now, await self.holidays(record.org_id, today, today))
        detail: list[dict[str, Any]] = []
        if not ok:
            status = "skipped"
            detail.append({"result": "skipped", "message": why})
        else:
            creator = await self._creator(record.created_by)
            if creator is None:
                status = "failed"
                detail.append({"result": "failed", "message": "the creator is disabled or gone"})
            else:
                detail = await self._actions(record, spec, spec.actions, creator, dry=dry)
                results = [d["result"] for d in detail if "tag" in d]
                good = [r for r in results if r in ("applied", "would_write")]
                status = ("ok" if len(good) == len(results) else
                          "partial" if good else "failed") if results else "ok"
        if run_id is not None:
            await self.store.finish_run(run_id, status, detail)
            if spec.timer and status != "skipped":     # a countdown is done once it ran
                await self.store.delete_automation(None, record.org_id, record.automation_id)
            self.svc.hub.bus.publish({"type": "automation", "automation_id":
                                      record.automation_id, "status": status})
            log.log(logging.INFO if status in ("ok", "skipped") else logging.WARNING,
                    "automation %s %r (%s): %s", record.automation_id, spec.name, key, status)
        return {"status": status, "detail": detail}

    async def _actions(self, record: Any, spec: AutomationIn | None, actions: list[Any],
                       creator: Principal, *, dry: bool = False, depth: int = 0,
                       reason: str | None = None) -> list[dict[str, Any]]:
        reason = reason or f"automation:{record.automation_id} {spec.name if spec else ''}"
        detail: list[dict[str, Any]] = []
        for action in actions:
            if isinstance(action, DelayAction):
                if not dry:
                    await asyncio.sleep(action.seconds)
                detail.append({"delay_s": action.seconds, "result": "ok"})
            elif isinstance(action, NotifyAction):
                detail.append(await self._notify(creator, spec, action, dry))
            elif isinstance(action, SceneAction):
                if depth:
                    detail.append({"scene": action.scene_id, "result": "failed",
                                   "message": "scenes do not run other scenes"})
                    continue
                try:
                    scene = await self.store.scene(None, action.scene_id)
                except ValueError:
                    detail.append({"scene": action.scene_id, "result": "failed",
                                   "message": "scene not found"})
                    continue
                if scene.org_id != record.org_id:
                    detail.append({"scene": action.scene_id, "result": "failed",
                                   "message": "scene of another organization"})
                    continue
                steps = _scene_steps(scene)
                detail += await self._actions(record, spec, steps, creator, dry=dry, depth=1,
                                              reason=reason)
            else:
                detail.append(await self._write(record, spec, action, creator, dry, reason))
        return detail

    async def _write(self, record: Any, spec: AutomationIn | None, action: WriteAction,
                     creator: Principal, dry: bool, reason: str) -> dict[str, Any]:
        path = platform_tag_path(action.device_id, action.tag)
        info: dict[str, Any] = {"tag": path, "value": action.value}
        now = self.clock()
        if spec is not None and spec.respect_manual_min:
            manual = await self.store.last_manual_write(
                path, now - timedelta(minutes=spec.respect_manual_min))
            if manual is not None:
                return info | {"result": "skipped_manual",
                               "message": f"changed by a person at {manual.isoformat()}"}
        priority = spec.priority if spec is not None else 100     # a scene run by a person
        recent = self._recent.get(path)
        if recent is not None and recent[2] != record.automation_id and \
                now - recent[1] < PRIORITY_WINDOW and recent[0] > priority:
            return info | {"result": "skipped_priority",
                           "message": f"automation {recent[2]} has a higher priority"}
        if dry:
            return info | {"result": "would_write"}
        try:
            result = await self.svc.control.request_write(creator, path, action.value, reason,
                                                          None)
        except Exception as exc:  # noqa: BLE001 - e.g. the device was removed
            return info | {"result": "failed", "message": str(exc)}
        if result.result == "applied" and spec is not None:     # priorities: automations only
            self._recent[path] = (priority, now, record.automation_id)
        return info | {"result": result.result, "message": result.message,
                       "request_id": str(result.request_id)}

    async def _notify(self, creator: Principal, spec: AutomationIn | None,
                      action: NotifyAction, dry: bool) -> dict[str, Any]:
        email = creator.user.email
        if not email:
            return {"notify": True, "result": "failed", "message": "the creator has no e-mail"}
        if not dry:
            from backend.mailer import render

            subject, body = render("automation", creator.user.language,
                                   name=spec.name if spec else "", text=action.text)
            try:
                await self.svc.accounts.mailer.send(email, subject, body)
            except Exception as exc:  # noqa: BLE001
                return {"notify": True, "result": "failed", "message": str(exc)}
        return {"notify": True, "result": "ok"}

    async def run_scene(self, principal: Principal, scene: Any) -> list[dict[str, Any]]:
        """A scene run by a person, with that person's rights."""

        class _SceneRun:
            automation_id = 0
            org_id = scene.org_id

        steps = _scene_steps(scene)
        return await self._actions(_SceneRun(), None, steps, principal,
                                   reason=f"scene:{scene.scene_id} {scene.name}")
