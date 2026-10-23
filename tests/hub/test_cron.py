from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.cron import Cron, CronError

T = datetime(2026, 10, 1, 8, 10, tzinfo=UTC)   # Thursday, 11:40 in Tehran


@pytest.mark.parametrize(
    "expr,expected_utc",
    [("*/15 * * * *", "2026-10-01T08:15"),                 # every 15 min
     ("0 6 * * *", "2026-10-02T02:30"),                    # 06:00 Tehran = 02:30 UTC
     ("30 11 * * *", "2026-10-02T08:00"),                  # 11:30 local already passed today
     ("0 12-18/2 * * *", "2026-10-01T08:30"),              # 12:00 local today
     ("0 7 * * 6", "2026-10-03T03:30"),                    # Saturday 07:00 local
     ("0 0 1 * *", "2026-10-31T20:30"),                    # 1 Nov 00:00 local
     ("0 8 15 * 0", "2026-10-04T04:30")],                  # day 15 OR Sunday -> Sunday first
)
def test_next_after(expr: str, expected_utc: str) -> None:
    got = Cron.parse(expr).next_after(T, "Asia/Tehran")
    assert got.astimezone(UTC).strftime("%Y-%m-%dT%H:%M") == expected_utc


def test_sunday_as_7_and_lists() -> None:
    cron = Cron.parse("5,35 9 * * 7")
    assert cron.weekdays == frozenset({0}) and cron.minutes == frozenset({5, 35})


@pytest.mark.parametrize("expr", ["* * * *", "61 * * * *", "* 24 * * *", "*/0 * * * *",
                                  "a * * * *", "5-1 * * * *", "0 0 31 2 *"])
def test_invalid(expr: str) -> None:
    with pytest.raises(CronError):
        Cron.parse(expr).next_after(T, "UTC")
