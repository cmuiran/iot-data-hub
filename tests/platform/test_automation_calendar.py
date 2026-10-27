"""Jalali calendar and the occurrences of time triggers (no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.automations import AutomationIn, Trigger, next_runs, occurrences
from hub import jalali

TZ = ZoneInfo("Asia/Tehran")
T = TypeAdapter(Trigger)


def trig(**data: object) -> object:
    return T.validate_python(data)


def local(dt: datetime) -> tuple[str, str]:
    d = dt.astimezone(TZ)
    return jalali.fmt(*jalali.to_jalali(d.date())), d.strftime("%H:%M")


def test_jalali_conversion() -> None:
    assert jalali.to_jalali(date(2026, 10, 27)) == (1405, 8, 5)
    assert jalali.to_gregorian(1405, 1, 1) == date(2026, 3, 21)
    assert jalali.to_gregorian(1403, 12, 30) == date(2025, 3, 20)          # leap Esfand
    assert [y for y in range(1398, 1410) if jalali.is_leap(y)] == [1399, 1403, 1408]
    with pytest.raises(ValueError):
        jalali.to_gregorian(1404, 12, 30)
    d = date(2000, 1, 1)
    while d < date(2040, 1, 1):
        assert jalali.to_gregorian(*jalali.to_jalali(d)) == d
        d += timedelta(days=1)
    assert jalali.weekday(date(2026, 10, 31)) == 0                         # Saturday
    assert jalali.fixed_holiday(date(2026, 4, 2)) == "روز طبیعت"


def test_weekdays_saturday_to_wednesday() -> None:
    t = trig(type="time", at="08:00", weekdays=[0, 1, 2, 3, 4])
    start = datetime(2026, 10, 30, tzinfo=UTC)                            # a Friday
    got = occurrences(t, start, start + timedelta(days=7), TZ, set())
    assert [local(x) for x in got] == [                                    # Sat 9 Aban ...
        ("1405/08/09", "08:00"), ("1405/08/10", "08:00"), ("1405/08/11", "08:00"),
        ("1405/08/12", "08:00"), ("1405/08/13", "08:00")]
    assert got[0].astimezone(UTC).strftime("%H:%M") == "04:30"            # UTC+3:30


def test_first_of_every_jalali_month() -> None:
    t = trig(type="time", at="09:00", month_days=[1])
    got = next_runs(t, datetime(2026, 10, 27, tzinfo=UTC), TZ, set(), 3)
    assert [local(x) for x in got] == [("1405/09/01", "09:00"), ("1405/10/01", "09:00"),
                                       ("1405/11/01", "09:00")]


def test_holidays() -> None:
    every_day = trig(type="time", at="07:00", holidays="skip")
    start = datetime(2026, 3, 18, tzinfo=UTC)                             # 1404/12/27
    days = [local(x)[0] for x in occurrences(every_day, start, start + timedelta(days=9),
                                             TZ, set())]
    assert days == ["1404/12/27", "1404/12/28", "1405/01/05", "1405/01/06"]   # Nowruz, 12/29
    extra = {jalali.to_gregorian(1405, 1, 5)}                             # a lunar holiday
    days = [local(x)[0] for x in occurrences(every_day, start, start + timedelta(days=9),
                                             TZ, extra)]
    assert "1405/01/05" not in days
    only = trig(type="time", at="10:00", holidays="only")
    days = [local(x)[0] for x in occurrences(only, start, start + timedelta(days=9), TZ, set())]
    assert days == ["1404/12/29", "1405/01/01", "1405/01/02", "1405/01/03", "1405/01/04"]


def test_interval_and_window_over_midnight() -> None:
    t = trig(type="interval", every_min=30, between=["08:00", "10:00"])
    start = datetime(2026, 10, 27, tzinfo=UTC)
    got = [local(x)[1] for x in occurrences(t, start, start + timedelta(hours=20), TZ, set())]
    assert got == ["08:00", "08:30", "09:00", "09:30"]
    night = trig(type="interval", every_min=60, between=["22:00", "02:00"])
    got = [local(x)[1] for x in occurrences(night, datetime(2026, 10, 27, 12, tzinfo=UTC),
                                            datetime(2026, 10, 28, 12, tzinfo=UTC), TZ, set())]
    assert got == ["22:00", "23:00", "00:00", "01:00"]


def test_once_and_bounds() -> None:
    once = trig(type="once", date="1405/09/01", at="18:30")
    assert [local(x) for x in next_runs(once, datetime(2026, 10, 27, tzinfo=UTC), TZ, set(),
                                        5)] == [("1405/09/01", "18:30")]
    bounded = trig(type="time", at="06:00", start="1405/08/10", end="1405/08/12")
    got = [local(x)[0] for x in next_runs(bounded, datetime(2026, 10, 27, tzinfo=UTC), TZ,
                                          set(), 10)]
    assert got == ["1405/08/10", "1405/08/11", "1405/08/12"]


def test_validation() -> None:
    for bad in ({"type": "time", "at": "25:00"}, {"type": "once", "date": "1404/12/30",
                                                  "at": "10:00"},
                {"type": "time", "at": "08:00", "weekdays": [7]},
                {"type": "interval", "every_min": 0}):
        with pytest.raises(ValidationError):
            trig(**bad)
    with pytest.raises(ValidationError):
        AutomationIn.model_validate({"name": "x", "trigger": {"type": "time", "at": "08:00"},
                                     "actions": []})
