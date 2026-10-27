"""The Jalali (Persian solar) calendar: conversion, weekdays, month lengths, fixed holidays.

Arithmetic 33-year-cycle algorithm (as used by jalaali-js), exact for 1178-1633 AP, far beyond
any date a schedule uses. Weekdays count from Saturday: 0 = شنبه ... 6 = جمعه.

Official holidays: the solar ones are fixed (FIXED_HOLIDAYS). The lunar (Hijri) holidays move
every year and are not computed here: an administrator or the home owner adds them as dates
(table holidays, backend.automations).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

FIXED_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 1): "نوروز", (1, 2): "نوروز", (1, 3): "نوروز", (1, 4): "نوروز",
    (1, 12): "روز جمهوری اسلامی", (1, 13): "روز طبیعت",
    (3, 14): "رحلت امام خمینی", (3, 15): "قیام ۱۵ خرداد",
    (11, 22): "پیروزی انقلاب اسلامی", (12, 29): "ملی شدن صنعت نفت",
}
WEEKDAYS_FA = ("شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه")
MONTHS_FA = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر",
             "دی", "بهمن", "اسفند")
_DATE_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")


def to_jalali(d: date) -> tuple[int, int, int]:
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
            + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def to_gregorian(jy: int, jm: int, jd: int) -> date:
    if not (1 <= jm <= 12 and 1 <= jd <= month_length(jy, jm)):
        raise ValueError(f"{jy}/{jm}/{jd} is not a Jalali date")
    return _gregorian(jy, jm, jd)


def _gregorian(jy: int, jm: int, jd: int) -> date:
    jy += 1595
    days = (-355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
            + ((jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186))
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    return date(gy, 1, 1) + timedelta(days=days)


def is_leap(jy: int) -> bool:
    """Esfand has 30 days in a leap year."""
    return to_jalali(_gregorian(jy, 12, 30)) == (jy, 12, 30)


def month_length(jy: int, jm: int) -> int:
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_leap(jy) else 29


def weekday(d: date) -> int:
    """0 = Saturday (شنبه) ... 6 = Friday (جمعه)."""
    return (d.weekday() + 2) % 7


def parse(value: str) -> tuple[int, int, int]:
    """'1405/08/05' (or with -) -> (1405, 8, 5), validated."""
    m = _DATE_RE.fullmatch(value.strip())
    if m is None:
        raise ValueError(f"{value!r} is not a Jalali date YYYY/MM/DD")
    jy, jm, jd = (int(x) for x in m.groups())
    to_gregorian(jy, jm, jd)
    return jy, jm, jd


def fmt(jy: int, jm: int, jd: int) -> str:
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def fixed_holiday(d: date) -> str | None:
    _, jm, jd = to_jalali(d)
    return FIXED_HOLIDAYS.get((jm, jd))
