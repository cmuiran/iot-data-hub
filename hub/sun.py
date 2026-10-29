"""Sunrise and sunset (NOAA solar position, accurate to about a minute below the polar circles).

``sun_times(day, latitude, longitude)`` -> (sunrise, sunset) in UTC, or None where the sun
does not rise or set that day.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

ZENITH = 90.833          # the sun's upper edge at the horizon, with refraction


def _sun_utc_minutes(day: date, latitude: float, longitude: float, rising: bool) -> float | None:
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat = math.radians(latitude)
    cos_ha = (math.cos(math.radians(ZENITH)) / (math.cos(lat) * math.cos(decl))
              - math.tan(lat) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None                                   # polar day or night
    ha = math.degrees(math.acos(cos_ha))
    if not rising:
        ha = -ha
    return 720 - 4 * (longitude + ha) - eqtime


def sun_times(day: date, latitude: float, longitude: float
              ) -> tuple[datetime | None, datetime | None]:
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    out = []
    for rising in (True, False):
        minutes = _sun_utc_minutes(day, latitude, longitude, rising)
        out.append(None if minutes is None else midnight + timedelta(minutes=minutes))
    return out[0], out[1]
