"""Five-field cron expressions: ``minute hour day-of-month month day-of-week``.

Supports ``*``, lists ``1,15``, ranges ``1-5``, steps ``*/10`` and ``8-18/2``; day of week is
0-6 with 0 = Sunday (7 is accepted as Sunday too). As in classic cron, when both day-of-month
and day-of-week are restricted a day matches if *either* does. Times are wall-clock times in
the schedule's time zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["minute", "hour", "day of month", "month", "day of week"]


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, name: str) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step in {name}: {step_text!r}")
            step = int(step_text)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"bad range in {name}: {part!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = hi
        else:
            raise CronError(f"bad value in {name}: {part!r}")
        if not lo <= start <= end <= hi:
            raise CronError(f"{name} must be within {lo}-{hi}: {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]          # 0 = Sunday
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> Cron:
        parts = text.split()
        if len(parts) != 5:
            raise CronError("a cron expression has 5 fields: minute hour day month weekday")
        fields = [_field(p, lo, hi, n) for p, (lo, hi), n in zip(parts, _RANGES, _NAMES,
                                                                 strict=True)]
        weekdays = frozenset(0 if d == 7 else d for d in fields[4])
        return cls(text=" ".join(parts), minutes=fields[0], hours=fields[1], days=fields[2],
                   months=fields[3], weekdays=weekdays, any_day=parts[2] == "*",
                   any_weekday=parts[4] == "*")

    def _day_matches(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays   # isoweekday: Mon=1..Sun=7
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def next_after(self, after: datetime, tz: str) -> datetime:
        """First matching minute strictly after ``after`` (aware), returned as aware UTC-ish
        datetime in ``after``'s zone."""
        zone = ZoneInfo(tz)
        local = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 5):  # days
            if self._day_matches(local):
                for hour in sorted(h for h in self.hours if h >= local.hour):
                    first_minute = local.minute if hour == local.hour else 0
                    for minute in sorted(m for m in self.minutes if m >= first_minute):
                        candidate = local.replace(hour=hour, minute=minute)
                        return candidate.astimezone(after.tzinfo)
            local = (local + timedelta(days=1)).replace(hour=0, minute=0)
        raise CronError(f"{self.text!r} never matches")
